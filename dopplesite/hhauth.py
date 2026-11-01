"""Sign in with Happy Hacks: one account for every Happy Hacks app (same flow as zipzip's autofill/hhauth.py).

Turned on by three settings in .env, printed by this command on the Happy Hacks server:
    python -m happyhacks.sso add doppleganger "Doppleganger" https://doppleganger.happyhacks.io/auth/happyhacks/callback
  HAPPYHACKS_URL=https://happyhacks.io   HAPPYHACKS_CLIENT_ID=doppleganger   HAPPYHACKS_CLIENT_SECRET=...

The flow (authorization code + PKCE):
  /auth/happyhacks            -> happyhacks.io/account/authorize?... (state + PKCE verifier kept in a short-lived signed cookie)
  /auth/happyhacks/callback   <- ?code&state; our server trades the code at /account/token for the person's details,
                                 finds or creates the Doppleganger user and starts a Doppleganger session.
Happy Hacks owns who you are (name, email, mobile, sign-in codes); Doppleganger keeps its own session and admin role.
"""
import base64
import hashlib
import hmac
import json
import logging
import re
import secrets
import time
import urllib.error
import urllib.request
from urllib.parse import urlencode

from . import settings

log = logging.getLogger('dopplesite')
COOKIE = 'dg_hh'
COOKIE_TTL = 900


class HHError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def enabled():
    return bool(settings.get('HAPPYHACKS_URL') and settings.get('HAPPYHACKS_CLIENT_ID')
                and settings.get('HAPPYHACKS_CLIENT_SECRET'))


def hh_url():
    return str(settings.get('HAPPYHACKS_URL') or 'https://happyhacks.io').rstrip('/')


def account_url():
    return hh_url() + '/account'


def signout_url(return_to):
    return hh_url() + '/account/signout?' + urlencode({'return_to': return_to})


def _key():
    return (settings.get('SECRET_KEY') or 'dev-only-change-me').encode()


def _sign(payload):
    raw = base64.urlsafe_b64encode(json.dumps(payload, separators=(',', ':')).encode()).decode().rstrip('=')
    return f'{raw}.{hmac.new(_key(), b"hh:" + raw.encode(), hashlib.sha256).hexdigest()}'


def _unsign(value):
    try:
        raw, mac = str(value or '').rsplit('.', 1)
        if not hmac.compare_digest(mac, hmac.new(_key(), b'hh:' + raw.encode(), hashlib.sha256).hexdigest()):
            return None
        data = json.loads(base64.urlsafe_b64decode(raw + '=' * (-len(raw) % 4)))
    except (ValueError, TypeError):
        return None
    return data if time.time() - data.get('t', 0) < COOKIE_TTL else None


def safe_next(n):
    """Where to land after signing in: a page of this site (a path), never another site."""
    n = str(n or '')
    return n if re.fullmatch(r'/(?!/)[A-Za-z0-9/_?=&.%#-]{0,300}', n) else '/'


def begin(redirect_uri, next_path='/', signup=False):
    """-> (url to send the browser to, value for the short-lived signed cookie)"""
    state, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
    q = {'client_id': settings.get('HAPPYHACKS_CLIENT_ID'), 'redirect_uri': redirect_uri, 'response_type': 'code',
         'state': state, 'code_challenge': challenge, 'code_challenge_method': 'S256'}
    if signup:
        q['screen_hint'] = 'signup'
    return f'{hh_url()}/account/authorize?{urlencode(q)}', _sign({'s': state, 'v': verifier, 'n': safe_next(next_path),
                                                                   't': time.time()})


def _post(url, body):
    req = urllib.request.Request(url, data=body.encode(), method='POST', headers={
        'Content-Type': 'application/x-www-form-urlencoded', 'Accept': 'application/json', 'User-Agent': 'doppleganger-site'})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def finish(args, cookie, redirect_uri):
    """Check the callback and trade the code for the Happy Hacks user. -> (hh_user, next_path)"""
    st = _unsign(cookie)
    if args.get('error'):
        raise HHError('Signing in with Happy Hacks was cancelled.')
    if not st or not args.get('state') or not hmac.compare_digest(str(args.get('state')), st['s']):
        raise HHError('That sign-in took too long or was started in another browser. Please try again.')
    code = args.get('code') or ''
    if not re.fullmatch(r'[A-Za-z0-9_-]{20,100}', code):
        raise HHError("Happy Hacks didn't send a sign-in code back. Please try again.")
    body = urlencode({'grant_type': 'authorization_code', 'client_id': settings.get('HAPPYHACKS_CLIENT_ID'),
                      'client_secret': settings.get('HAPPYHACKS_CLIENT_SECRET'), 'code': code,
                      'redirect_uri': redirect_uri, 'code_verifier': st['v']})
    try:
        status, content = _post(hh_url() + '/account/token', body)
    except (urllib.error.URLError, OSError) as e:
        log.error('Happy Hacks token exchange failed: %s', e)
        raise HHError("Happy Hacks couldn't be reached to finish signing in. Try again in a moment.", 502)
    try:
        data = json.loads(content or b'{}')
    except ValueError:
        data = {}
    if status != 200 or not isinstance(data.get('user'), dict) or not data['user'].get('id'):
        log.error('Happy Hacks token exchange refused: HTTP %s %s', status, (content or b'')[:300])
        raise HHError("Happy Hacks didn't accept this sign-in. Please try again.", 502)
    return data['user'], st['n']
