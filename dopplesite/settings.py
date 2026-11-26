"""Settings from the environment (.env is loaded by wsgi.py). Real environment variables win - except the few that can
be changed in Admin (EDITABLE): a value saved there wins over .env, and clearing it there falls back to .env."""
import os
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# Changeable in Admin > Overview > Pricing (stored in the database, table site_settings)
EDITABLE = ('MONTHLY_PRICE_CENTS', 'ANNUAL_PRICE_CENTS', 'PROMO_ANNUAL_PRICE_CENTS', 'PROMO_ENDS', 'PROMO_NAME',
            'PITCH_TEXT', 'OFFER_TEXT', 'RETENTION_PCT', 'TEST_PRICES')
_saved = {'at': 0.0, 'values': {}, 'db': None}
_saved_lock = threading.Lock()
SAVED_TTL = 5.0   # other web workers see a change within this many seconds


def saved():
    """Values saved in Admin (cached briefly)."""
    now = time.monotonic()
    try:
        from . import users
        where = users.path()
    except Exception:  # noqa: BLE001
        where = None
    if now - _saved['at'] < SAVED_TTL and _saved['db'] == where:
        return _saved['values']
    with _saved_lock:
        try:
            con = users.db()
            con.execute('CREATE TABLE IF NOT EXISTS site_settings (name TEXT PRIMARY KEY, value TEXT NOT NULL, '
                        "updated_at REAL NOT NULL, updated_by TEXT NOT NULL DEFAULT '')")
            values = {r['name']: r['value'] for r in con.execute('SELECT name, value FROM site_settings')}
        except Exception:  # noqa: BLE001 - no database yet: just .env
            values = {}
        _saved.update(at=now, values=values, db=where)
    return values


def forget_saved():
    _saved['at'] = 0.0


def save(values, by=''):
    """Save Admin's values (name -> text; '' or None removes it, so .env applies again)."""
    from . import users
    saved()
    con = users.db()
    for name, value in values.items():
        if name not in EDITABLE:
            raise KeyError(name)
        if value in (None, ''):
            con.execute('DELETE FROM site_settings WHERE name=?', (name,))
        else:
            con.execute('INSERT OR REPLACE INTO site_settings (name, value, updated_at, updated_by) VALUES (?,?,?,?)',
                        (name, str(value), time.time(), by))
    con.commit()
    forget_saved()


def get(name, default=None):
    if name in EDITABLE:
        v = saved().get(name)
        if v not in (None, ''):
            return v
    v = os.environ.get(name)
    return v if v not in (None, '') else default


def flag(name, default=False):
    v = get(name)
    return default if v is None else str(v).strip().lower() in ('1', 'true', 'yes', 'on')


def data_dir():
    d = get('DATA_DIR') or os.path.join(ROOT, 'data')
    os.makedirs(d, exist_ok=True)
    return d


def public_url():
    return (get('PUBLIC_URL') or 'https://brb.happyhacks.io').rstrip('/')


def releases_repo():
    """The public GitHub repository the app's releases are published to."""
    repo = get('RELEASES_REPO') or 'busybeavernc/brb-public'
    return 'busybeavernc/brb-public' if repo == 'busybeavernc/doppleganger-public' else repo   # renamed on GitHub


def contacts(name):
    """Comma-separated emails / mobiles from a setting, normalised for comparing."""
    return {norm_contact(c) for c in str(get(name, '')).split(',') if c.strip()}


def norm_contact(c):
    c = str(c or '').strip().lower()
    if '@' in c:
        return c
    digits = ''.join(ch for ch in c if ch.isdigit())
    if len(digits) == 10:  # a US number written without the country code
        digits = '1' + digits
    return '+' + digits if digits else ''
