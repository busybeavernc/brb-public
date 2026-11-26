"""/admin: customers, keys, computers, support requests, numbers - and updating the live site from GitHub.

Ways in (same as happyhacks.io):
  - a BRB admin or support person, signed in with Happy Hacks (ADMIN_CONTACTS, or:
    python -m dopplesite.users admin|support you@example.com - or Admin > Team)
  - ADMIN_PASSWORD from .env, kept as a backup (for example if Happy Hacks sign-in is down): full admin
Support people can help customers (computers, free time, support requests); only admins create or change keys,
delete computers, export, run bulk actions, manage the team and update the website. Every change is in the audit log.
"""
import hmac
import os
import secrets
import time
from functools import wraps

from flask import Blueprint, Response, abort, g, jsonify, redirect, render_template, request, send_file, session, url_for

from . import audit, customers, hhauth, releases, selfupdate, settings, support, users

bp = Blueprint('admin', __name__)

SESSION_HOURS = 12
_failures = {}   # ip -> [timestamps]; per worker, enough to slow down password guessing


def _password():
    return os.environ.get('ADMIN_PASSWORD', '')


def _locked(ip):
    now = time.time()
    recent = [t for t in _failures.get(ip, []) if now - t < 900]
    _failures[ip] = recent
    return len(recent) >= 5


def _password_session():
    return bool(session.get('admin_pw')) and time.time() - session.get('admin_at', 0) < SESSION_HOURS * 3600


def _account_admin():
    u = g.get('user')
    return u if u and u.get('role') in ('admin', 'support') else None


def signed_in():
    return _password_session() or bool(_account_admin())


def role():
    """'admin' or 'support' (the admin password counts as admin)."""
    u = _account_admin()
    if u and u['role'] == 'admin':
        return 'admin'
    return 'admin' if _password_session() else ('support' if u else '')


def _who():
    u = _account_admin()
    return (u['name'] or u['email'] or 'admin') if u else 'admin password'


def admin_only(view):
    @wraps(view)
    def wrapped(*a, **kw):
        if role() != 'admin':
            return jsonify(error='Only an admin can do that.'), 403
        return view(*a, **kw)
    return wrapped


def login_required(view):
    @wraps(view)
    def wrapped(*a, **kw):
        if not signed_in():
            if request.path.startswith('/admin/api/'):
                return jsonify(error='Please sign in again.'), 401
            return redirect(url_for('admin.login'))
        if request.method == 'POST' and not hmac.compare_digest(
                request.headers.get('X-CSRF-Token', ''), session.get('csrf', '') or '-'):
            return jsonify(error='This page is out of date. Reload it and try again.'), 400
        return view(*a, **kw)
    return wrapped


@bp.route('/admin/login', methods=['GET', 'POST'])
def login():
    if request.method == 'GET' and signed_in():
        return redirect(url_for('admin.dashboard'))
    u = g.get('user')
    not_admin = (u['name'] or u['email'] or 'This account') if u and u.get('role') not in ('admin', 'support') else None
    error = None
    if request.method == 'POST' and not _password():
        error = 'The admin password is turned off (ADMIN_PASSWORD is empty in .env). Sign in with an admin account.'
    elif request.method == 'POST':
        ip = request.remote_addr or '?'
        if _locked(ip):
            error = 'Too many tries. Wait 15 minutes and try again.'
        elif hmac.compare_digest(request.form.get('password', '').encode(), _password().encode()):
            _failures.pop(ip, None)
            uid = session.get('uid')
            session.clear()
            session.permanent = True
            session.update(admin_pw=True, admin_at=time.time(), csrf=secrets.token_urlsafe(24))
            if uid:
                session['uid'] = uid
            return redirect(url_for('admin.dashboard'))
        else:
            _failures.setdefault(ip, []).append(time.time())
            error = 'That password is not right.'
    return render_template('admin_login.html', error=error, not_admin=not_admin, password_on=bool(_password()),
                           sso=hhauth.enabled()), (429 if error and 'Too many' in error else 200)


@bp.post('/admin/logout')
def logout():
    if hmac.compare_digest(request.form.get('csrf', ''), session.get('csrf', '') or '-'):
        session.pop('admin_pw', None)
        session.pop('admin_at', None)
        if _account_admin():
            session.pop('uid', None)
    return redirect(url_for('admin.login'))


@bp.get('/admin')
@login_required
def dashboard():
    return render_template('admin.html', who=_who(), role=role())


@bp.get('/admin/api/updates')
@login_required
def updates():
    fetch = request.args.get('fetch', '1') != '0'
    st = selfupdate.status(10 ** 12)
    st.pop('log', None)
    return jsonify({**selfupdate.version(fetch=fetch), **st})


@bp.post('/admin/api/updates')
@login_required
@admin_only
def update_run():
    try:
        pid = selfupdate.start(_who())
    except selfupdate.UpdateError as e:
        return jsonify(error=str(e)), 409
    return jsonify(ok=True, pid=pid)


@bp.get('/admin/api/updates/log')
@login_required
def update_log():
    return jsonify(selfupdate.status(request.args.get('from', 0, type=int)))


@bp.get('/admin/api/app')
@login_required
def app_status():
    """The Doppleganger app itself: latest release on GitHub, which downloads it has, download counts."""
    rel = releases.latest(force=request.args.get('fresh') == '1')
    have = releases.available()
    return jsonify({
        'repo': (rel or {}).get('repo') or settings.releases_repo(),
        'release': {'version': rel['version'], 'published': rel['published'], 'url': rel['url']} if rel else None,
        'platforms': [{'id': k, 'label': f"{p['label']} ({p['detail']})", 'ready': have[k]}
                      for k, p in releases.PLATFORMS.items()],
        'stats': users.download_stats(),
        'github': {'downloads': (rel or {}).get('github_downloads'), 'releases': (rel or {}).get('github_releases_counted')},
        'sso': hhauth.enabled(),
    })


@bp.after_request
def no_store(resp):
    if request.path.startswith('/admin'):
        resp.headers['Cache-Control'] = 'no-store'
        resp.headers['X-Robots-Tag'] = 'noindex'
    return resp


# ------------------------------------------------------------------ license keys and computers
def _lic_json(lic):
    out = {k: lic.get(k) for k in ('id', 'key', 'plan', 'status', 'max_devices', 'expires_at', 'email', 'note',
                                   'square_status', 'created_at', 'created_by', 'valid', 'why', 'renews_at', 'payment_failed_at', 'pay_url')}
    out['devices'] = [{k: d.get(k) for k in ('id', 'name', 'platform', 'app_version', 'added_at', 'last_seen')}
                      for d in lic.get('devices', [])]
    return out


@bp.get('/admin/api/licenses')
@login_required
def licenses_list():
    from . import licenses
    return jsonify(licenses=[_lic_json(x) for x in licenses.all_licenses(request.args.get('q', ''))],
                   free_seconds=licenses.free_seconds())


@bp.post('/admin/api/licenses')
@login_required
@admin_only
def licenses_create():
    from . import licenses
    d = request.get_json(silent=True) or {}
    try:
        days = int(d.get('days') or 0)
        n = int(d.get('max_devices') or 1)
    except ValueError:
        return jsonify(error='Days and computers must be numbers.'), 400
    lic = licenses.create(plan='unlimited', max_devices=max(1, min(50, n)),
                          expires_at=time.time() + days * 86400 if days > 0 else None,
                          email=str(d.get('email') or '').strip()[:120], note=str(d.get('note') or '').strip()[:200], by=_who())
    if d.get('device_id'):   # put it straight on a computer (e.g. someone who asked by email)
        dev = licenses.db().execute('SELECT id FROM devices WHERE id=?', (d['device_id'],)).fetchone()
        if dev:
            licenses.bind_device(lic['id'], dev['id'])
            lic = licenses.get(lic['id'])
    audit.log(_who(), 'create_key', lic['id'], key=lic['key'][-4:], email=lic['email'], note=lic['note'],
              max_devices=lic['max_devices'], device=d.get('device_id') or '')
    return jsonify(license=_lic_json(lic))


@bp.post('/admin/api/licenses/<lid>')
@login_required
@admin_only
def licenses_update(lid):
    from . import licenses
    if not licenses.get(lid):
        return jsonify(error='No such key.'), 404
    d = request.get_json(silent=True) or {}
    changes = {k: d[k] for k in ('status', 'max_devices', 'note', 'email') if k in d}
    if 'expires_days' in d:   # 0 / empty = never
        days = int(d['expires_days'] or 0)
        changes['expires_at'] = time.time() + days * 86400 if days > 0 else None
    try:
        lic = licenses.update(lid, **changes)
    except (licenses.LicenseError, ValueError) as e:
        return jsonify(error=str(e)), 400
    audit.log(_who(), 'change_key', lid, **changes)
    return jsonify(license=_lic_json(lic))


@bp.post('/admin/api/licenses/<lid>/devices/<did>/remove')
@login_required
def licenses_remove_device(lid, did):
    from . import licenses
    if licenses.remove_device(lid, did):
        audit.log(_who(), 'remove_computer_from_key', lid, device=did)
    return jsonify(license=_lic_json(licenses.get(lid)))


@bp.get('/admin/api/devices')
@login_required
def devices_list():
    from . import licenses
    return jsonify(devices=licenses.devices(request.args.get('q', '')), free_seconds=licenses.free_seconds())


@bp.post('/admin/api/devices/<did>/delete')
@login_required
@admin_only
def devices_delete(did):
    from . import licenses
    if not licenses.delete_device(did):
        return jsonify(error='No such computer.'), 404
    audit.log(_who(), 'delete_computer', did)
    return jsonify(ok=True)


@bp.post('/admin/api/devices/<did>/free')
@login_required
def devices_free(did):
    from . import licenses
    d = request.get_json(silent=True) or {}
    if d.get('reset'):
        n = licenses.add_free_time(did, 'reset')
    else:
        try:
            minutes = float(d.get('minutes') or 0)
        except ValueError:
            return jsonify(error='Minutes must be a number.'), 400
        if role() != 'admin' and abs(minutes) > 600:
            return jsonify(error='Support can add up to 10 hours at a time. Ask an admin for more.'), 403
        n = licenses.add_free_time(did, minutes * 60)
    if not n:
        return jsonify(error='No such computer.'), 404
    audit.log(_who(), 'reset_free_time' if d.get('reset') else 'add_free_time', did,
              **({} if d.get('reset') else {'minutes': d.get('minutes')}))
    return jsonify(ok=True)


# ------------------------------------------------------------------ who's here, team
@bp.get('/admin/api/me')
@login_required
def me():
    return jsonify(who=_who(), role=role())


@bp.get('/admin/api/team')
@login_required
def team():
    return jsonify(team=[{k: u[k] for k in ('id', 'name', 'email', 'phone', 'role', 'last_login_at')} for u in users.staff()])


@bp.post('/admin/api/team')
@login_required
@admin_only
def team_set():
    d = request.get_json(silent=True) or {}
    who, new = str(d.get('who') or '').strip(), str(d.get('role') or '')
    if new not in users.ROLES or not who:
        return jsonify(error='Choose a person (email or mobile) and a role.'), 400
    n = users.set_role(who, new)
    if not n:
        return jsonify(error=f'No account matches {who} yet. They need to sign in at the site once first.'), 404
    audit.log(_who(), 'set_role', who, role=new)
    return team()


# ------------------------------------------------------------------ search, customers, filters, numbers
@bp.get('/admin/api/search')
@login_required
def search():
    return jsonify(results=customers.search(request.args.get('q', '')))


def _cust_json(c):
    from . import licenses
    return {
        'user': c['user'], 'email': c['email'],
        'keys': [_lic_json(k) | {'moves_left': c['self_moves_left'].get(k['id'])} for k in c['keys']],
        'devices': [{k: d.get(k) for k in ('id', 'name', 'platform', 'app_version', 'free_left', 'last_seen', 'last_ip',
                                            'first_seen', 'linked')} | {'keys': d['keys']} for d in c['devices']],
        'tickets': c['tickets'], 'history': c['history'], 'free_seconds': licenses.free_seconds(),
    }


@bp.get('/admin/api/customer')
@login_required
def customer():
    c = customers.customer(request.args.get('who', ''))
    return jsonify(customer=_cust_json(c)) if c else (jsonify(error='No customer found.'), 404)


@bp.get('/admin/api/filters')
@login_required
def filters():
    return jsonify(filters=[{'id': k, 'label': v} for k, v in customers.FILTERS.items()])


@bp.get('/admin/api/filters/<name>')
@login_required
def filter_run(name):
    try:
        kind, rows = customers.run_filter(name)
    except KeyError:
        return jsonify(error='No such list.'), 404
    if kind == 'keys':
        rows = [_lic_json(x) for x in rows if x]
    return jsonify(kind=kind, rows=rows)


@bp.get('/admin/api/metrics')
@login_required
def metrics():
    from . import backup
    path, at = backup.latest()
    from . import mailer, mailin
    return jsonify(metrics=customers.metrics(), backup={'file': os.path.basename(path) if path else '', 'at': at},
                   mail={'sending': mailer.ready(), 'from': mailer.from_address(), 'notify': mailer.staff(),
                         'receiving': mailin.ready(), 'last_check': mailin.status['last_check'],
                         'error': mailin.status['last_error']})


def _pricing_json():
    from . import licenses, square
    saved = settings.saved()
    end = square.promo_ends()
    fields = {
        'monthly': ('MONTHLY_PRICE_CENTS', square.monthly_cents() / 100),
        'annual': ('ANNUAL_PRICE_CENTS', square.annual_cents() / 100),
        'promo_price': ('PROMO_ANNUAL_PRICE_CENTS', square.promo_cents() / 100),
        'promo_ends': ('PROMO_ENDS', end.isoformat() if end else ''),
        'promo_name': ('PROMO_NAME', square.promo_name()),
        'pitch': ('PITCH_TEXT', settings.get('PITCH_TEXT', '') or ''),
        'offer': ('OFFER_TEXT', settings.get('OFFER_TEXT', '') or ''),
        'stay_pct': ('RETENTION_PCT', square.retention_pct()),
        'test_prices': ('TEST_PRICES', square.test_prices_on()),
    }
    pr = square.pricing()
    return {'values': {k: v for k, (_n, v) in fields.items()},
            'from_admin': {k: n in saved for k, (n, _v) in fields.items()},
            'promo_active': pr['promo'], 'preview': pr,
            'options': {k: square.describe(o) for k, o in square.offers().items()},
            'free': licenses.free_words()}


@bp.get('/admin/api/pricing')
@login_required
def pricing_get():
    return jsonify(_pricing_json())


@bp.post('/admin/api/pricing')
@admin_only
def pricing_set():
    """Save prices / the promo / the app's wording (blank = back to .env), then bring Square up to date."""
    from datetime import date
    from . import square
    d = request.get_json(silent=True) or {}

    def cents(key, label):
        raw = str(d.get(key, '')).strip().replace('$', '').replace(',', '')
        if raw == '':
            return ''
        try:
            c = int(round(float(raw) * 100))
        except ValueError:
            raise ValueError(f'{label}: enter an amount like 9.99.')
        if not 50 <= c <= 1_000_000:
            raise ValueError(f'{label}: between $0.50 and $10,000.')
        return str(c)
    try:
        new = {'MONTHLY_PRICE_CENTS': cents('monthly', 'Monthly price'),
               'ANNUAL_PRICE_CENTS': cents('annual', 'Annual price'),
               'PROMO_ANNUAL_PRICE_CENTS': cents('promo_price', 'Promo first-year price')}
        ends = str(d.get('promo_ends', '')).strip()
        if ends and ends.lower() != 'off':
            try:
                date.fromisoformat(ends)
            except ValueError:
                raise ValueError('Promo ends: pick a date (or leave it empty for no promo).')
        new['PROMO_ENDS'] = ends or 'off'
        new['PROMO_NAME'] = str(d.get('promo_name', '')).strip()[:60]
        new['PITCH_TEXT'] = str(d.get('pitch', '')).strip()[:600]
        new['OFFER_TEXT'] = str(d.get('offer', '')).strip()[:600]
        sp = str(d.get('stay_pct', '')).strip().rstrip('%')
        if sp and not (sp.isdigit() and 0 <= int(sp) <= 90):
            raise ValueError('Stay offer: a whole percent from 0 (no offer) to 90.')
        new['RETENTION_PCT'] = sp
        new['TEST_PRICES'] = 'on' if d.get('test_prices') in (True, 'on', 'true', '1') else 'off'
        m = int(new['MONTHLY_PRICE_CENTS'] or square.monthly_cents())
        a = int(new['ANNUAL_PRICE_CENTS'] or square.annual_cents())
        p = int(new['PROMO_ANNUAL_PRICE_CENTS'] or square.promo_cents())
        if new['PROMO_ENDS'] != 'off' and p >= a:
            raise ValueError('The promo first-year price must be lower than the annual price.')
    except ValueError as e:
        return jsonify(error=str(e)), 400
    before = _pricing_json()['values']
    settings.save(new, by=_who())
    after = _pricing_json()['values']
    audit.log(_who(), 'pricing', 'site', **{k: f'{before[k]} -> {after[k]}' for k in after if before[k] != after[k]})
    out = _pricing_json()
    out['warning'] = ('Annual costs more than 12 months of monthly.' if a > 12 * m else '')
    if square.ready():
        try:
            out['square'] = square.sync_catalog()
        except square.SquareError as e:
            out['square_error'] = str(e)
    return jsonify(out)


@bp.get('/admin/api/cancellations')
@login_required
def cancellations():
    from . import square
    rows, counts = square.feedback_list(200)
    return jsonify(rows=rows, counts=counts)


@bp.get('/admin/api/square')
@login_required
def square_info():
    from . import square
    st = square.sync_status
    return jsonify(ready=square.ready(), env=square.env(), prices=[square.price_text(c) for c in square.wanted_prices()],
                   synced_at=st['at'], error=st['error'], report=st['report'],
                   webhook=bool(square._setting('WEBHOOK_SIGNATURE_KEY')))


@bp.post('/admin/api/square/promo-check')
@admin_only
def square_promo_check():
    """Run the promo check now (normally hourly) and say what it did."""
    from . import square
    try:
        n = square.promo_safeguard()
    except square.SquareError as e:
        return jsonify(error=str(e)), 502
    return jsonify(moved=n, report=square._promo_check['last'] or ['No promo subscriptions are due within 14 days.'])


@bp.post('/admin/api/square/sync')
@admin_only
def square_sync():
    from . import square
    try:
        report = square.sync_catalog()
    except square.SquareError as e:
        return jsonify(error=str(e)), 502
    audit.log(_who(), 'square_sync', report['plan_id'], created=', '.join(report['created']) or 'nothing')
    return jsonify(report=report)


@bp.get('/admin/export/<kind>.csv')
@login_required
@admin_only
def export(kind):
    try:
        body = customers.export(kind)
    except KeyError:
        abort(404)
    audit.log(_who(), 'export', kind)
    return Response(body, mimetype='text/csv', headers={
        'Content-Disposition': f'attachment; filename=brb-{kind}-{time.strftime("%Y-%m-%d")}.csv'})


@bp.post('/admin/api/bulk')
@login_required
@admin_only
def bulk():
    d = request.get_json(silent=True) or {}
    ids = [str(x) for x in (d.get('devices') or []) if isinstance(x, str)]
    if not ids:
        return jsonify(error='Pick some computers first.'), 400
    if d.get('action') == 'free_time':
        try:
            n = customers.bulk_free_time(ids, float(d.get('minutes') or 0), _who())
        except ValueError:
            return jsonify(error='Minutes must be a number.'), 400
    elif d.get('action') == 'block':
        n = customers.bulk_block(ids, _who())
    elif d.get('action') == 'delete':
        n = customers.bulk_delete_devices(ids, _who())
    else:
        return jsonify(error='Unknown action.'), 400
    return jsonify(ok=True, count=n)


@bp.get('/admin/api/audit')
@login_required
def audit_list():
    return jsonify(entries=audit.recent(300, q=request.args.get('q', '')))


# ------------------------------------------------------------------ support requests
def _ticket_json(t):
    return {k: t.get(k) for k in ('id', 'email', 'name', 'topic', 'subject', 'status', 'assignee', 'app_version',
                                  'platform', 'device_id', 'created_at', 'updated_at', 'user_id')} | {
        'topic_label': support.TOPICS.get(t.get('topic'), t.get('topic'))}


@bp.get('/admin/api/tickets')
@login_required
def tickets():
    rows, total = support.search(request.args.get('q', ''), request.args.get('status', 'active'),
                                 offset=request.args.get('offset', 0, type=int))
    return jsonify(tickets=[_ticket_json(t) for t in rows], total=total, counts=support.counts())


@bp.get('/admin/api/tickets/<int:tid>')
@login_required
def ticket(tid):
    t = support.get(tid)
    if not t:
        return jsonify(error='No such request.'), 404
    c = customers.customer(t['email']) or customers.customer(t['user_id'] or '')
    return jsonify(ticket=_ticket_json(t), messages=support.messages(tid), customer=_cust_json(c) if c else None,
                   canned=support.canned(), statuses=support.STATUSES)


@bp.post('/admin/api/tickets/<int:tid>/reply')
@login_required
def ticket_reply(tid):
    d = request.get_json(silent=True) or {}
    try:
        support.staff_reply(tid, _who(), d.get('body', ''), note=bool(d.get('note')), status=d.get('status'))
    except support.SupportError as e:
        return jsonify(error=str(e)), 400
    audit.log(_who(), 'support_note' if d.get('note') else 'support_reply', f'ticket {tid}')
    return ticket(tid)


@bp.post('/admin/api/tickets/<int:tid>/status')
@login_required
def ticket_status(tid):
    d = request.get_json(silent=True) or {}
    try:
        if 'status' in d:
            support.set_status(tid, d['status'], _who())
        if 'assignee' in d:
            support.assign(tid, d['assignee'] or '')
    except support.SupportError as e:
        return jsonify(error=str(e)), 400
    audit.log(_who(), 'support_update', f'ticket {tid}', **{k: d[k] for k in ('status', 'assignee') if k in d})
    return ticket(tid)


@bp.get('/admin/support/<int:tid>/file/<stored>')
@login_required
def ticket_file(tid, stored):
    p = support.attachment_path(tid, stored)
    if not p:
        abort(404)
    ext = os.path.splitext(stored)[1].lower()
    return send_file(p, mimetype=support.ATTACH_TYPES.get(ext, 'application/octet-stream'), as_attachment=True,
                     download_name=stored.split('-', 1)[-1])


@bp.post('/admin/api/canned')
@login_required
def canned_save():
    d = request.get_json(silent=True) or {}
    try:
        if d.get('delete'):
            support.delete_canned(int(d['id']))
        else:
            support.save_canned(d.get('title'), d.get('body'), d.get('id'))
    except (support.SupportError, ValueError, KeyError) as e:
        return jsonify(error=str(e) or 'Something is missing.'), 400
    return jsonify(canned=support.canned())
