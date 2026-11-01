"""/admin: update the live site from GitHub and see how Doppleganger is doing.

Two ways in (same as happyhacks.io):
  - a Doppleganger admin, signed in with Happy Hacks (ADMIN_CONTACTS, or: python -m dopplesite.users admin you@example.com)
  - ADMIN_PASSWORD from .env, kept as a backup (for example if Happy Hacks sign-in is down)
"""
import hmac
import os
import secrets
import time
from functools import wraps

from flask import Blueprint, g, jsonify, redirect, render_template, request, session, url_for

from . import hhauth, releases, selfupdate, settings, users

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
    return u if u and u.get('role') == 'admin' else None


def signed_in():
    return _password_session() or bool(_account_admin())


def _who():
    u = _account_admin()
    return (u['name'] or u['email'] or 'admin') if u else 'admin password'


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
    not_admin = (u['name'] or u['email'] or 'This account') if u and u.get('role') != 'admin' else None
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
    return render_template('admin.html', who=_who())


@bp.get('/admin/api/updates')
@login_required
def updates():
    fetch = request.args.get('fetch', '1') != '0'
    st = selfupdate.status(10 ** 12)
    st.pop('log', None)
    return jsonify({**selfupdate.version(fetch=fetch), **st})


@bp.post('/admin/api/updates')
@login_required
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
        'sso': hhauth.enabled(),
    })


@bp.after_request
def no_store(resp):
    if request.path.startswith('/admin'):
        resp.headers['Cache-Control'] = 'no-store'
        resp.headers['X-Robots-Tag'] = 'noindex'
    return resp
