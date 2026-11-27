"""Doppleganger licenses: free time per computer, license keys, and which computers use them.

The rules
  - Every computer gets FREE_SECONDS (default 30 minutes) of LOOP time in total, without a key. Time counts only while
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
import json
import time
import uuid

from . import settings, users

LEASE_SECONDS = 30   # default; LEASE_SECONDS in .env (15-300): longer = fewer requests from computers on air


def lease_seconds():
    try:
        return max(15, min(300, int(float(settings.get('LEASE_SECONDS', LEASE_SECONDS)))))
    except ValueError:
        return LEASE_SECONDS


def check_every():
    """How often an idle app re-checks its license (STATUS_SECONDS, 60-3600, default 300)."""
    try:
        return max(60, min(3600, int(float(settings.get('STATUS_SECONDS', 300)))))
    except ValueError:
        return 300
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


INDEXES = """
CREATE INDEX IF NOT EXISTS devices_last_seen ON devices (last_seen);
CREATE INDEX IF NOT EXISTS devices_user ON devices (user_id);
CREATE INDEX IF NOT EXISTS devices_first_ip ON devices (first_ip, first_seen);
CREATE INDEX IF NOT EXISTS licenses_email ON licenses (email);
CREATE INDEX IF NOT EXISTS licenses_user ON licenses (user_id);
CREATE INDEX IF NOT EXISTS license_devices_device ON license_devices (device_id);
"""
SELF_MOVES_PER_MONTH = 3   # how often a customer may move their own key to another computer (stops key sharing)


class LicenseError(Exception):
    def __init__(self, message, code='bad_request', status=400):
        super().__init__(message)
        self.code, self.status = code, status


_ready = set()


def db():
    con = users.db()
    key = (id(con), users.path())   # a connection's id can be reused by a new one (e.g. a new database in tests)
    if key not in _ready:
        con.executescript(SCHEMA)
        cols = [r[1] for r in con.execute('PRAGMA table_info(devices)')]
        if 'first_ip' not in cols:
            con.execute("ALTER TABLE devices ADD COLUMN first_ip TEXT NOT NULL DEFAULT ''")
            con.execute('UPDATE devices SET first_ip=last_ip')
            con.commit()
        if 'user_id' not in cols:   # the account this computer is linked to (from BRB's "Link to my account")
            con.execute('ALTER TABLE devices ADD COLUMN user_id TEXT')
            con.commit()
        if 'vm' not in cols:   # BRB says it runs in a virtual machine: free time there needs an account
            con.execute('ALTER TABLE devices ADD COLUMN vm INTEGER NOT NULL DEFAULT 0')
            con.commit()
        lcols = [r[1] for r in con.execute('PRAGMA table_info(licenses)')]
        if 'renews_at' not in lcols:   # a subscription's next renewal (paid up to then), from Square
            con.execute('ALTER TABLE licenses ADD COLUMN renews_at REAL')
            con.commit()
        if 'payment_failed_at' not in lcols:   # a renewal that didn't go through: since when, where to pay, what we've said
            con.executescript('ALTER TABLE licenses ADD COLUMN payment_failed_at REAL; '
                              'ALTER TABLE licenses ADD COLUMN pay_url TEXT; ALTER TABLE licenses ADD COLUMN payment_notice TEXT;')
            con.commit()
        con.executescript(INDEXES)
        _ready.add(key)
    return con


def free_seconds():
    try:
        return max(0, int(float(settings.get('FREE_SECONDS', 1800))))
    except ValueError:
        return 1800


def grace_seconds():
    """On air when the free time runs out: the app keeps the LOOP on air this long (GRACE_SECONDS, default 20 min)."""
    try:
        return max(0, min(4 * 3600, int(float(settings.get('GRACE_SECONDS', 1200)))))
    except ValueError:
        return 1200


def _words(template, **values):
    """A .env text with {placeholders}; a typo in it falls back to the built-in words rather than breaking the API."""
    try:
        return template.format(**values)
    except (KeyError, IndexError, ValueError):
        return None


def free_words(seconds=None):
    """1800 -> '30 minutes', 3600 -> '1 hour', 5400 -> '90 minutes', 7200 -> '2 hours'."""
    s = free_seconds() if seconds is None else int(seconds)
    if s >= 3600 and s % 3600 == 0:
        h = s // 3600
        return f'{h} hour' + ('' if h == 1 else 's')
    m = s // 60
    return f'{m} minute' + ('' if m == 1 else 's')


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


PAYMENT_PAUSED = ("Your subscription's last payment didn't go through, so unlimited LOOP time is paused. Pay it at "
                  'brb.happyhacks.io/account and it switches back on by itself.')


def valid(lic):
    """(True, '') or (False, the reason in words)."""
    if lic['status'] == 'revoked':
        return False, 'This license key has been turned off.'
    if lic.get('expires_at') and lic['expires_at'] < time.time():
        return False, ('Your subscription has ended.' if lic['plan'] == 'subscription' else 'This license key has expired.')
    if lic.get('payment_failed_at'):
        from . import square
        if time.time() - lic['payment_failed_at'] > square.grace_days() * 86400:
            return False, PAYMENT_PAUSED
    return True, ''


def for_user(user_id, email=None):
    rows = db().execute('SELECT id FROM licenses WHERE user_id=? OR (email<>\'\' AND email=?) ORDER BY created_at DESC',
                        (user_id, (email or '').lower() or '-')).fetchall()
    return [get(r['id']) for r in rows]


def update(lid, **changes):
    allowed = {'status', 'max_devices', 'expires_at', 'email', 'note', 'user_id', 'square_status', 'plan', 'renews_at',
               'payment_failed_at', 'pay_url', 'payment_notice'}
    sets = {k: v for k, v in changes.items() if k in allowed}
    if not sets:
        return get(lid)
    if 'max_devices' in sets:
        sets['max_devices'] = max(1, min(50, int(sets['max_devices'])))
    if 'status' in sets and sets['status'] not in ('active', 'revoked'):
        raise LicenseError('Status must be active or revoked.')
    con = db()
    # column names come from the `allowed` set above, never from the request; the values are bound parameters
    con.execute(f'UPDATE licenses SET {", ".join(k + "=?" for k in sets)} WHERE id=?', (*sets.values(), lid))  # nosec B608
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
        return int(settings.get('NEW_DEVICES_PER_IP_DAY', 3))
    except ValueError:
        return 3


def touch_device(device_id, name='', platform='', app_version='', ip='', create=True, vm=False):
    """This computer's record, refreshed. create=False (a status check): a computer we haven't seen yet isn't
    recorded - it gets a full free allowance on paper, and is recorded the first time it puts the LOOP on air."""
    if not plausible_device_id(device_id):
        raise LicenseError('This copy of BRB sent a device id the server does not recognise. '
                           'Update BRB and try again.', 'bad_device')
    now = time.time()
    con = db()
    name, platform, app_version, ip = str(name)[:80], str(platform)[:20], str(app_version)[:20], str(ip)[:60]
    vm = 1 if vm else 0
    if con.execute('SELECT 1 FROM devices WHERE id=?', (device_id,)).fetchone():
        con.execute('UPDATE devices SET name=?, platform=?, app_version=?, last_seen=?, last_ip=?, vm=MAX(vm, ?) WHERE id=?',
                    (name, platform, app_version, now, ip, vm, device_id))
    elif not create:
        return {'id': device_id, 'name': name, 'platform': platform, 'app_version': app_version, 'used_seconds': 0.0,
                'bonus_seconds': 0.0, 'first_seen': now, 'last_seen': now, 'last_ip': ip, 'first_ip': ip, 'note': '',
                'discount_used_at': None, 'new': True, 'vm': vm, 'user_id': None}
    else:
        # A brake on making up device ids for more free time: only so many new computers per internet connection
        # per day (an office shares one address, so it's a handful, not one).
        cap = new_devices_per_ip_day()
        if cap > 0 and ip and con.execute('SELECT COUNT(*) FROM devices WHERE first_ip=? AND first_seen > ?',
                                          (ip, now - 86400)).fetchone()[0] >= cap:
            raise LicenseError('Too many new computers have started BRB from this internet connection today. '
                               'Please try again tomorrow, or contact support from brb.happyhacks.io/license.',
                               'too_many_devices', 429)
        con.execute('INSERT INTO devices (id, name, platform, app_version, first_seen, last_seen, last_ip, first_ip, vm) '
                    'VALUES (?,?,?,?,?,?,?,?,?)', (device_id, name, platform, app_version, now, now, ip, ip, vm))
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


VM_NOTICE = ('BRB is running in a virtual machine. Free LOOP time in virtual machines needs a BRB account (one free '
             'allowance per account): in BRB, open Settings > License and click "Link this computer to my account", then '
             'sign in.')


def free_left(device):
    """Free time this computer has left. In a virtual machine (each one looks like a new computer), free time needs
    an account, and all of an account's virtual machines share one allowance."""
    bonus, used = float(device.get('bonus_seconds') or 0), float(device.get('used_seconds') or 0)
    if device.get('vm'):
        if not device.get('user_id'):
            return 0.0
        row = db().execute('SELECT COALESCE(SUM(used_seconds), 0) AS u FROM devices WHERE user_id=? AND vm=1 AND id<>?',
                           (device['user_id'], device['id'])).fetchone()
        used += float(row['u'] or 0)
    return max(0.0, free_seconds() + bonus - used)


def needs_account(device):
    return bool(device.get('vm')) and not device.get('user_id')


def _paused_here(device_id):
    """A key on this computer that's paused for a failed payment: the words to show, else ''."""
    for r in db().execute('SELECT license_id FROM license_devices WHERE device_id=?', (device_id,)):
        lic = get(r['license_id'])
        if lic and not lic['valid'] and lic.get('payment_failed_at') and not lic.get('expires_at'):
            return lic['why']
    return ''


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
        bound = _bound_license(device_id)
        return bound, ('' if bound else _paused_here(device_id))
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


OFFLINE_PASS_PREFIX = b'BRB-OFFLINE-PASS\n'   # signed passes can never be mistaken for a signed reply, or the reverse


def offline_days():
    try:
        return max(0.0, min(60.0, float(settings.get('OFFLINE_DAYS', 14))))
    except ValueError:
        return 14.0


def offline_pass(device_id, lic):
    """A licensed computer's pass for when this site can't be reached: signed, for this computer only, valid for
    OFFLINE_DAYS (default 14) - or until the license ends, if sooner. Renewed with every status/lease reply, so a
    cancelled or removed license simply stops being renewed. None when OFFLINE_DAYS is 0."""
    days = offline_days()
    if not days:
        return None
    from . import signing
    now = time.time()
    until = now + days * 86400
    if lic.get('expires_at'):
        until = min(until, float(lic['expires_at']))
    body = json.dumps({'device_id': device_id, 'plan': lic['plan'], 'issued': int(now), 'until': int(until)},
                      separators=(',', ':'), sort_keys=True)
    return {'body': body, 'sig': signing.sign(OFFLINE_PASS_PREFIX + body.encode())}


def status(device, key=None):
    """What this computer may do right now (no time is charged)."""
    lic, problem = _license_for(device['id'], key)
    out = {'free_total': free_seconds(), 'grace_seconds': grace_seconds(), 'check_every': check_every(),
           'server_time': time.time()}
    if lic:
        out.update(plan=lic['plan'], unlimited=True, key=lic['key'], key_problem='',
                   expires_at=lic.get('expires_at'), max_devices=lic['max_devices'], devices=len(lic['devices']))
        out['offline_pass'] = offline_pass(device['id'], lic)
    else:
        out.update(plan='free', unlimited=False, key=None, key_problem=problem)
    out['free_left'] = free_left(device)
    if not lic and needs_account(device):
        out['need_account'] = True
        out['notice'] = VM_NOTICE
    out['can_loop'] = out['unlimited'] or out['free_left'] > 0
    out['license_url'] = settings.public_url() + '/license'
    # "Link this computer to my account" (Settings > License): signs in, then shows the computer on /account
    out['link_url'] = settings.public_url() + '/account?d=' + offer_token(device['id'])
    from . import square
    pr = square.pricing()
    out['price'], out['early_price'] = pr['monthly'], pr['from']
    # the words the app shows in its license dialog (written here, so prices and offers are never stale in the app).
    # PITCH_TEXT / OFFER_TEXT in .env replace them, with {free} {monthly} {annual_month} {annual_total} {promo_month}
    # {promo_total} {promo_name} {promo_ends} (and the older {price} {early_price} {percent_off})
    pct = round(100 * (1 - square.promo_cents() / square.annual_cents())) if square.annual_cents() else 0
    words = dict(free=free_words(), price=pr['monthly'], early_price=pr['from'], percent_off=pct, monthly=pr['monthly'],
                 annual_month=pr['annual_month'], annual_total=pr['annual_total'], promo_month=pr['promo_month'],
                 promo_total=pr['promo_total'], promo_name=pr['promo_name'], promo_ends=pr['promo_ends'])
    out['pitch'] = _words(settings.get('PITCH_TEXT', ''), **words) or (
        f'Every computer gets {free_words()} of free LOOP time (it only counts while the LOOP is on air). '
        f'Unlimited Access: unlimited LOOP time for {pr["annual_month"]}/month billed yearly ({pr["annual_total"]}), '
        f'or {pr["monthly"]} month to month. Renews automatically; cancel any time.')
    out['offer_text'] = ''
    if not lic:
        # The link carries a signed token for this computer: whatever is bought through it is put on this computer,
        # and the app picks it up by itself.
        out['license_url'] += '?d=' + offer_token(device['id'])
    if not lic and pr['promo']:
        out['offer'] = True
        out['offer_text'] = _words(settings.get('OFFER_TEXT', ''), **words) or (
            f'{pr["promo_name"]}: {pr["promo_month"]}/month for your first year on the annual plan '
            f'({pr["promo_total"]}, then {pr["annual_total"]}/year) - order by {pr["promo_ends"]}.')
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
        charge, secs = 0.0, lease_seconds()
    else:
        secs = min(lease_seconds(), st['free_left'])
        charge = secs
    if secs <= 0:
        con.commit()
        return {**st, 'ok': False, 'lease_seconds': 0,
                'message': VM_NOTICE if st.get('need_account') else
                'Your free LOOP time is used up. Get a license key for unlimited use.'}
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


# ------------------------------------------------------------------ accounts: linking computers, self-service
def link_device(device_id, user_id):
    con = db()
    n = con.execute('UPDATE devices SET user_id=? WHERE id=?', (user_id, device_id)).rowcount
    con.commit()
    return n


def unlink_device(device_id, user_id):
    con = db()
    n = con.execute('UPDATE devices SET user_id=NULL WHERE id=? AND user_id=?', (device_id, user_id)).rowcount
    con.commit()
    return n


def user_devices(user_id, email=None):
    """The computers that belong to an account: linked to it, or using one of its keys."""
    lic_ids = [x['id'] for x in for_user(user_id, email)]
    con = db()
    ids = {r['id'] for r in con.execute('SELECT id FROM devices WHERE user_id=?', (user_id,))}
    if lic_ids:
        ids |= {r['device_id'] for r in con.execute(
            'SELECT device_id FROM license_devices WHERE license_id IN (%s)' % ','.join('?' * len(lic_ids)),  # nosec B608
            lic_ids)}
    out = []
    for did in ids:
        row = con.execute('SELECT * FROM devices WHERE id=?', (did,)).fetchone()
        if not row:
            continue
        d = dict(row)
        d['free_left'] = free_left(d)
        d['linked'] = d.get('user_id') == user_id
        d['keys'] = []
        for r in con.execute('SELECT l.* FROM license_devices ld JOIN licenses l ON l.id = ld.license_id '
                             'WHERE ld.device_id=?', (did,)):
            lic = dict(r)
            ok, _ = valid(lic)
            d['keys'].append({'id': lic['id'], 'key': lic['key'], 'valid': ok})
        out.append(d)
    return sorted(out, key=lambda d: -d['last_seen'])


def self_moves_left(lid):
    from . import audit
    since = time.time() - 30 * 86400
    n = audit.db().execute("SELECT COUNT(*) FROM audit WHERE action='self_remove_device' AND target=? AND at > ?",
                           (lid, since)).fetchone()[0]
    return max(0, SELF_MOVES_PER_MONTH - n)


def self_remove_device(user, lid, device_id):
    """A customer frees their own key from one of their computers (to use it on a new one)."""
    from . import audit
    lic = get(lid)
    owned = lic and (lic.get('user_id') == user['id'] or (user.get('email') and lic.get('email') == user['email']))
    if not owned:
        raise LicenseError('That key is not on your account.', 'not_yours', 403)
    if device_id not in [d['id'] for d in lic['devices']]:
        raise LicenseError('That computer is not using this key.', 'not_on_key', 404)
    if self_moves_left(lid) <= 0:
        raise LicenseError(f'A key can be moved {SELF_MOVES_PER_MONTH} times a month. Please contact support '
                           'and we will move it for you.', 'too_many_moves', 429)
    remove_device(lid, device_id)
    audit.log(user.get('email') or user['id'], 'self_remove_device', lid, device=device_id)
