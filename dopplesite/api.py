"""The license API the Doppleganger app talks to, the /license page, and Square's webhook.

App API (JSON, POST):
  /api/v1/license/status   {device_id, device_name, platform, app_version, key}  -> what this computer may do
  /api/v1/license/lease    {... , action: start|renew|release}                    -> permission to keep the LOOP on air
"""
import hmac
import json
import logging
import time

from flask import Blueprint, g, jsonify, redirect, render_template, request, session, url_for

from . import hhauth, licenses, settings, square

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
                                request.remote_addr or '', create=create)
    return d, dev


@bp.errorhandler(licenses.LicenseError)
def license_error(e):
    return jsonify(error=str(e), code=e.code), e.status


@bp.post('/api/v1/license/status')
def license_status():
    if _too_many(request.remote_addr or '?'):
        return jsonify(error='Too many requests. Try again in a minute.', code='slow_down'), 429
    d = request.get_json(silent=True) or {}
    d, dev = _device(create=bool(d.get('key')))   # typing in a key records the computer (it's put on the key)
    return jsonify(licenses.status(dev, d.get('key')))


@bp.post('/api/v1/license/lease')
def license_lease():
    if _too_many(request.remote_addr or '?'):
        return jsonify(error='Too many requests. Try again in a minute.', code='slow_down'), 429
    d = request.get_json(silent=True) or {}
    action = str(d.get('action') or 'start')
    d, dev = _device(create=action != 'release')
    if dev.get('new'):   # releasing a lease it never had: nothing to do
        return jsonify({**licenses.status(dev, d.get('key')), 'ok': True, 'lease_seconds': 0})
    return jsonify(licenses.lease(dev, d.get('key'), action))


# ------------------------------------------------------------------ /license (people)
def _csrf_ok():
    return hmac.compare_digest(request.form.get('csrf', ''), session.get('csrf', '') or '-')


def _offer_device():
    """The computer whose early-bird link brought this visitor here (kept in the session through sign-in)."""
    if request.args.get('offer'):
        session['offer'] = request.args['offer']
    return licenses.offer_device(session.get('offer')) if session.get('offer') else None


@bp.get('/license')
def license_page():
    keys = licenses.for_user(g.user['id'], g.user.get('email')) if g.user else []
    dev = _offer_device()
    if request.args.get('offer'):   # tidy URL (the offer is in the session now)
        return redirect(url_for('api.license_page', n=request.args.get('n')), 303)
    return render_template('license.html', keys=keys, free_minutes=licenses.free_seconds() // 60,
                           price=square.price_text(), early_price=square.price_text(square.early_price_cents()),
                           offer=dev, square_ready=square.ready(), sso=hhauth.enabled(), notice=request.args.get('n'))


@bp.post('/license/subscribe')
def subscribe():
    if not g.user:
        return redirect(url_for('hh_start', next='/license'), 303)
    if not _csrf_ok():
        return redirect(url_for('api.license_page', n='expired'), 303)
    try:
        early = _offer_device() if request.form.get('early') else None
        url = square.checkout(g.user, settings.public_url() + url_for('api.subscribe_return'), early_device=early)
    except square.SquareError as e:
        return render_template('message.html', title="Couldn't open checkout", body=str(e),
                               action=('Back to your license', url_for('api.license_page'))), e.status
    return redirect(url, 303)


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
