"""Doppleganger licenses: free time per computer, license keys, and which computers use them.

The rules
  - Every computer gets FREE_SECONDS (default 1 hour) of LOOP time in total, without a key. Time counts only while
    the LOOP is on air. It's tracked here, per computer (device id), so reinstalling the app doesn't reset it.
  - A license key means unlimited LOOP time: a $5/month Square subscription, or a key an admin creates.
  - A key works on max_devices computers (default 1). Admins can move a key to a new computer or allow more.

How the app spends time (so it can't be faked on the computer itself)
  The app asks for a *lease* the moment it cuts to the LOOP and renews it while the LOOP is on air. Each lease is
  charged against the free time when it's granted, using this server's clock. When the app cuts back to LIVE it
  releases the lease, and the unused part is given back, again measured with this server's clock. So the free time
  left is always what this server says, no matter what's saved, edited or reinstalled on the computer.
"""
import base64
import hashlib
import hmac
import re
import secrets
import time
import uuid

from . import settings, users

LEASE_SECONDS = 30
KEY_ALPHABET = '0123456789ABCDEFGHJKMNPQRSTVWXYZ'   # Crockford base32: no I, L, O, U
DEVICE_ID = re.compile(r'^[0-9a-f]{64}$')

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
  id TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '', platform TEXT NOT NULL DEFAULT '', app_version TEXT NOT NULL DEFAULT '',
  used_seconds REAL NOT NULL DEFAULT 0, bonus_seconds REAL NOT NULL DEFAULT 0,
  first_seen REAL NOT NULL, last_seen REAL NOT NULL, last_ip TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '',
  discount_used_at REAL
);
CREATE TABLE IF NOT EXISTS licenses (
  id TEXT PRIMARY KEY, key TEXT UNIQUE NOT NULL, plan TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active',
  max_devices INTEGER NOT NULL DEFAULT 1, expires_at REAL, user_id TEXT, email TEXT NOT NULL DEFAULT '',
  note TEXT NOT NULL DEFAULT '', square_subscription_id TEXT UNIQUE, square_status TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL, created_by TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS license_devices (
  license_id TEXT NOT NULL, device_id TEXT NOT NULL, added_at REAL NOT NULL, last_seen REAL NOT NULL,
  PRIMARY KEY (license_id, device_id)
);
CREATE TABLE IF NOT EXISTS leases (
  id TEXT PRIMARY KEY, device_id TEXT NOT NULL, license_id TEXT, charged REAL NOT NULL DEFAULT 0,
  started_at REAL NOT NULL, until REAL NOT NULL, released_at REAL
);
CREATE INDEX IF NOT EXISTS leases_device ON leases (device_id, released_at);
"""


class LicenseError(Exception):
    def __init__(self, message, code='bad_request', status=400):
        super().__init__(message)
        self.code, self.status = code, status


_ready = set()


def db():
    con = users.db()
    if id(con) not in _ready:
        con.executescript(SCHEMA)
        if 'first_ip' not in [r[1] for r in con.execute('PRAGMA table_info(devices)')]:
            con.execute("ALTER TABLE devices ADD COLUMN first_ip TEXT NOT NULL DEFAULT ''")
            con.execute('UPDATE devices SET first_ip=last_ip')
            con.commit()
        _ready.add(id(con))
    return con


def free_seconds():
    try:
        return max(0, int(float(settings.get('FREE_SECONDS', 3600))))
    except ValueError:
        return 3600


# ------------------------------------------------------------------ keys
def new_key():
    raw = ''.join(secrets.choice(KEY_ALPHABET) for _ in range(16))
    return 'DG-' + '-'.join(raw[i:i + 4] for i in range(0, 16, 4))


def normalize_key(k):
    """What people type ('dg 4k7m-...', lower case, O for 0) -> the stored form 'DG-XXXX-XXXX-XXXX-XXXX'."""
    s = re.sub(r'[^0-9A-Za-z]', '', str(k or '')).upper()
    s = s.removeprefix('DG')
    s = s.translate(str.maketrans({'O': '0', 'I': '1', 'L': '1'}))
    if len(s) != 16 or any(c not in KEY_ALPHABET for c in s):
        return None
    return 'DG-' + '-'.join(s[i:i + 4] for i in range(0, 16, 4))


def create(plan='unlimited', max_devices=1, expires_at=None, email='', note='', user_id=None, by='',
           square_subscription_id=None, square_status=''):
    con = db()
    for _ in range(5):
        lid, key = uuid.uuid4().hex[:12], new_key()
        try:
            con.execute('INSERT INTO licenses (id, key, plan, max_devices, expires_at, user_id, email, note, '
                        'square_subscription_id, square_status, created_at, created_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                        (lid, key, plan, max(1, int(max_devices)), expires_at, user_id, (email or '').lower(), note,
                         square_subscription_id, square_status, time.time(), by))
            con.commit()
            return get(lid)
        except Exception as e:   # noqa: BLE001 - a key collision (80 bits: practically never): try another
            if 'UNIQUE' not in str(e) or 'square_subscription_id' in str(e):
                raise
    raise LicenseError('Could not make a new key. Try again.')


def get(lid):
    row = db().execute('SELECT * FROM licenses WHERE id=?', (lid,)).fetchone()
    return _with_devices(dict(row)) if row else None


def by_key(key):
    k = normalize_key(key)
    row = db().execute('SELECT * FROM licenses WHERE key=?', (k,)).fetchone() if k else None
    return dict(row) if row else None


def _with_devices(lic):
    lic['devices'] = [dict(r) for r in db().execute(
        'SELECT d.id, d.name, d.platform, d.app_version, ld.added_at, ld.last_seen FROM license_devices ld '
        'JOIN devices d ON d.id = ld.device_id WHERE ld.license_id=? ORDER BY ld.added_at', (lic['id'],))]
    lic['valid'], lic['why'] = valid(lic)
    return lic


def valid(lic):
    """(True, '') or (False, the reason in words)."""
    if lic['status'] == 'revoked':
        return False, 'This license key has been turned off.'
    if lic.get('expires_at') and lic['expires_at'] < time.time():
        return False, ('Your subscription has ended.' if lic['plan'] == 'subscription' else 'This license key has expired.')
    return True, ''


def for_user(user_id, email=None):
    rows = db().execute('SELECT id FROM licenses WHERE user_id=? OR (email<>\'\' AND email=?) ORDER BY created_at DESC',
                        (user_id, (email or '').lower() or '-')).fetchall()
    return [get(r['id']) for r in rows]


def update(lid, **changes):
    allowed = {'status', 'max_devices', 'expires_at', 'email', 'note', 'user_id', 'square_status', 'plan'}
    sets = {k: v for k, v in changes.items() if k in allowed}
    if not sets:
        return get(lid)
    if 'max_devices' in sets:
        sets['max_devices'] = max(1, min(50, int(sets['max_devices'])))
    if 'status' in sets and sets['status'] not in ('active', 'revoked'):
        raise LicenseError('Status must be active or revoked.')
    con = db()
    con.execute(f'UPDATE licenses SET {", ".join(k + "=?" for k in sets)} WHERE id=?', (*sets.values(), lid))
    con.commit()
    return get(lid)


def remove_device(lid, device_id):
    """Frees a key's slot so it can be used on another computer (e.g. a new laptop)."""
    con = db()
    n = con.execute('DELETE FROM license_devices WHERE license_id=? AND device_id=?', (lid, device_id)).rowcount
    con.commit()
    return n


def all_licenses(q='', limit=200):
    con = db()
    if q:
        like = f'%{q.strip().lower()}%'
        rows = con.execute('SELECT DISTINCT l.id FROM licenses l LEFT JOIN license_devices ld ON ld.license_id = l.id '
                           'LEFT JOIN devices d ON d.id = ld.device_id WHERE lower(l.key) LIKE ? OR lower(l.email) LIKE ? '
                           'OR lower(l.note) LIKE ? OR lower(d.name) LIKE ? OR d.id LIKE ? ORDER BY l.created_at DESC LIMIT ?',
                           (like, like, like, like, like, limit)).fetchall()
    else:
        rows = con.execute('SELECT id FROM licenses ORDER BY created_at DESC LIMIT ?', (limit,)).fetchall()
    return [get(r['id']) for r in rows]


# ------------------------------------------------------------------ computers and free time
def plausible_device_id(device_id):
    """A real device id is a SHA-256 in hex: 64 characters that use most of the 16 hex digits.
    Hand-made ones (all 'a', 'abab...') don't; the chance of a real one using fewer than 10 is negligible."""
    d = str(device_id or '')
    return bool(DEVICE_ID.match(d)) and len(set(d)) >= 10


def new_devices_per_ip_day():
    try:
        return int(settings.get('NEW_DEVICES_PER_IP_DAY', 5))
    except ValueError:
        return 5


def touch_device(device_id, name='', platform='', app_version='', ip='', create=True):
    """This computer's record, refreshed. create=False (a status check): a computer we haven't seen yet isn't
    recorded - it gets a full free allowance on paper, and is recorded the first time it puts the LOOP on air."""
    if not plausible_device_id(device_id):
        raise LicenseError('This copy of BRB sent a device id the server does not recognise. '
                           'Update BRB and try again.', 'bad_device')
    now = time.time()
    con = db()
    name, platform, app_version, ip = str(name)[:80], str(platform)[:20], str(app_version)[:20], str(ip)[:60]
    if con.execute('SELECT 1 FROM devices WHERE id=?', (device_id,)).fetchone():
        con.execute('UPDATE devices SET name=?, platform=?, app_version=?, last_seen=?, last_ip=? WHERE id=?',
                    (name, platform, app_version, now, ip, device_id))
    elif not create:
        return {'id': device_id, 'name': name, 'platform': platform, 'app_version': app_version, 'used_seconds': 0.0,
                'bonus_seconds': 0.0, 'first_seen': now, 'last_seen': now, 'last_ip': ip, 'first_ip': ip, 'note': '',
                'discount_used_at': None, 'new': True}
    else:
        # A brake on making up device ids for more free time: only so many new computers per internet connection
        # per day (an office shares one address, so it's a handful, not one).
        cap = new_devices_per_ip_day()
        if cap > 0 and ip and con.execute('SELECT COUNT(*) FROM devices WHERE first_ip=? AND first_seen > ?',
                                          (ip, now - 86400)).fetchone()[0] >= cap:
            raise LicenseError('Too many new computers have started BRB from this internet connection today. '
                               'Please try again tomorrow, or contact support from brb.happyhacks.io/license.',
                               'too_many_devices', 429)
        con.execute('INSERT INTO devices (id, name, platform, app_version, first_seen, last_seen, last_ip, first_ip) '
                    'VALUES (?,?,?,?,?,?,?,?)', (device_id, name, platform, app_version, now, now, ip, ip))
    con.commit()
    return dict(con.execute('SELECT * FROM devices WHERE id=?', (device_id,)).fetchone())


def delete_device(device_id):
    """Admin: forget a computer entirely (its free-time record, key assignments and lease history)."""
    con = db()
    n = con.execute('DELETE FROM devices WHERE id=?', (device_id,)).rowcount
    con.execute('DELETE FROM license_devices WHERE device_id=?', (device_id,))
    con.execute('DELETE FROM leases WHERE device_id=?', (device_id,))
    con.commit()
    return n


def free_left(device):
    return max(0.0, free_seconds() + float(device.get('bonus_seconds') or 0) - float(device.get('used_seconds') or 0))


def _bound_license(device_id):
    """A valid key this computer was put on (bought from it, or given by an admin)."""
    for r in db().execute('SELECT license_id FROM license_devices WHERE device_id=? ORDER BY added_at DESC', (device_id,)):
        lic = get(r['license_id'])
        if lic and lic['valid']:
            return lic
    return None


def _license_for(device_id, key):
    """The license this computer may use with this key, binding the computer to it if there's a free slot.
    -> (license or None, message for the person or '')."""
    if not key:
        # no key typed in yet: a key this computer was put on (bought from it, or given by an admin)
        return _bound_license(device_id), ''
    lic, problem = _license_for_key(device_id, key)
    if lic is None:
        # the typed key doesn't work here (mistyped, expired, used elsewhere), but this computer may have a key of its
        # own - e.g. just bought from it: that one wins, and the app swaps it in
        bound = _bound_license(device_id)
        if bound:
            return bound, ''
    return lic, problem


def _license_for_key(device_id, key):
    lic = by_key(key)
    if not lic:
        return None, "That license key isn't right. Check it and try again."
    lic = _with_devices(lic)
    if not lic['valid']:
        return None, lic['why']
    con = db()
    now = time.time()
    if any(d['id'] == device_id for d in lic['devices']):
        con.execute('UPDATE license_devices SET last_seen=? WHERE license_id=? AND device_id=?', (now, lic['id'], device_id))
        con.commit()
        return lic, ''
    if len(lic['devices']) >= lic['max_devices']:
        n = lic['max_devices']
        return None, (f"This license key is already in use on {'another computer' if n == 1 else f'{n} other computers'}. "
                      'To move it to this computer, contact support from brb.happyhacks.io/license.')
    con.execute('INSERT INTO license_devices (license_id, device_id, added_at, last_seen) VALUES (?,?,?,?)',
                (lic['id'], device_id, now, now))
    con.commit()
    return get(lic['id']), ''


def status(device, key=None):
    """What this computer may do right now (no time is charged)."""
    lic, problem = _license_for(device['id'], key)
    out = {'free_total': free_seconds(), 'server_time': time.time()}
    if lic:
        out.update(plan=lic['plan'], unlimited=True, key=lic['key'], key_problem='',
                   expires_at=lic.get('expires_at'), max_devices=lic['max_devices'], devices=len(lic['devices']))
    else:
        out.update(plan='free', unlimited=False, key=None, key_problem=problem)
    out['free_left'] = free_left(device)
    out['can_loop'] = out['unlimited'] or out['free_left'] > 0
    out['license_url'] = settings.public_url() + '/license'
    from . import square
    out['price'], out['early_price'] = square.price_text(), square.price_text(square.early_price_cents())
    # the words the app shows in its license dialog (written here, so prices and offers are never stale in the app)
    hours = free_seconds() / 3600
    free_words = (f'{hours:g} hour' + ('' if hours == 1 else 's')) if free_seconds() % 1800 == 0 else f'{free_seconds() // 60} minutes'
    out['pitch'] = (f'Every computer gets {free_words} of free LOOP time (it only counts while the LOOP is on air). '
                    f'Unlimited Access: unlimited LOOP time for {out["price"]}/month, cancel any time.')
    out['offer_text'] = ''
    if not lic:
        # The link carries a signed token for this computer: whatever is bought through it is put on this computer,
        # and the app picks it up by itself. While free time is left it also unlocks the early-bird price.
        out['license_url'] += '?d=' + offer_token(device['id'])
    if not lic and out['free_left'] > 0 and not device.get('discount_used_at'):
        out['offer'] = True
        out['offer_text'] = (f'Special offer: 50% off. Subscribe while you still have free time and pay '
                             f'{out["early_price"]}/month instead of {out["price"]}, for as long as you stay subscribed.')
    return out


def _refund_open_leases(device_id, now):
    """Give back the unused part of any lease still open (the app crashed, or this is a renewal)."""
    con = db()
    back = 0.0
    for r in con.execute('SELECT * FROM leases WHERE device_id=? AND released_at IS NULL', (device_id,)).fetchall():
        unused = max(0.0, min(r['charged'], r['until'] - now))
        back += unused
        con.execute('UPDATE leases SET released_at=? WHERE id=?', (now, r['id']))
    if back:
        con.execute('UPDATE devices SET used_seconds=MAX(0, used_seconds-?) WHERE id=?', (back, device_id))
    return back


def lease(device, key=None, action='start'):
    """action 'start' / 'renew': the LOOP is (going) on air -> {ok, lease_seconds, ...status}.
       action 'release': back to LIVE -> unused time is given back."""
    now = time.time()
    con = db()
    if action == 'release':
        _refund_open_leases(device['id'], now)
        con.commit()
        dev = dict(con.execute('SELECT * FROM devices WHERE id=?', (device['id'],)).fetchone())
        return {**status(dev, key), 'ok': True, 'lease_seconds': 0}
    if action not in ('start', 'renew'):
        raise LicenseError('Unknown action.')
    _refund_open_leases(device['id'], now)
    dev = dict(con.execute('SELECT * FROM devices WHERE id=?', (device['id'],)).fetchone())
    st = status(dev, key)
    if st['unlimited']:
        charge, secs = 0.0, LEASE_SECONDS
    else:
        secs = min(LEASE_SECONDS, st['free_left'])
        charge = secs
    if secs <= 0:
        con.commit()
        return {**st, 'ok': False, 'lease_seconds': 0,
                'message': 'Your free LOOP time is used up. Get a license key for unlimited use.'}
    lid = uuid.uuid4().hex[:16]
    con.execute('INSERT INTO leases (id, device_id, license_id, charged, started_at, until) VALUES (?,?,?,?,?,?)',
                (lid, dev['id'], None, charge, now, now + secs))
    if charge:
        con.execute('UPDATE devices SET used_seconds=used_seconds+? WHERE id=?', (charge, dev['id']))
    con.commit()
    dev = dict(con.execute('SELECT * FROM devices WHERE id=?', (dev['id'],)).fetchone())
    return {**status(dev, key), 'ok': True, 'lease_seconds': secs, 'lease_id': lid}


def devices(q='', limit=100):
    con = db()
    like = f'%{q.strip().lower()}%'
    rows = con.execute('SELECT * FROM devices WHERE ? = \'%%\' OR id LIKE ? OR lower(name) LIKE ? OR last_ip LIKE ? '
                       'ORDER BY last_seen DESC LIMIT ?', (like, like, like, like, limit)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d['free_left'] = free_left(d)
        d['keys'] = [x['key'] for x in con.execute(
            'SELECT l.key FROM license_devices ld JOIN licenses l ON l.id = ld.license_id WHERE ld.device_id=?', (d['id'],))]
        out.append(d)
    return out


def add_free_time(device_id, seconds):
    """Admin: give a computer more free time (negative takes it away); 'reset' = used back to 0."""
    con = db()
    if seconds == 'reset':
        n = con.execute('UPDATE devices SET used_seconds=0, bonus_seconds=0 WHERE id=?', (device_id,)).rowcount
    else:
        n = con.execute('UPDATE devices SET bonus_seconds=bonus_seconds+? WHERE id=?', (float(seconds), device_id)).rowcount
    con.commit()
    return n


# ------------------------------------------------------------------ early-bird offer (half price while free time is left)
OFFER_TTL = 3 * 86400


def _sig(payload):
    key = (settings.get('SECRET_KEY') or 'dev-only-change-me').encode()
    return base64.urlsafe_b64encode(hmac.new(key, b'offer:' + payload.encode(), hashlib.sha256).digest()[:18]).decode()


def offer_token(device_id):
    payload = f'{device_id}.{int(time.time())}'
    return f'{payload}.{_sig(payload)}'


def linked_device(token):
    """The computer a link from the app belongs to, if the link is genuine and recent (any computer, licensed or not)."""
    try:
        device_id, ts, sig = str(token or '').split('.')
        if not hmac.compare_digest(sig, _sig(f'{device_id}.{ts}')) or time.time() - int(ts) > OFFER_TTL:
            return None
    except ValueError:
        return None
    row = db().execute('SELECT * FROM devices WHERE id=?', (device_id,)).fetchone()
    if not row:   # signed by us, but not recorded yet (it hasn't used the LOOP): record it now
        try:
            row = touch_device(device_id)
        except LicenseError:
            return None
    return dict(row)


def offer_device(token):
    """The computer an offer link belongs to, if the link is genuine, recent, and that computer still qualifies."""
    try:
        device_id, ts, sig = str(token or '').split('.')
        if not hmac.compare_digest(sig, _sig(f'{device_id}.{ts}')) or time.time() - int(ts) > OFFER_TTL:
            return None
    except ValueError:
        return None
    row = db().execute('SELECT * FROM devices WHERE id=?', (device_id,)).fetchone()
    if not row:   # signed by us, but not recorded yet (it hasn't used the LOOP): record it now
        try:
            row = touch_device(device_id)
        except LicenseError:
            return None
    d = dict(row)
    if d.get('discount_used_at') or free_left(d) <= 0:
        return None
    if db().execute('SELECT 1 FROM license_devices ld JOIN licenses l ON l.id = ld.license_id '
                    "WHERE ld.device_id=? AND l.status='active' AND (l.expires_at IS NULL OR l.expires_at > ?)",
                    (device_id, time.time())).fetchone():
        return None   # already licensed
    return d


def bind_device(lid, device_id, discount=False):
    """Put a computer on a key (the one that bought it with the early-bird offer)."""
    con = db()
    now = time.time()
    con.execute('INSERT OR IGNORE INTO license_devices (license_id, device_id, added_at, last_seen) VALUES (?,?,?,?)',
                (lid, device_id, now, now))
    if discount:
        con.execute('UPDATE devices SET discount_used_at=? WHERE id=?', (now, device_id))
    con.commit()
