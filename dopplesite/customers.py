"""Admin views across the whole customer base: one search box, a page per customer, saved filters, numbers,
CSV exports and bulk actions. Everything reads the same SQLite file as the rest of the site.

A "customer" is an email address: a Happy Hacks account (users table), the keys sold or given to that email, the
computers linked to the account or using those keys, and their support requests.
"""
import csv
import io
import time

from . import audit, licenses, support, users

DAY = 86400


def _db():
    licenses.db()
    support.db()
    audit.db()
    return users.db()


# ------------------------------------------------------------------ search
def search(q, limit=25):
    """Everything that matches: accounts, keys, computers and support requests -> list of results that each name the
    customer (email) they belong to, if any."""
    q = (q or '').strip()
    if len(q) < 2:
        return []
    con = _db()
    like = f'%{q.lower()}%'
    out = []
    for r in con.execute('SELECT * FROM users WHERE lower(name) LIKE ? OR lower(email) LIKE ? OR phone LIKE ? OR id=? '
                         'ORDER BY last_login_at DESC LIMIT ?', (like, like, like, q, limit)):
        out.append({'type': 'account', 'title': r['name'] or r['email'] or r['phone'], 'detail': r['email'] or r['phone'] or '',
                    'customer': r['email'] or r['id'], 'role': r['role']})
    for r in con.execute('SELECT * FROM licenses WHERE lower(key) LIKE ? OR lower(email) LIKE ? OR lower(note) LIKE ? '
                         'OR lower(square_subscription_id) LIKE ? ORDER BY created_at DESC LIMIT ?',
                         (like, like, like, like, limit)):
        out.append({'type': 'key', 'title': r['key'], 'detail': f"{r['plan']} · {r['email'] or r['note']}",
                    'customer': r['email'] or _user_email(r['user_id']) or '', 'id': r['id']})
    for r in con.execute('SELECT * FROM devices WHERE lower(name) LIKE ? OR id LIKE ? OR last_ip LIKE ? OR first_ip LIKE ? '
                         'ORDER BY last_seen DESC LIMIT ?', (like, like, like, like, limit)):
        out.append({'type': 'computer', 'title': r['name'] or 'Computer', 'detail': f"{r['platform']} · v{r['app_version']}",
                    'customer': _device_owner(r) or '', 'id': r['id']})
    rows, _ = support.search(q, limit=limit)
    for t in rows:
        out.append({'type': 'request', 'title': f"#{t['id']} {t['subject']}", 'detail': f"{t['status']} · {t['email']}",
                    'customer': t['email'], 'id': t['id']})
    return out


def _user_email(uid):
    if not uid:
        return ''
    r = users.db().execute('SELECT email FROM users WHERE id=?', (uid,)).fetchone()
    return (r['email'] or '') if r else ''


def _device_owner(dev):
    if dev['user_id']:
        e = _user_email(dev['user_id'])
        if e:
            return e
    r = licenses.db().execute('SELECT l.email, l.user_id FROM license_devices ld JOIN licenses l ON l.id = ld.license_id '
                              'WHERE ld.device_id=? ORDER BY ld.added_at DESC LIMIT 1', (dev['id'],)).fetchone()
    return (r['email'] or _user_email(r['user_id'])) if r else ''


# ------------------------------------------------------------------ one customer
def customer(who):
    """Everything about one customer, by email (or account id)."""
    con = _db()
    who = (who or '').strip()
    u = con.execute('SELECT * FROM users WHERE id=? OR lower(email)=?', (who, who.lower())).fetchone()
    u = dict(u) if u else None
    email = (u['email'] if u else who).lower() if (u and u['email']) or '@' in who else ''
    keys = licenses.for_user(u['id'] if u else '-', email or None)
    devs = licenses.user_devices(u['id'] if u else '-', email or None)
    tickets = support.search(email=email or None, user_id=u['id'] if u else None, limit=100)[0] if (email or u) else []
    targets = [k['id'] for k in keys] + [d['id'] for d in devs] + ([u['id']] if u else []) + ([email] if email else [])
    history = audit.recent(100, target=targets) if targets else []
    if not (u or keys or devs or tickets):
        return None
    return {'user': u, 'email': email, 'keys': keys, 'devices': devs, 'tickets': tickets, 'history': history,
            'self_moves_left': {k['id']: licenses.self_moves_left(k['id']) for k in keys}}


# ------------------------------------------------------------------ saved filters
FILTERS = {
    'payment_problem': 'Subscriptions with a payment problem',
    'cancelled': 'Cancelled subscriptions (still in their paid month or ended in the last 30 days)',
    'at_limit': 'Keys using all their computers',
    'expiring': 'Keys that end in the next 14 days',
    'leads': 'Computers out of free time, with no key (sales leads)',
    'stale': 'Computers not seen for 90 days',
    'no_account': 'Computers with no account (e.g. downloaded straight from GitHub)',
    'new_accounts': 'Accounts created in the last 7 days',
}


def run_filter(name, limit=500):
    con = _db()
    now = time.time()
    if name == 'payment_problem':
        rows = con.execute("SELECT id FROM licenses WHERE plan='subscription' AND square_status IN "
                           "('DEACTIVATED','PAUSED','DELINQUENT','PAST_DUE') ORDER BY created_at DESC LIMIT ?", (limit,))
        return 'keys', [licenses.get(r['id']) for r in rows]
    if name == 'cancelled':
        rows = con.execute("SELECT id FROM licenses WHERE plan='subscription' AND square_status='CANCELED' AND "
                           "(expires_at IS NULL OR expires_at > ?) ORDER BY expires_at DESC LIMIT ?", (now - 30 * DAY, limit))
        return 'keys', [licenses.get(r['id']) for r in rows]
    if name == 'at_limit':
        rows = con.execute("SELECT l.id FROM licenses l WHERE l.status='active' AND (SELECT COUNT(*) FROM license_devices "
                           "ld WHERE ld.license_id=l.id) >= l.max_devices ORDER BY l.created_at DESC LIMIT ?", (limit,))
        return 'keys', [licenses.get(r['id']) for r in rows]
    if name == 'expiring':
        rows = con.execute("SELECT id FROM licenses WHERE status='active' AND expires_at BETWEEN ? AND ? "
                           "ORDER BY expires_at LIMIT ?", (now, now + 14 * DAY, limit))
        return 'keys', [licenses.get(r['id']) for r in rows]
    if name == 'leads':
        free = licenses.free_seconds()
        rows = con.execute("SELECT * FROM devices d WHERE d.used_seconds >= ? + d.bonus_seconds AND NOT EXISTS "
                           "(SELECT 1 FROM license_devices ld JOIN licenses l ON l.id = ld.license_id WHERE "
                           "ld.device_id = d.id AND l.status='active' AND (l.expires_at IS NULL OR l.expires_at > ?)) "
                           "ORDER BY d.last_seen DESC LIMIT ?", (free, now, limit))
        return 'computers', [_dev(r) for r in rows]
    if name == 'stale':
        rows = con.execute('SELECT * FROM devices WHERE last_seen < ? ORDER BY last_seen LIMIT ?', (now - 90 * DAY, limit))
        return 'computers', [_dev(r) for r in rows]
    if name == 'no_account':
        rows = con.execute('SELECT * FROM devices d WHERE d.user_id IS NULL AND NOT EXISTS (SELECT 1 FROM license_devices '
                           'ld JOIN licenses l ON l.id = ld.license_id WHERE ld.device_id = d.id AND (l.user_id IS NOT NULL '
                           "OR l.email <> '')) ORDER BY d.last_seen DESC LIMIT ?", (limit,))
        return 'computers', [_dev(r) for r in rows]
    if name == 'new_accounts':
        rows = con.execute('SELECT * FROM users WHERE created_at > ? ORDER BY created_at DESC LIMIT ?', (now - 7 * DAY, limit))
        return 'accounts', [dict(r) for r in rows]
    raise KeyError(name)


def _dev(row):
    d = dict(row)
    d['free_left'] = licenses.free_left(d)
    d['customer'] = _device_owner(row)
    return d


# ------------------------------------------------------------------ numbers
def metrics():
    con = _db()
    now = time.time()
    from . import square
    subs = [dict(r) for r in con.execute(
        "SELECT * FROM licenses WHERE plan='subscription' AND status='active' AND (expires_at IS NULL OR expires_at > ?) "
        "AND square_status NOT IN ('CANCELED','DEACTIVATED')", (now,))]
    mrr = 0
    for lic in subs:
        r = None
        if lic.get('user_id'):
            try:
                r = con.execute("SELECT price_cents FROM square_checkouts WHERE user_id=? AND status='done' "
                                'ORDER BY created_at DESC LIMIT 1', (lic['user_id'],)).fetchone()
            except Exception:  # noqa: BLE001 - Square tables not created yet
                r = None
        mrr += (r['price_cents'] if r and r['price_cents'] else square.price_cents())
    count = lambda sql, *a: con.execute(sql, a).fetchone()[0]   # noqa: E731
    weeks = []
    for i in range(7, -1, -1):
        a, b = now - (i + 1) * 7 * DAY, now - i * 7 * DAY
        weeks.append({'week': time.strftime('%b %-d', time.localtime(a)),
                      'accounts': count('SELECT COUNT(*) FROM users WHERE created_at BETWEEN ? AND ?', a, b),
                      'computers': count('SELECT COUNT(*) FROM devices WHERE first_seen BETWEEN ? AND ?', a, b),
                      'keys': count('SELECT COUNT(*) FROM licenses WHERE created_at BETWEEN ? AND ?', a, b)})
    tc = support.counts()
    return {
        'accounts': count('SELECT COUNT(*) FROM users'),
        'accounts_7d': count('SELECT COUNT(*) FROM users WHERE created_at > ?', now - 7 * DAY),
        'computers': count('SELECT COUNT(*) FROM devices'),
        'active_7d': count('SELECT COUNT(*) FROM devices WHERE last_seen > ?', now - 7 * DAY),
        'active_30d': count('SELECT COUNT(*) FROM devices WHERE last_seen > ?', now - 30 * DAY),
        'subscribers': len(subs),
        'mrr_cents': mrr,
        'cancelled_30d': count("SELECT COUNT(*) FROM licenses WHERE plan='subscription' AND square_status='CANCELED' "
                               'AND expires_at > ?', now - 30 * DAY),
        'no_account': count('SELECT COUNT(*) FROM devices WHERE user_id IS NULL'),
        'other_keys': count("SELECT COUNT(*) FROM licenses WHERE plan<>'subscription' AND status='active' AND "
                            '(expires_at IS NULL OR expires_at > ?)', now),
        'open_requests': tc.get('new', 0) + tc.get('open', 0),
        'new_requests': tc.get('new', 0),
        'weeks': weeks,
    }


# ------------------------------------------------------------------ CSV
def export(kind):
    con = _db()
    buf = io.StringIO()
    w = csv.writer(buf)
    iso = lambda t: time.strftime('%Y-%m-%d %H:%M', time.gmtime(t)) if t else ''   # noqa: E731
    if kind == 'accounts':
        w.writerow(['email', 'name', 'phone', 'role', 'created (UTC)', 'last sign-in (UTC)', 'keys', 'computers'])
        for r in con.execute('SELECT * FROM users ORDER BY created_at'):
            keys = licenses.for_user(r['id'], r['email'])
            w.writerow([r['email'] or '', r['name'], r['phone'] or '', r['role'], iso(r['created_at']),
                        iso(r['last_login_at']), len(keys), len(licenses.user_devices(r['id'], r['email']))])
    elif kind == 'keys':
        w.writerow(['key', 'plan', 'status', 'square status', 'email', 'note', 'computers', 'max computers',
                    'expires (UTC)', 'created (UTC)', 'created by'])
        for r in con.execute('SELECT * FROM licenses ORDER BY created_at'):
            n = con.execute('SELECT COUNT(*) FROM license_devices WHERE license_id=?', (r['id'],)).fetchone()[0]
            w.writerow([r['key'], r['plan'], r['status'], r['square_status'], r['email'], r['note'], n, r['max_devices'],
                        iso(r['expires_at']), iso(r['created_at']), r['created_by']])
    elif kind == 'computers':
        w.writerow(['device id', 'name', 'platform', 'version', 'free minutes left', 'first seen (UTC)', 'last seen (UTC)',
                    'last IP', 'account email'])
        for r in con.execute('SELECT * FROM devices ORDER BY first_seen'):
            w.writerow([r['id'], r['name'], r['platform'], r['app_version'], int(licenses.free_left(dict(r)) // 60),
                        iso(r['first_seen']), iso(r['last_seen']), r['last_ip'], _device_owner(r)])
    elif kind == 'requests':
        w.writerow(['#', 'status', 'email', 'name', 'topic', 'subject', 'app version', 'created (UTC)', 'updated (UTC)'])
        for r in con.execute('SELECT * FROM tickets ORDER BY id'):
            w.writerow([r['id'], r['status'], r['email'], r['name'], r['topic'], r['subject'], r['app_version'],
                        iso(r['created_at']), iso(r['updated_at'])])
    else:
        raise KeyError(kind)
    return buf.getvalue()


# ------------------------------------------------------------------ bulk
def bulk_free_time(device_ids, minutes, who):
    n = 0
    for did in device_ids[:1000]:
        n += licenses.add_free_time(did, float(minutes) * 60)
    audit.log(who, 'bulk_free_time', f'{n} computers', minutes=minutes)
    return n


def bulk_delete_devices(device_ids, who):
    n = 0
    for did in device_ids[:1000]:
        n += licenses.delete_device(did)
    audit.log(who, 'bulk_delete_computers', f'{n} computers')
    return n
