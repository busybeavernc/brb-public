"""brb.happyhacks.io: the website of BRB (Be Right Back; formerly Doppleganger). Download page, setup guide, Sign in with Happy Hacks, /admin."""
import hmac
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone

from flask import Flask, abort, g, redirect, render_template, request, send_file, send_from_directory, session, url_for
from werkzeug.middleware.proxy_fix import ProxyFix

from . import hhauth, releases, settings

log = logging.getLogger('dopplesite')
ROOT = settings.ROOT


def create_app() -> Flask:
    from . import users   # imported here so `python -m dopplesite.users` runs cleanly
    app = Flask(__name__, static_folder=os.path.join(ROOT, 'static'), static_url_path='/static')
    app.config['SECRET_KEY'] = settings.get('SECRET_KEY', 'dev-only-change-me')
    app.config['MAX_CONTENT_LENGTH'] = 3 * 1024 * 1024   # support attachments are up to 2 MB
    app.config.update(
        SESSION_COOKIE_NAME='dg_session',
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE='Lax',   # Lax: coming back from happyhacks.io keeps the cookie
        SESSION_COOKIE_SECURE=os.environ.get('SESSION_COOKIE_SECURE', '1') != '0',
        PERMANENT_SESSION_LIFETIME=timedelta(days=30),
    )
    # nginx (CloudPanel) sits in front: trust its X-Forwarded-For/-Proto so client IPs and https are right
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    from .admin import bp as admin_bp
    from .api import bp as api_bp
    app.register_blueprint(admin_bp)
    app.register_blueprint(api_bp)

    def asset(filename):
        """Static URL with ?v=<mtime>: CloudPanel's vhost caches css/js/images forever, so a changed file needs a new URL."""
        try:
            v = int(os.path.getmtime(os.path.join(app.static_folder, filename)))
        except OSError:
            v = 0
        return url_for('static', filename=filename, v=v)

    def csrf_token():
        if not session.get('csrf'):
            session['csrf'] = secrets.token_urlsafe(24)
        return session['csrf']

    def csp_nonce():
        if 'csp_nonce' not in g:
            g.csp_nonce = secrets.token_urlsafe(16)
        return g.csp_nonce

    @app.after_request
    def security_headers(resp):
        """The usual protective headers; security reviewers check for these (securityheaders.com, Mozilla Observatory)."""
        h = resp.headers
        script = f"'self' 'nonce-{g.csp_nonce}'" if 'csp_nonce' in g else "'self'"
        h.setdefault('Content-Security-Policy',
                     f"default-src 'self'; script-src {script}; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                     "font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'")
        if request.is_secure:
            h.setdefault('Strict-Transport-Security', 'max-age=31536000; includeSubDomains')
        h.setdefault('X-Content-Type-Options', 'nosniff')
        # the same values CloudPanel's nginx adds, so the duplicate headers agree (CSP frame-ancestors is the strict one)
        h.setdefault('X-Frame-Options', 'SAMEORIGIN')
        h.setdefault('Referrer-Policy', 'same-origin')
        h.setdefault('Permissions-Policy', 'camera=(), microphone=(), geolocation=(), payment=(), usb=(), interest-cohort=()')
        h.setdefault('Cross-Origin-Opener-Policy', 'same-origin')
        return resp

    @app.before_request
    def load_user():
        g.user = users.get(session['uid']) if session.get('uid') else None
        if session.get('uid') and not g.user:
            session.pop('uid', None)

    def free_label():
        from . import licenses
        return licenses.free_words()

    def pricing():
        from . import square
        return square.pricing()

    def download_info():
        """For the Download Free dialog in the header: which downloads exist, and their sizes."""
        return {'available': releases.available(), 'sizes': {p: releases.size_mb(p) for p in releases.PLATFORMS}}

    @app.context_processor
    def inject():
        return {'pricing': pricing, 'asset': asset, 'user': g.get('user'), 'sso': hhauth.enabled(), 'csrf': csrf_token,
                'account_url': hhauth.account_url(), 'platforms': releases.PLATFORMS, 'download_info': download_info,
                'free_label_text': free_label, 'csp_nonce': csp_nonce}

    @app.template_filter('first_name')
    def first_name(u):
        """What to call someone in the header: their first name - not an email address or number that ended up in
        the name field (accounts made before Happy Hacks asked for a name)."""
        first = str((u or {}).get('name') or '').strip().split(' ')[0]
        return first if first and '@' not in first and not any(c.isdigit() for c in first) else 'Account'

    @app.template_filter('datefmt')
    def datefmt(ts):
        import datetime as _dt
        return _dt.datetime.fromtimestamp(float(ts)).strftime('%b %-d, %Y') if ts else ''

    @app.route('/favicon.ico')
    def favicon():
        return send_from_directory(app.static_folder, 'favicon.ico')

    @app.route('/')
    def index():
        rel = releases.latest()
        from . import square
        return render_template('index.html', release=rel, available=releases.available(), free_label=free_label(),
                               price=square.price_text(), early_price=square.price_text(square.early_price_cents()),
                               sizes={p: releases.size_mb(p) for p in releases.PLATFORMS},
                               need_signin=settings.flag('DOWNLOADS_REQUIRE_SIGNIN') and not g.user and hhauth.enabled())

    @app.route('/privacy-policy')
    @app.route('/privacy')       # the main address
    def privacy():
        return render_template('guide.html', guide_html=_md_html('PRIVACY.md'), title='Privacy Policy',
                               lede='What BRB collects, what it never collects, and why.', legal=True)

    def security_contact():
        return settings.get('SECURITY_CONTACT') or 'support@happyhacks.io'

    @app.route('/security')
    def security():
        html = _md_html('SECURITY.md').replace('support@happyhacks.io', security_contact())
        return render_template('guide.html', guide_html=html, title='Security',
                               lede='How to report a security problem, and how to check a download.', legal=True)

    @app.route('/.well-known/security.txt')
    @app.route('/security.txt')
    def security_txt():
        """RFC 9116. Expires is kept about six months ahead, as the RFC asks for a date under a year away."""
        root = (settings.get('PUBLIC_URL') or request.url_root).rstrip('/')
        expires = (datetime.now(timezone.utc) + timedelta(days=180)).strftime('%Y-%m-%dT00:00:00Z')
        body = (f"Contact: mailto:{security_contact()}\n"
                f"Expires: {expires}\n"
                f"Policy: {root}{url_for('security')}\n"
                f"Canonical: {root}/.well-known/security.txt\n"
                "Preferred-Languages: en\n")
        return app.response_class(body, mimetype='text/plain')

    @app.route('/for-it')
    @app.route('/it')            # the main address (registered first: the lower decorator)
    def for_it():
        return render_template('it.html', files={k: releases.details(k) for k in releases.PLATFORMS},
                               signing=settings.get('CODE_SIGNING') or '', free_label=free_label())

    @app.route('/see-it-in-action')
    def see_it():
        from . import square
        return render_template('see.html', price=square.price_text())

    @app.route('/guide')
    def guide():
        return render_template('guide.html', guide_html=_md_html('GUIDE.md'), title='Setup guide',
                               lede='Everything about installing and using BRB.')

    @app.route('/terms')
    @app.route('/eula')          # the main address (url_for uses the route registered first, i.e. the lower one)
    def eula():
        return render_template('guide.html', guide_html=_md_html('EULA.md'), title='End User License Agreement',
                               lede='The terms for using BRB.', legal=True)

    @app.route('/download/<platform>')
    def download(platform):
        if platform not in releases.PLATFORMS:
            abort(404)
        if settings.flag('DOWNLOADS_REQUIRE_SIGNIN') and hhauth.enabled() and not g.user:
            return redirect(url_for('hh_start', next=request.path), 303)
        url, version = releases.link(platform)
        if not url:
            return render_template('message.html', title='Not ready yet',
                                   body=f"The {releases.PLATFORMS[platform]['label']} "
                                        f"({releases.PLATFORMS[platform]['detail']}) download isn't in the latest "
                                        "release yet. Please check back in a few minutes."), 404
        try:
            users.count_download(platform, version)
        except Exception:   # noqa: BLE001 - counting must never stop a download
            log.exception('counting a download')
        return redirect(url, 302)

    # ---------- Sign in with Happy Hacks ----------
    def callback_url():
        return settings.public_url() + url_for('hh_callback')

    @app.get('/auth/happyhacks')
    def hh_start():
        if not hhauth.enabled():
            return render_template('message.html', title='Sign-in is not set up yet',
                                   body='Signing in with Happy Hacks is not turned on for this site yet.'), 503
        url, cookie = hhauth.begin(callback_url(), request.args.get('next') or '/', signup=bool(request.args.get('signup')))
        resp = redirect(url, 303)
        resp.set_cookie(hhauth.COOKIE, cookie, max_age=hhauth.COOKIE_TTL, httponly=True, samesite='Lax',
                        secure=app.config['SESSION_COOKIE_SECURE'], path='/auth/happyhacks')
        return resp

    @app.get('/auth/happyhacks/callback')
    def hh_callback():
        try:
            hh_user, nxt = hhauth.finish(request.args, request.cookies.get(hhauth.COOKIE), callback_url())
        except hhauth.HHError as e:
            return render_template('message.html', title="Couldn't sign you in", body=str(e),
                                   action=('Try again', url_for('hh_start'))), e.status
        user = users.sign_in(hh_user)
        session.clear()
        session.permanent = True
        session.update(uid=user['id'], csrf=secrets.token_urlsafe(24))
        resp = redirect(nxt, 303)
        resp.delete_cookie(hhauth.COOKIE, path='/auth/happyhacks')
        return resp

    @app.post('/signout')
    def signout():
        """Sign out here AND of Happy Hacks (in this browser): otherwise "Sign in" would just sign you straight back
        in, because Happy Hacks still remembers you. Happy Hacks then sends you back to this site's home page."""
        ok = hmac.compare_digest(request.form.get('csrf', ''), session.get('csrf', '') or '-')
        session.clear()
        if ok and hhauth.enabled():
            return redirect(hhauth.signout_url(settings.public_url() + '/'), 303)
        return redirect(url_for('index'), 303)

    # ---------- support requests and the account page ----------
    from . import audit, backup, licenses, mailin, support

    @app.before_request
    def nightly_backup():
        if request.endpoint not in (None, 'static', 'healthz'):
            backup.maybe_run()
            mailin.start()       # read the support mailbox in the background (if IMAP_ settings are set)
            from . import square
            square.sync_in_background()   # Square's plan matches the prices in .env (after start-up or a price change)

    def _form_csrf_ok():
        return hmac.compare_digest(request.form.get('csrf', ''), session.get('csrf', '') or '-')

    def _attach():
        f = request.files.get('attachment')
        return f if f and f.filename else None

    @app.route('/support', methods=['GET', 'POST'])
    @app.route('/contact', methods=['GET', 'POST'])
    def support_page():
        """The support form. BRB's Help > Contact support opens it with ?v=<version>&os=<platform>&d=<computer link>."""
        a = request.args
        form = {'email': (g.user or {}).get('email') or '', 'name': (g.user or {}).get('name') or '',
                'topic': a.get('topic', ''), 'subject': '', 'body': '',
                'app_version': a.get('v', '')[:20], 'platform': a.get('os', '')[:20]}
        if a.get('d'):
            session['support_device'] = a['d'][:200]
        error = None
        if request.method == 'POST':
            form.update({k: request.form.get(k, '') for k in ('email', 'name', 'topic', 'subject', 'body', 'app_version',
                                                              'platform')})
            ip = request.remote_addr or ''
            dev = licenses.linked_device(session.get('support_device')) if session.get('support_device') else None
            if request.form.get('website'):            # the honeypot field people never see: a bot filled it in
                return redirect(url_for('support_page', sent=1), 303)
            if not _form_csrf_ok():
                error = 'This page was open too long. Please send it again.'
            elif support.recent_from_ip(ip) >= 5:
                error = 'Too many requests from this connection in the last hour. Please try again later.'
            else:
                try:
                    t = support.create(form['email'], form['subject'], form['body'], name=form['name'], topic=form['topic'],
                                       user_id=(g.user or {}).get('id'), app_version=form['app_version'],
                                       platform=form['platform'], device_id=dev['id'] if dev else '', ip=ip,
                                       upload=_attach())
                    session.pop('support_device', None)
                    return redirect(url_for('support_ticket', token=t['token'], n='sent'), 303)
                except support.SupportError as e:
                    error = str(e)
        return render_template('support.html', form=form, topics=support.TOPICS, error=error,
                               mine=support.search(email=g.user.get('email'), user_id=g.user['id'], limit=20)[0] if g.user else [])

    @app.route('/support/t/<token>', methods=['GET', 'POST'])
    def support_ticket(token):
        t = support.by_token(token)
        if not t:
            abort(404)
        error = None
        if request.method == 'POST':
            if not _form_csrf_ok():
                error = 'This page was open too long. Please send it again.'
            else:
                try:
                    support.customer_reply(t, request.form.get('body', ''), _attach())
                    return redirect(url_for('support_ticket', token=token, n='replied'), 303)
                except support.SupportError as e:
                    error = str(e)
        return render_template('support_ticket.html', t=support.get(t['id']), msgs=support.messages(t['id'], include_notes=False),
                               topic=support.TOPICS.get(t['topic'], ''), error=error, notice=request.args.get('n'))

    @app.get('/support/t/<token>/file/<stored>')
    def support_file(token, stored):
        t = support.by_token(token)
        p = support.attachment_path(t['id'], stored) if t else None
        if not p:
            abort(404)
        ext = os.path.splitext(stored)[1].lower()
        return send_file(p, mimetype=support.ATTACH_TYPES.get(ext, 'application/octet-stream'), as_attachment=True,
                         download_name=stored.split('-', 1)[-1])

    @app.get('/account')
    def account():
        """Your keys, your computers (move a key yourself) and your support requests. ?d=<token> from BRB's
        "Link this computer to my account" links that computer to the signed-in account."""
        if request.args.get('d'):
            session['link_device'] = request.args['d'][:200]
            return redirect(url_for('account'), 303)
        if not g.user:
            if not hhauth.enabled():
                return render_template('message.html', title='Sign-in is not set up yet',
                                       body='Accounts are not turned on for this site yet.'), 503
            return redirect(url_for('hh_start', next='/account'), 303)
        linked = None
        if session.get('link_device'):
            dev = licenses.linked_device(session.pop('link_device'))
            if dev:
                licenses.link_device(dev['id'], g.user['id'])
                audit.log(g.user.get('email') or g.user['id'], 'link_computer', dev['id'])
                linked = dev
        keys = licenses.for_user(g.user['id'], g.user.get('email'))
        return render_template('account.html', keys=keys, devices=licenses.user_devices(g.user['id'], g.user.get('email')),
                               moves={k['id']: licenses.self_moves_left(k['id']) for k in keys},
                               tickets=support.search(email=g.user.get('email'), user_id=g.user['id'], limit=50)[0],
                               linked=linked, notice=request.args.get('n'), error=request.args.get('e'),
                               per_month=licenses.SELF_MOVES_PER_MONTH, hh_account=hhauth.account_url())

    @app.post('/account/keys/<lid>/computers/<did>/remove')
    def account_remove_device(lid, did):
        if not g.user or not _form_csrf_ok():
            return redirect(url_for('account'), 303)
        try:
            licenses.self_remove_device(g.user, lid, did)
        except licenses.LicenseError as e:
            return redirect(url_for('account', e=str(e)), 303)
        return redirect(url_for('account', n='moved'), 303)

    @app.post('/account/computers/<did>/unlink')
    def account_unlink(did):
        if g.user and _form_csrf_ok() and licenses.unlink_device(did, g.user['id']):
            audit.log(g.user.get('email') or g.user['id'], 'unlink_computer', did)
        return redirect(url_for('account'), 303)

    @app.route('/healthz')
    def healthz():
        return {'status': 'ok'}

    @app.errorhandler(404)
    def not_found(_e):
        return render_template('message.html', title='Page not found',
                               body="There's nothing at this address.", action=('Go to the download page', '/')), 404

    return app


_md = {}


def _md_html(name):
    """A Markdown file of this repository (GUIDE.md, EULA.md) as HTML, re-read when it changes."""
    path = os.path.join(ROOT, name)
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return '<p>This page is missing.</p>'
    if _md.get(name, (None,))[0] != mtime:
        import markdown
        with open(path, encoding='utf-8') as fh:
            text = fh.read()
        text = text.split('\n', 1)[1] if text.startswith('<p align="center">') else text   # the logo is in the page header
        if text.lstrip().startswith('# '):   # the page has its own title
            text = text.lstrip().split('\n', 1)[1]
        _md[name] = (mtime, markdown.markdown(text, extensions=['tables', 'sane_lists', 'toc', 'fenced_code']))
    return _md[name][1]
