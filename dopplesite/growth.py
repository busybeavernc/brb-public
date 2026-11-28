"""Launch helpers: the download -> subscription funnel, opt-in crash reports from the app, and the welcome and 7-day
check-in emails (with an unsubscribe link).

Funnel: every computer that asks /api/v1/license/status gets one row in `installs` (by its scrambled id), with the
first time it recorded a loop, put it on air, ran out of free time and became unlimited. No personal data.
"""
import hashlib
import hmac
import logging
import re
import threading
import time

from . import mailer, settings, users

log = logging.getLogger('dopplesite')

SCHEMA = """
CREATE TABLE IF NOT EXISTS installs (
  device_id TEXT PRIMARY KEY, platform TEXT NOT NULL DEFAULT '', app_version TEXT NOT NULL DEFAULT '',
  first_seen REAL NOT NULL, last_seen REAL NOT NULL,
  loop_at REAL, on_air_at REAL, ran_out_at REAL, unlimited_at REAL
);
CREATE INDEX IF NOT EXISTS installs_first ON installs(first_seen);
CREATE TABLE IF NOT EXISTS crashes (
  id INTEGER PRIMARY KEY AUTOINCREMENT, signature TEXT UNIQUE NOT NULL, kind TEXT NOT NULL DEFAULT '',
  summary TEXT NOT NULL DEFAULT '', report TEXT NOT NULL DEFAULT '', app_version TEXT NOT NULL DEFAULT '',
  platform TEXT NOT NULL DEFAULT '', first_at REAL NOT NULL, last_at REAL NOT NULL, count INTEGER NOT NULL DEFAULT 1,
  computers TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'new'
);
"""
MAX_REPORT = 64 * 1024
_ready = set()


def db():
    con = users.db()
    key = (id(con), users.path())
    if key not in _ready:
        con.executescript(SCHEMA)
        cols = [r[1] for r in con.execute('PRAGMA table_info(users)')]
        for col, kind in (('emails_off', 'INTEGER NOT NULL DEFAULT 0'), ('welcome_at', 'REAL'), ('checkin_at', 'REAL')):
            if col not in cols:
                con.execute(f'ALTER TABLE users ADD COLUMN {col} {kind}')
        con.commit()
        _ready.add(key)
    return con


# ------------------------------------------------------------------ funnel
def note_status(d, out, on_air=False):
    """Called for each status/lease request: d is what the app sent, out what we answered."""
    did = str(d.get('device_id') or '')[:128]
    if not did:
        return
    now = time.time()
    con = db()
    con.execute('INSERT INTO installs (device_id, platform, app_version, first_seen, last_seen) VALUES (?,?,?,?,?) '
                'ON CONFLICT(device_id) DO UPDATE SET platform=excluded.platform, app_version=excluded.app_version, '
                'last_seen=excluded.last_seen',
                (did, str(d.get('platform') or '')[:40], str(d.get('app_version') or '')[:40], now, now))
    sets = []
    if d.get('has_loop'):
        sets.append('loop_at')
    if on_air:
        sets += ['loop_at', 'on_air_at']
    if out.get('unlimited') and out.get('plan') not in (None, 'free'):
        sets.append('unlimited_at')
    elif out.get('free_left') is not None and float(out.get('free_left') or 0) <= 0:
        sets.append('ran_out_at')
    for col in dict.fromkeys(sets):
        con.execute(f'UPDATE installs SET {col}=? WHERE device_id=? AND {col} IS NULL', (now, did))
    con.commit()


def funnel(days=30):
    """Computers first seen in the last `days`, and how far each got."""
    since = time.time() - days * 86400
    con = db()
    r = con.execute('SELECT COUNT(*) AS installs, COUNT(loop_at) AS loop, COUNT(on_air_at) AS on_air, '
                    'COUNT(ran_out_at) AS ran_out, COUNT(unlimited_at) AS unlimited FROM installs WHERE first_seen>=?',
                    (since,)).fetchone()
    stats = users.download_stats(days)
    out = {'days': days, 'site_downloads': stats['recent'], 'site_downloads_total': stats['total'], **dict(r)}
    try:
        from . import releases
        rel = releases.latest() or {}
        out['github_downloads_total'] = rel.get('github_downloads')
    except Exception:  # noqa: BLE001
        out['github_downloads_total'] = None
    out['all_time_installs'] = con.execute('SELECT COUNT(*) FROM installs').fetchone()[0]
    return out


# ------------------------------------------------------------------ crash reports
_HOME = re.compile(r'([A-Za-z]:\\Users\\|/Users/|/home/)[^\\/\n"\']+', re.I)


def scrub(text):
    """Take people's user names out of file paths (C:\\Users\\Ann\\... -> C:\\Users\\~\\...)."""
    return _HOME.sub(lambda m: m.group(1) + '~', str(text or ''))


def _signature(kind, report, version):
    lines = [ln.strip() for ln in report.strip().splitlines() if ln.strip()]
    frames = [ln for ln in lines if ln.startswith('File ')][-3:]
    last = lines[-1] if lines else ''
    last = re.sub(r'\d+', '#', last)[:200]     # numbers change between crashes of the same bug
    return hashlib.sha256('\n'.join([kind, version.split('.')[0] if version else '', *frames, last]).encode()).hexdigest()[:32]


def save_crash(d):
    report = scrub(str(d.get('report') or ''))[:MAX_REPORT]
    if not report.strip():
        raise ValueError('empty report')
    kind = str(d.get('kind') or 'crash')[:40]
    version = str(d.get('app_version') or '')[:40]
    lines = [ln for ln in report.strip().splitlines() if ln.strip()]
    summary = scrub(lines[-1] if lines else '')[:300]
    sig = _signature(kind, report, version)
    did = str(d.get('device_id') or '')[:12]
    now = time.time()
    con = db()
    row = con.execute('SELECT id, computers FROM crashes WHERE signature=?', (sig,)).fetchone()
    if row:
        comps = row['computers'].split(',') if row['computers'] else []
        if did and did not in comps and len(comps) < 200:
            comps.append(did)
        con.execute("UPDATE crashes SET count=count+1, last_at=?, app_version=?, platform=?, report=?, computers=?, "
                    "status=CASE WHEN status='fixed' THEN 'new' ELSE status END WHERE id=?",
                    (now, version, str(d.get('platform') or '')[:40], report, ','.join(comps), row['id']))
        cid = row['id']
    else:
        cur = con.execute('INSERT INTO crashes (signature, kind, summary, report, app_version, platform, first_at, '
                          'last_at, computers) VALUES (?,?,?,?,?,?,?,?,?)',
                          (sig, kind, summary, report, version, str(d.get('platform') or '')[:40], now, now, did))
        cid = cur.lastrowid
    con.commit()
    return cid


def crashes(limit=100):
    rows = [dict(r) for r in db().execute(
        "SELECT id, kind, summary, app_version, platform, first_at, last_at, count, computers, status FROM crashes "
        "ORDER BY CASE status WHEN 'new' THEN 0 WHEN 'seen' THEN 1 ELSE 2 END, last_at DESC LIMIT ?", (limit,))]
    for r in rows:
        r['computers'] = len([c for c in r['computers'].split(',') if c])
    return rows


def crash(cid):
    r = db().execute('SELECT * FROM crashes WHERE id=?', (cid,)).fetchone()
    return dict(r) if r else None


def set_crash_status(cid, status):
    if status not in ('new', 'seen', 'fixed'):
        raise ValueError('unknown status')
    con = db()
    n = con.execute('UPDATE crashes SET status=? WHERE id=?', (status, cid)).rowcount
    con.commit()
    return n


# ------------------------------------------------------------------ welcome and check-in emails
CHECKIN_DAYS = 7


def _token(uid):
    key = (settings.get('SECRET_KEY', 'dev-only-change-me') or '').encode()
    return hmac.new(key, b'unsubscribe:' + uid.encode(), hashlib.sha256).hexdigest()[:24]


def unsubscribe_url(uid):
    return f'{settings.public_url()}/email/unsubscribe?u={uid}&t={_token(uid)}'


def unsubscribe(uid, token, on=True):
    if not uid or not hmac.compare_digest(_token(uid), str(token or '')):
        return False
    con = db()
    n = con.execute('UPDATE users SET emails_off=? WHERE id=?', (1 if on else 0, uid)).rowcount
    con.commit()
    return bool(n)


def _footer(uid):
    lines = ['', '--', 'You get this because you signed in to BRB at brb.happyhacks.io.',
             f"Don't want these tips? Unsubscribe: {unsubscribe_url(uid)}",
             '(Emails about your license, payments and support requests still come.)']
    addr = (settings.get('MAIL_POSTAL_ADDRESS', '') or '').strip()
    lines.append('Busy Beaver Group LLC, doing business as Happy Hacks' + (f', {addr}' if addr else ''))
    return lines


def _send(user, subject, lines):
    uid = user['id']
    return mailer.send(user['email'], subject, '\n'.join(lines + _footer(uid)), kind='account',
                       headers={'List-Unsubscribe': f'<{unsubscribe_url(uid)}>',
                                'List-Unsubscribe-Post': 'List-Unsubscribe=One-Click'})


def _hi(user):
    from .purchasemail import _name
    return f'Hi {_name(user)},' if _name(user) else 'Hi,'


def welcome(user):
    """Once, when someone signs in for the first time."""
    if not user or not user.get('email'):
        return False
    con = db()
    row = con.execute('SELECT welcome_at, emails_off FROM users WHERE id=?', (user['id'],)).fetchone()
    if not row or row['welcome_at'] or row['emails_off']:
        return False
    con.execute('UPDATE users SET welcome_at=? WHERE id=?', (time.time(), user['id']))
    con.commit()
    site = settings.public_url()
    return _send(user, 'Welcome to BRB', [
        _hi(user), '',
        'Thanks for signing up for BRB (Be Right Back). Getting set up takes about two minutes:', '',
        f'1. Download and install BRB: {site}/#download',
        '2. In Zoom, Teams, Meet or any meeting app, pick "HappyHacks Virtual Camera" as your camera',
        '   (on a Mac: "OBS Virtual Camera").',
        '3. Click "Record loop" and sit naturally for a minute or so. That loop is what people see when you step away.', '',
        'Then, in a meeting, press F9 (fn+F9 on many laptops) to cut to your loop, and again to come back live.', '',
        f'The guide covers everything else: {site}/guide',
        'Questions? Just reply to this email.', '', 'The BRB team at Happy Hacks'])


def _checkin_lines(user):
    site = settings.public_url()
    con = db()
    paid = con.execute("SELECT COUNT(*) FROM licenses WHERE user_id=? AND status='active' AND plan!='free'",
                       (user['id'],)).fetchone()[0] if _has_table(con, 'licenses') else 0
    lines = [_hi(user), '', "You've had BRB for a week. A few things people find useful:", '',
             '- Record a fresh loop in the clothes and light you have that day: it matches far better.',
             '- Mini Bar keeps BRB small and out of the way during meetings.',
             '- F9 cuts to your loop and back while your meeting app is in front; F10 switches between the full',
             '  window and the Mini Bar.', '']
    if not paid:
        lines += ['Free LOOP time is limited. If BRB is earning its keep, unlimited is on the pricing page:',
                  f'  {site}/#pricing', '']
    lines += ["Has anything not worked the way you expected? Reply and tell us: we read every message.", '',
              'The BRB team at Happy Hacks']
    return lines


def _has_table(con, name):
    return bool(con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


def send_checkins(now=None):
    """Accounts made 7-14 days ago that haven't had their check-in (older accounts never get one)."""
    now = now or time.time()
    con = db()
    rows = [dict(r) for r in con.execute(
        'SELECT * FROM users WHERE checkin_at IS NULL AND emails_off=0 AND email IS NOT NULL AND created_at<=? '
        'AND created_at>?', (now - CHECKIN_DAYS * 86400, now - 2 * CHECKIN_DAYS * 86400))]
    sent = 0
    for u in rows:
        con.execute('UPDATE users SET checkin_at=? WHERE id=?', (now, u['id']))
        con.commit()
        if _send(u, 'How is BRB working for you?', _checkin_lines(u)):
            sent += 1
    return sent


_last = {'at': 0.0}


def maybe_run():
    """Called on requests: sends due check-ins about once an hour, in the background."""
    if settings.get('NO_AUTO_BACKUP') or time.time() - _last['at'] < 3600:
        return
    _last['at'] = time.time()

    def run():
        try:
            con = db()
            con.execute('DELETE FROM crashes WHERE last_at<?', (time.time() - 365 * 86400,))   # kept at most a year
            con.commit()
        except Exception:  # noqa: BLE001
            log.exception('crash cleanup')
        try:
            n = send_checkins()
            if n:
                log.info('check-in emails sent: %d', n)
        except Exception:  # noqa: BLE001
            log.exception('check-in emails')
    threading.Thread(target=run, daemon=True, name='checkins').start()
