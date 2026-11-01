"""Doppleganger's own users and download counts (a small SQLite file in DATA_DIR).

Who someone is comes from their Happy Hacks account (see hhauth.py); this table only links that account to
Doppleganger and holds what is Doppleganger's own: the admin role and when they were last here.

Admins: emails / mobiles in ADMIN_CONTACTS become admins when they next sign in (confirmed contacts only), or
    python -m dopplesite.users admin you@example.com      (and: unadmin, list)
"""
import os
import sqlite3
import sys
import threading
import time
import uuid

from . import settings

_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY, hh_id TEXT UNIQUE NOT NULL, name TEXT NOT NULL DEFAULT '', email TEXT, phone TEXT,
  role TEXT NOT NULL DEFAULT 'user', created_at REAL NOT NULL, last_login_at REAL
);
CREATE TABLE IF NOT EXISTS downloads (
  day TEXT NOT NULL, platform TEXT NOT NULL, version TEXT NOT NULL DEFAULT '', count INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (day, platform, version)
);
"""


def path():
    return os.path.join(settings.data_dir(), 'doppleganger.sqlite')


def db():
    p = path()
    con = getattr(_local, 'con', None)
    if con is None or getattr(_local, 'path', None) != p:
        con = sqlite3.connect(p, timeout=10)
        con.row_factory = sqlite3.Row
        con.execute('PRAGMA journal_mode=WAL')
        con.executescript(SCHEMA)
        _local.con, _local.path = con, p
    return con


def reset():
    con = getattr(_local, 'con', None)
    if con is not None:
        con.close()
    _local.con = _local.path = None


def get(uid):
    row = db().execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone()
    return dict(row) if row else None


def _is_admin_contact(email, phone):
    admins = settings.contacts('ADMIN_CONTACTS')
    return bool(admins) and bool({settings.norm_contact(email), settings.norm_contact(phone)} & (admins - {''}))


def sign_in(hh):
    """The Doppleganger user for this Happy Hacks account (created on first visit), with its details refreshed."""
    hid = str(hh['id'])
    email = str(hh['email']).lower() if hh.get('email') and hh.get('email_verified') else None
    phone = str(hh['phone']) if hh.get('phone') and hh.get('phone_verified') else None
    name = str(hh.get('name') or '').strip()[:120]
    now = time.time()
    con = db()
    row = con.execute('SELECT id, role FROM users WHERE hh_id=?', (hid,)).fetchone()
    if row:
        uid, role = row['id'], row['role']
        con.execute('UPDATE users SET name=?, email=?, phone=?, last_login_at=? WHERE id=?', (name, email, phone, now, uid))
    else:
        uid, role = uuid.uuid4().hex[:16], 'user'
        con.execute('INSERT INTO users (id, hh_id, name, email, phone, role, created_at, last_login_at) VALUES (?,?,?,?,?,?,?,?)',
                    (uid, hid, name, email, phone, role, now, now))
    if role != 'admin' and _is_admin_contact(email, phone):
        con.execute("UPDATE users SET role='admin' WHERE id=?", (uid,))
    con.commit()
    return get(uid)


def set_role(who, role):
    w = settings.norm_contact(who)
    con = db()
    cur = con.execute('UPDATE users SET role=? WHERE id=? OR hh_id=? OR email=? OR phone=?', (role, who, who, w, w))
    con.commit()
    return cur.rowcount


def count_download(platform, version):
    con = db()
    con.execute('INSERT INTO downloads (day, platform, version, count) VALUES (?,?,?,1) '
                'ON CONFLICT(day, platform, version) DO UPDATE SET count=count+1',
                (time.strftime('%Y-%m-%d', time.gmtime()), platform, version or ''))
    con.commit()


def download_stats(days=30):
    since = time.strftime('%Y-%m-%d', time.gmtime(time.time() - days * 86400))
    con = db()
    by_platform = {r['platform']: r['n'] for r in con.execute(
        'SELECT platform, SUM(count) AS n FROM downloads WHERE day>=? GROUP BY platform', (since,))}
    total = con.execute('SELECT COALESCE(SUM(count), 0) FROM downloads').fetchone()[0]
    users = con.execute('SELECT COUNT(*) FROM users').fetchone()[0]
    return {'days': days, 'by_platform': by_platform, 'recent': sum(by_platform.values()), 'total': total, 'users': users}


def main(argv):
    if len(argv) >= 2 and argv[0] in ('admin', 'unadmin'):
        n = set_role(argv[1], 'admin' if argv[0] == 'admin' else 'user')
        print(f'{argv[0]}: {argv[1]} -> {n} account(s) changed' if n else
              f'No Doppleganger account matches {argv[1]} yet. Sign in once at {settings.public_url()} first, '
              'or add it to ADMIN_CONTACTS.')
        return 0 if n else 1
    if argv[:1] == ['list']:
        for r in db().execute('SELECT * FROM users ORDER BY created_at'):
            print(f"{r['id']}  {r['role']:5}  {r['name']}  {r['email'] or ''} {r['phone'] or ''}")
        return 0
    if argv[:1] == ['check']:
        db()
        print(f'Database OK: {path()}')
        return 0
    print(__doc__)
    return 2


if __name__ == '__main__':
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(settings.ROOT, '.env'))
    except ImportError:
        pass
    sys.exit(main(sys.argv[1:]))
