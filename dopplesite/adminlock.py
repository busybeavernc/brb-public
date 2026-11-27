"""Hidden admin: /admin answers 404 until an admin or support person unlocks it.

How it opens:
  - Signed in with Happy Hacks as admin or support, on /account: type the unlock word (ADMIN_UNLOCK_KEYS, default
    "unlock") within 10 seconds of the page loading, or tap the page heading 5 times quickly. A code box appears;
    enter the 6-digit code from your authenticator app. Admin then stays open for this browser session for 30
    minutes after your last admin page or action, and never longer than 12 hours in a row.
  - Recovery only: with ADMIN_RECOVERY=1 *and* ADMIN_PASSWORD set in .env, /admin/login takes the password. Turn
    ADMIN_RECOVERY off again as soon as you're back in.

Authenticator codes are set up on the server, never in the browser (someone who takes over your email or phone
can't add their own authenticator):
    python -m dopplesite.users totp you@example.com          (prints the setup key and a QR link)
    python -m dopplesite.users totp-off you@example.com
Every unlock, failed code and recovery sign-in is in the audit log and emailed to SUPPORT_NOTIFY and the person.
"""
import base64
import hashlib
import hmac
import os
import secrets
import struct
import time
from urllib.parse import quote

from flask import g, request, session

from . import audit, mailer, settings, users

UNLOCK_MINUTES = 30
MAX_HOURS = 12
FAILS_ALLOWED = 5          # wrong codes per person (or recovery passwords per IP) ...
FAIL_WINDOW = 15 * 60      # ... in this many seconds
STEP = 30
ISSUER = 'BRB admin'


# ------------------------------------------------------------------ TOTP (RFC 6238, SHA-1, 6 digits, 30 s)

def new_secret():
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip('=')


def _key(secret):
    s = secret.upper().replace(' ', '')
    return base64.b32decode(s + '=' * (-len(s) % 8))


def code_at(secret, step):
    digest = hmac.new(_key(secret), struct.pack('>Q', step), hashlib.sha1).digest()
    off = digest[-1] & 0x0F
    return '%06d' % ((struct.unpack('>I', digest[off:off + 4])[0] & 0x7FFFFFFF) % 1000000)


def check_code(secret, code, last_step=0, now=None):
    """The time step the code belongs to (now, or 30 s either side), or None. A step at or before last_step is
    refused, so a code can't be used twice."""
    code = ''.join(ch for ch in str(code or '') if ch.isdigit())
    if not secret or len(code) != 6:
        return None
    step = int((now if now is not None else time.time()) // STEP)
    for s in (step, step - 1, step + 1):
        if s > (last_step or 0) and hmac.compare_digest(code_at(secret, s), code):
            return s
    return None


def setup_uri(secret, account):
    label = quote(f'{ISSUER}:{account}')
    return f'otpauth://totp/{label}?secret={secret}&issuer={quote(ISSUER)}&digits=6&period={STEP}'


# ------------------------------------------------------------------ settings

def recovery_on():
    return str(os.environ.get('ADMIN_RECOVERY', '')).strip().lower() in ('1', 'true', 'yes', 'on') \
        and bool(os.environ.get('ADMIN_PASSWORD', ''))


def unlock_keys():
    return (settings.get('ADMIN_UNLOCK_KEYS') or 'unlock').strip().lower()[:40] or 'unlock'


# ------------------------------------------------------------------ the unlock itself

def _staff(u=None):
    u = u if u is not None else g.get('user')
    return u if u and u.get('role') in ('admin', 'support') else None


def unlocked():
    """True while the signed-in staff member's unlock is live in this session."""
    u, lk = _staff(), session.get('admin_unlock')
    if not u or not isinstance(lk, dict) or lk.get('uid') != u['id']:
        return False
    now = time.time()
    return now < lk.get('until', 0) and now - lk.get('since', 0) < MAX_HOURS * 3600


def expired_for_staff():
    """A staff member whose unlock ran out (they get a 'locked' answer; everyone else gets a plain 404)."""
    return bool(_staff()) and not unlocked()


def touch():
    """Slide the 30 minutes along; note (and report) a change of network address."""
    lk = dict(session.get('admin_unlock') or {})
    lk['until'] = time.time() + UNLOCK_MINUTES * 60
    ip = request.remote_addr or ''
    if ip and lk.get('ip') and ip != lk['ip']:
        u = _staff()
        who = (u.get('email') or u.get('name') or u['id']) if u else '?'
        audit.log(who, 'admin_ip_changed', u['id'] if u else '', old=lk['ip'], new=ip)
        _alert(u, 'Admin is being used from a new network address',
               f'{who} unlocked admin from {lk["ip"]} and is now using it from {ip}.\n'
               'That is normal when a phone moves between Wi-Fi and mobile data. If it was not you, change your '
               'Happy Hacks sign-in and run: python -m dopplesite.users totp-off ' + str(who))
    lk['ip'] = ip or lk.get('ip', '')
    session['admin_unlock'] = lk


def lock():
    session.pop('admin_unlock', None)


def has_totp(u):
    row = users.db().execute('SELECT totp_secret FROM users WHERE id=?', (u['id'],)).fetchone()
    return bool(row and row['totp_secret'])


def _recent_failures(action, target):
    return audit.db().execute('SELECT COUNT(*) FROM audit WHERE action=? AND target=? AND at>?',
                              (action, target, time.time() - FAIL_WINDOW)).fetchone()[0]


def try_unlock(code):
    """Unlock for the signed-in staff member. Returns '' on success or what went wrong, in words."""
    u = _staff()
    if not u:
        return 'not_staff'
    who = u.get('email') or u.get('name') or u['id']
    ip = request.remote_addr or ''
    if _recent_failures('admin_unlock_failed', u['id']) >= FAILS_ALLOWED:
        return 'Too many wrong codes. Wait 15 minutes and try again.'
    con = users.db()
    row = con.execute('SELECT totp_secret, totp_last_step FROM users WHERE id=?', (u['id'],)).fetchone()
    if not row or not row['totp_secret']:
        return 'No authenticator is set up for your account yet. On the server, run: ' \
               f'python -m dopplesite.users totp {who}'
    step = check_code(row['totp_secret'], code, row['totp_last_step'] or 0)
    if step is None:
        audit.log(who, 'admin_unlock_failed', u['id'], ip=ip)
        if _recent_failures('admin_unlock_failed', u['id']) == FAILS_ALLOWED:
            _alert(u, 'Admin unlock blocked after wrong codes',
                   f'{FAILS_ALLOWED} wrong authenticator codes were entered for {who} (last from {ip}). Unlocking is '
                   'blocked for 15 minutes. If this was not you, someone has your Happy Hacks sign-in: change it.')
        return "That code didn't work. Codes change every 30 seconds; use the one showing now."
    con.execute('UPDATE users SET totp_last_step=? WHERE id=?', (step, u['id']))
    con.commit()
    now = time.time()
    session['admin_unlock'] = {'uid': u['id'], 'since': now, 'until': now + UNLOCK_MINUTES * 60, 'ip': ip}
    session['csrf'] = secrets.token_urlsafe(24)
    audit.log(who, 'admin_unlocked', u['id'], ip=ip)
    _alert(u, 'Admin was unlocked', f'{who} unlocked BRB admin from {ip} at {_when(now)}.\n'
                                    'If this was not you, sign out everywhere and turn off their authenticator: '
                                    f'python -m dopplesite.users totp-off {who}')
    return ''


def recovery_failures(ip):
    return _recent_failures('admin_password_failed', ip)


def note_recovery(ok, ip):
    if ok:
        audit.log('admin password', 'admin_recovery_sign_in', ip)
        _alert(None, 'Admin recovery password was used',
               f'Someone signed in to BRB admin with ADMIN_PASSWORD from {ip} at {_when(time.time())}.\n'
               'Recovery is on (ADMIN_RECOVERY=1 in .env). Turn it off again as soon as you are back in.')
    else:
        audit.log('admin password', 'admin_password_failed', ip)


def _when(t):
    return time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(t))


def _alert(u, subject, body):
    to = list(mailer.staff())
    if u and u.get('email') and u['email'] not in to:
        to.append(u['email'])
    mailer.send(to, f'[BRB] {subject}', body)


_ACTION_WORDS = {
    'set_role': 'changed a team role', 'update_website': 'started a website update', 'export': 'exported data',
    'bulk_delete_computers': 'deleted computers in bulk', 'delete_computer': 'deleted a computer',
    'pricing': 'changed pricing', 'create_key': 'created a license key', 'change_key': 'changed a license key',
    'totp_set_up': 'set up an admin authenticator', 'totp_removed': 'removed an admin authenticator',
}


def alert_action(who, action, target, details):
    """Called by audit.log for the actions in audit.ALERT_ACTIONS. Never raises."""
    try:
        lines = [f'{who} {_ACTION_WORDS.get(action, action)} at {_when(time.time())}.']
        if target:
            lines.append(f'Target: {target}')
        for k, v in (details or {}).items():
            lines.append(f'{k}: {v}')
        lines.append('\nFull history: Admin > Audit log.')
        mailer.send(mailer.staff(), f'[BRB admin] {who} {_ACTION_WORDS.get(action, action)}', '\n'.join(lines))
    except Exception:   # an alert must never break the action itself  # nosec B110
        pass
