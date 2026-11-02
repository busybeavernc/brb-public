"""doppleganger.happyhacks.io: Doppleganger's website. Download page, setup guide, Sign in with Happy Hacks, /admin."""
import hmac
import logging
import os
import secrets
from datetime import timedelta

from flask import Flask, abort, g, redirect, render_template, request, send_from_directory, session, url_for
from werkzeug.middleware.proxy_fix import ProxyFix

from . import hhauth, releases, settings

log = logging.getLogger('dopplesite')
ROOT = settings.ROOT


def create_app() -> Flask:
    from . import users   # imported here so `python -m dopplesite.users` runs cleanly
    app = Flask(__name__, static_folder=os.path.join(ROOT, 'static'), static_url_path='/static')
    app.config['SECRET_KEY'] = settings.get('SECRET_KEY', 'dev-only-change-me')
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

    @app.before_request
    def load_user():
        g.user = users.get(session['uid']) if session.get('uid') else None
        if session.get('uid') and not g.user:
            session.pop('uid', None)

    @app.context_processor
    def inject():
        return {'asset': asset, 'user': g.get('user'), 'sso': hhauth.enabled(), 'csrf': csrf_token,
                'account_url': hhauth.account_url(), 'platforms': releases.PLATFORMS}

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
        from . import licenses, square
        mins = licenses.free_seconds() // 60
        free_label = (f'{mins // 60} hour' + ('s' if mins // 60 != 1 else '')) if mins % 60 == 0 and mins else f'{mins} minutes'
        return render_template('index.html', release=rel, available=releases.available(), free_label=free_label,
                               price=square.price_text(), early_price=square.price_text(square.early_price_cents()),
                               sizes={p: releases.size_mb(p) for p in releases.PLATFORMS},
                               need_signin=settings.flag('DOWNLOADS_REQUIRE_SIGNIN') and not g.user and hhauth.enabled())

    @app.route('/guide')
    def guide():
        return render_template('guide.html', guide_html=_guide_html())

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
        ok = hmac.compare_digest(request.form.get('csrf', ''), session.get('csrf', '') or '-')
        session.clear()
        if ok and hhauth.enabled() and request.form.get('everywhere'):
            return redirect(hhauth.signout_url(settings.public_url() + '/'), 303)
        return redirect(url_for('index'), 303)

    @app.route('/healthz')
    def healthz():
        return {'status': 'ok'}

    @app.errorhandler(404)
    def not_found(_e):
        return render_template('message.html', title='Page not found',
                               body="There's nothing at this address.", action=('Go to the download page', '/')), 404

    return app


_guide = {'mtime': None, 'html': ''}


def _guide_html():
    """GUIDE.md (the user guide, also shown on GitHub) as HTML, re-read when it changes."""
    path = os.path.join(ROOT, 'GUIDE.md')
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return '<p>The guide is missing.</p>'
    if mtime != _guide['mtime']:
        import markdown
        with open(path, encoding='utf-8') as fh:
            text = fh.read()
        text = text.split('\n', 1)[1] if text.startswith('<p align="center">') else text   # the logo is in the page header
        _guide.update(mtime=mtime, html=markdown.markdown(text, extensions=['tables', 'sane_lists', 'toc']))
    return _guide['html']
