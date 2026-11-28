"""The license API the Doppleganger app talks to, the /license page, and Square's webhook.

App API (JSON, POST):
  /api/v1/license/status   {device_id, device_name, platform, app_version, key}  -> what this computer may do
  /api/v1/license/lease    {... , action: start|renew|release}                    -> permission to keep the LOOP on air
"""
import hmac
import json
import logging
import time

from flask import Blueprint, Response, g, jsonify, redirect, render_template, request, session, url_for

from . import growth, hhauth, licenses, settings, signing, square

log = logging.getLogger('dopplesite')
bp = Blueprint('api', __name__)

_hits = {}   # ip -> [timestamps]; per worker, a simple brake on key guessing


def _too_many(ip, limit=600, window=60):   # generous: many people can share one office IP
    now = time.time()
    recent = [t for t in _hits.get(ip, []) if now - t < window]
    recent.append(now)
    _hits[ip] = recent
    return len(recent) > limit


def _device(create):
    d = request.get_json(silent=True) or {}
    dev = licenses.touch_device(d.get('device_id'), d.get('device_name', ''), d.get('platform', ''), d.get('app_version', ''),
                                request.remote_addr or '', create=create, vm=bool(d.get('vm')))
    return d, dev


@bp.errorhandler(licenses.LicenseError)
def license_error(e):
    return jsonify(error=str(e), code=e.code), e.status


def _signed(out, d):
    """The reply, signed: it echoes the app's nonce and computer, and X-BRB-Signature covers the exact bytes."""
    out = {**out, 'nonce': str(d.get('nonce') or '')[:64], 'device_id': str(d.get('device_id') or '')}
    body = json.dumps(out, separators=(',', ':'), sort_keys=True).encode()
    resp = Response(body, mimetype='application/json')
    resp.headers['X-BRB-Signature'] = signing.sign(body)
    resp.headers['Cache-Control'] = 'no-store'
    return resp


@bp.get('/api/v1/license/public-key')   # (not /.well-known: the web server answers those paths itself)
def license_public_key():
    return Response(signing.public_key() + '\n', mimetype='text/plain')


@bp.post('/api/v1/license/status')
def license_status():
    if _too_many(request.remote_addr or '?'):
        return jsonify(error='Too many requests. Try again in a minute.', code='slow_down'), 429
    d = request.get_json(silent=True) or {}
    d, dev = _device(create=bool(d.get('key')))   # typing in a key records the computer (it's put on the key)
    out = licenses.status(dev, d.get('key'))
    _funnel(d, out)
    return _signed(out, d)


def _funnel(d, out, on_air=False):
    try:
        growth.note_status(d, out, on_air=on_air)
    except Exception:  # noqa: BLE001 - counting must never break licensing
        log.exception('funnel')


@bp.post('/api/v1/crash')
def crash_report():
    """An opt-in crash report from the app: {device_id, app_version, platform, kind, report}."""
    if _too_many('crash:' + (request.remote_addr or '?'), limit=20):
        return jsonify(error='Too many reports.', code='slow_down'), 429
    if (request.content_length or 0) > growth.MAX_REPORT * 2:
        return jsonify(error='Report too large.', code='too_large'), 413
    try:
        cid = growth.save_crash(request.get_json(silent=True) or {})
    except ValueError as e:
        return jsonify(error=str(e), code='bad_report'), 400
    return jsonify(ok=True, id=cid)


@bp.post('/api/v1/license/lease')
def license_lease():
    if _too_many(request.remote_addr or '?'):
        return jsonify(error='Too many requests. Try again in a minute.', code='slow_down'), 429
    d = request.get_json(silent=True) or {}
    action = str(d.get('action') or 'start')
    d, dev = _device(create=action != 'release')
    if dev.get('new'):   # releasing a lease it never had: nothing to do
        return _signed({**licenses.status(dev, d.get('key')), 'ok': True, 'lease_seconds': 0}, d)
    out = licenses.lease(dev, d.get('key'), action)
    if action == 'start' and out.get('ok'):
        _funnel(d, out, on_air=True)
    return _signed(out, d)


# ------------------------------------------------------------------ /license (people)
def _csrf_ok():
    return hmac.compare_digest(request.form.get('csrf', ''), session.get('csrf', '') or '-')


def _link_token():
    """The computer token from the app's link (?d=..., or ?offer=... from older versions), kept through sign-in."""
    t = request.args.get('d') or request.args.get('offer')
    if t:
        session['offer'] = t
    return session.get('offer')


def _offer_device():
    """(Older: the early-bird offer's computer - no longer offered.)"""
    t = _link_token()
    return licenses.offer_device(t) if t else None


def _linked_device():
    """The computer whose link brought this visitor here (a purchase is put on it)."""
    t = _link_token()
    return licenses.linked_device(t) if t else None


@bp.get('/license')
def license_page():
    square.settle_if_pending(g.user)
    keys = licenses.for_user(g.user['id'], g.user.get('email')) if g.user else []
    computer = _linked_device()
    if request.args.get('offer') or request.args.get('d'):   # tidy URL (the token is in the session now)
        return redirect(url_for('api.license_page', n=request.args.get('n')), 303)
    return render_template('license.html', keys=keys, free_minutes=licenses.free_seconds() // 60,
                           computer=computer, square_ready=square.ready(), sso=hhauth.enabled(),
                           notice=request.args.get('n'), option=request.args.get('plan', 'annual'),
                           staff_tests=square.test_offers() if g.user and g.user.get('role') in ('admin', 'support') else {},
                           describe=square.describe)


@bp.post('/license/subscribe')
def subscribe():
    if not g.user:
        return redirect(url_for('hh_start', next='/license'), 303)
    if not _csrf_ok():
        return redirect(url_for('api.license_page', n='expired'), 303)
    try:
        staff = g.user.get('role') in ('admin', 'support')
        option = request.form.get('plan') if request.form.get('plan') in ('monthly', 'test_daily', 'test_promo') else 'annual'
        from . import salestax
        try:
            where = salestax.quote(request.form.get('state'), request.form.get('zip'), request.form.get('county'))
        except ValueError as e:
            return render_template('message.html', title='Where are you?', body=str(e),
                                   action=('Back to checkout', url_for('api.license_page'))), 400
        session['tax_where'] = {k: where[k] for k in ('state', 'zip', 'county')}   # filled in next time
        url = square.checkout(g.user, settings.public_url() + url_for('api.subscribe_return'), option=option,
                              device=_linked_device(), staff=staff, where=where)
    except square.SquareError as e:
        return render_template('message.html', title="Couldn't open checkout", body=str(e),
                               action=('Back to your license', url_for('api.license_page'))), e.status
    return redirect(url, 303)


@bp.get('/license/tax-data')
def tax_data():
    """NC ZIP -> county and county -> rate, for the checkout form (about 20 KB, cached by the browser for a day)."""
    from . import salestax
    resp = jsonify(salestax.page_data())
    resp.headers['Cache-Control'] = 'public, max-age=86400'
    return resp


@bp.get('/license/check')
def license_check():
    """The license page, just back from checkout, asks every few seconds: is the key here yet? (Looks in Square too.)"""
    if not g.user:
        return jsonify(keys=0), 401
    n = len(licenses.for_user(g.user['id'], g.user.get('email')))
    if not n and square.ready():
        try:
            square.settle(g.user)
        except square.SquareError as e:
            log.warning('license check: %s', e)
        n = len(licenses.for_user(g.user['id'], g.user.get('email')))
    return jsonify(keys=n)


@bp.get('/license/return')
def subscribe_return():
    if g.user:
        try:
            square.settle(g.user)
        except square.SquareError as e:
            log.warning('settle after checkout: %s', e)
    return redirect(url_for('api.license_page', n='thanks'), 303)


@bp.post('/license/<lid>/cancel')
def cancel(lid):
    lic = licenses.get(lid)
    if not g.user or not lic or lic.get('user_id') != g.user['id'] or not _csrf_ok():
        return redirect(url_for('api.license_page'), 303)
    try:
        square.cancel(lic)
    except square.SquareError as e:
        return render_template('message.html', title="Couldn't cancel", body=str(e),
                               action=('Back to your license', url_for('api.license_page'))), e.status
    return redirect(url_for('api.license_page', n='canceled'), 303)


# ------------------------------------------------------------------ Square
@bp.post('/api/square/webhook')
def square_webhook():
    raw = request.get_data()
    url = settings.get('SQUARE_WEBHOOK_URL') or (settings.public_url() + '/api/square/webhook')
    if not square.verify(raw, request.headers.get('x-square-hmacsha256-signature', ''), url):
        log.warning('Square webhook with a bad signature')
        return jsonify(error='bad signature'), 403
    try:
        note = square.handle(json.loads(raw or b'{}'))
    except square.SquareError as e:
        log.warning('Square webhook: %s', e)
        return jsonify(error=str(e)), 503   # Square retries
    log.info('Square webhook: %s', note)
    return jsonify(ok=True, note=note)
