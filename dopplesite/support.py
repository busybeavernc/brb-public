"""Support requests ("tickets"): customers write in from /support (or BRB's Contact support), staff answer in Admin.

A ticket belongs to an email address (and the signed-in account, if any). The customer gets a private link
(/support/t/<token>) to read and answer it, by email and on their account page. Staff replies are emailed; internal
notes never leave Admin. Attachments (a log or a screenshot, up to 2 MB) are kept in DATA_DIR/support/<ticket>/.
"""
import os
import re
import secrets
import time

from . import mailer, settings, users

STATUSES = ('new', 'open', 'waiting', 'closed')       # waiting = on the customer
TOPICS = {
    'license': 'License or billing',
    'install': 'Installing or updating',
    'camera': 'Camera or meeting app',
    'loop': 'Recording or using loops',
    'alerts': 'Name alerts',
    'other': 'Something else',
}
MAX_ATTACHMENT = 2 * 1024 * 1024
ATTACH_TYPES = {'.log': 'text/plain', '.txt': 'text/plain', '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg'}

SCHEMA = """
CREATE TABLE IF NOT EXISTS tickets (
  id INTEGER PRIMARY KEY AUTOINCREMENT, token TEXT UNIQUE NOT NULL, user_id TEXT, email TEXT NOT NULL,
  name TEXT NOT NULL DEFAULT '', topic TEXT NOT NULL DEFAULT 'other', subject TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'new', assignee TEXT NOT NULL DEFAULT '',
  app_version TEXT NOT NULL DEFAULT '', platform TEXT NOT NULL DEFAULT '', device_id TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL, updated_at REAL NOT NULL, ip TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS tickets_status ON tickets (status, updated_at);
CREATE INDEX IF NOT EXISTS tickets_email ON tickets (email);
CREATE INDEX IF NOT EXISTS tickets_user ON tickets (user_id);
CREATE TABLE IF NOT EXISTS ticket_messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ticket_id INTEGER NOT NULL, kind TEXT NOT NULL,
  author TEXT NOT NULL DEFAULT '', body TEXT NOT NULL, attachment TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ticket_messages_ticket ON ticket_messages (ticket_id, id);
CREATE TABLE IF NOT EXISTS canned_replies (
  id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, body TEXT NOT NULL
);
"""
STARTER_REPLIES = [
    ('Choose the BRB camera in your meeting app',
     "In your meeting app's video settings, choose \"HappyHacks Virtual Camera\" as the camera (in Zoom: Settings > "
     "Video > Camera). BRB then sends either your live picture or your loop. If it isn't listed, open BRB's setup "
     "guide (Help > Setup guide) and click Install Virtual Camera, then restart the meeting app."),
    ('Move your key to a new computer',
     "I've removed your old computer from your key, so it will work on the next computer you use it on. In BRB, open "
     "Settings > License, paste your key and click Apply. You can also do this yourself any time from "
     "brb.happyhacks.io/account."),
    ('Send us your log',
     "Could you send us BRB's log? In BRB, open Help > Contact support: it fills in your version, and you can attach "
     "brb.log from the folder it opens (it's in %APPDATA%\\BRB on Windows). It contains no video or audio."),
]
_ready = set()


class SupportError(Exception):
    pass


def db():
    con = users.db()
    key = (id(con), users.path())
    if key not in _ready:
        con.executescript(SCHEMA)
        if not con.execute('SELECT 1 FROM canned_replies LIMIT 1').fetchone():
            con.executemany('INSERT INTO canned_replies (title, body) VALUES (?,?)', STARTER_REPLIES)
            con.commit()
        _ready.add(key)
    return con


def _clean(s, n):
    return re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', str(s or '')).strip()[:n]


def valid_email(e):
    return bool(re.fullmatch(r'[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+', e or '')) and len(e) <= 200


def ticket_url(t):
    return f"{settings.public_url()}/support/t/{t['token']}"


def attachments_dir(ticket_id):
    d = os.path.join(settings.data_dir(), 'support', str(int(ticket_id)))
    os.makedirs(d, exist_ok=True)
    return d


def _save_attachment(ticket_id, msg_id, upload):
    """-> stored file name, or '' (no file). Raises SupportError for a file we don't take."""
    if upload is None or not getattr(upload, 'filename', ''):
        return ''
    name = os.path.basename(upload.filename.replace('\\', '/'))
    ext = os.path.splitext(name)[1].lower()
    if ext not in ATTACH_TYPES:
        raise SupportError('Attach a .log, .txt, .png or .jpg file.')
    data = upload.read(MAX_ATTACHMENT + 1)
    if len(data) > MAX_ATTACHMENT:
        raise SupportError('That file is over 2 MB. Please attach a smaller one.')
    if not data:
        return ''
    safe = re.sub(r'[^A-Za-z0-9._-]', '_', name)[:80] or 'file' + ext
    stored = f'{msg_id}-{safe}'
    with open(os.path.join(attachments_dir(ticket_id), stored), 'wb') as f:
        f.write(data)
    return stored


def attachment_path(ticket_id, stored):
    """Full path of an attachment, only if it really belongs to this ticket."""
    if not stored or '/' in stored or '\\' in stored or stored.startswith('.'):
        return None
    row = db().execute('SELECT 1 FROM ticket_messages WHERE ticket_id=? AND attachment=?', (ticket_id, stored)).fetchone()
    p = os.path.join(attachments_dir(ticket_id), stored)
    return p if row and os.path.exists(p) else None


# ------------------------------------------------------------------ tickets
def create(email, subject, body, name='', topic='other', user_id=None, app_version='', platform='', device_id='',
           ip='', upload=None, confirm=True):
    email = _clean(email, 200).lower()
    subject, body, name = _clean(subject, 150), _clean(body, 8000), _clean(name, 120)
    if not valid_email(email):
        raise SupportError('Please enter a valid email address, so we can answer you.')
    if len(body) < 10:
        raise SupportError('Please describe what happened (a sentence or two).')
    topic = topic if topic in TOPICS else 'other'
    subject = subject or TOPICS[topic]
    now = time.time()
    con = db()
    cur = con.execute('INSERT INTO tickets (token, user_id, email, name, topic, subject, app_version, platform, device_id, '
                      'created_at, updated_at, ip) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                      (secrets.token_urlsafe(24), user_id, email, name, topic, subject, _clean(app_version, 20),
                       _clean(platform, 20), _clean(device_id, 64), now, now, _clean(ip, 60)))
    tid = cur.lastrowid
    try:
        _add_message(con, tid, 'customer', name or email, body, upload)
    except SupportError:
        con.rollback()
        con.execute('DELETE FROM tickets WHERE id=?', (tid,))
        con.commit()
        raise
    con.commit()
    t = get(tid)
    if confirm:   # (not for requests that came in by email: no automatic answers to whoever wrote, e.g. spam)
        mailer.send(email, f'[BRB #{tid}] We got your message: {subject}',
                    f"Hi{(' ' + name.split()[0]) if name else ''},\n\nThanks for writing to BRB support. We usually "
                    f"answer within one business day.\n\nYour request: {subject}\nSee it or add to it any time: "
                    f"{ticket_url(t)}\n\nYou can also just reply to this email.\n\n- BRB Support (Happy Hacks)")
    mailer.send(mailer.staff(), f'[BRB #{tid}] New: {subject}',
                f"From: {name} <{email}>\nTopic: {TOPICS[topic]}\nBRB {app_version or '?'} {platform}\n\n{body}\n\n"
                f"Answer it in Admin: {settings.public_url()}/admin#support/{tid}", reply_to=email)
    return t


def _add_message(con, tid, kind, author, body, upload=None):
    cur = con.execute('INSERT INTO ticket_messages (ticket_id, kind, author, body, created_at) VALUES (?,?,?,?,?)',
                      (tid, kind, _clean(author, 120), body, time.time()))
    mid = cur.lastrowid
    stored = _save_attachment(tid, mid, upload)
    if stored:
        con.execute('UPDATE ticket_messages SET attachment=? WHERE id=?', (stored, mid))
    return mid


def get(tid):
    row = db().execute('SELECT * FROM tickets WHERE id=?', (tid,)).fetchone()
    return dict(row) if row else None


def by_token(token):
    if not token or len(token) > 64:
        return None
    row = db().execute('SELECT * FROM tickets WHERE token=?', (token,)).fetchone()
    return dict(row) if row else None


def messages(tid, include_notes=True):
    rows = db().execute('SELECT * FROM ticket_messages WHERE ticket_id=? ORDER BY id', (tid,))
    return [dict(r) for r in rows if include_notes or r['kind'] != 'note']


def customer_reply(t, body, upload=None):
    body = _clean(body, 8000)
    if len(body) < 2:
        raise SupportError('Please write a reply.')
    con = db()
    _add_message(con, t['id'], 'customer', t['name'] or t['email'], body, upload)
    con.execute("UPDATE tickets SET status='open', updated_at=? WHERE id=?", (time.time(), t['id']))
    con.commit()
    mailer.send(mailer.staff(), f"[BRB #{t['id']}] Reply: {t['subject']}",
                f"{t['name'] or t['email']} wrote:\n\n{body}\n\n{settings.public_url()}/admin#support/{t['id']}",
                reply_to=t['email'])


def staff_reply(tid, who, body, note=False, status=None):
    """A reply (emailed to the customer) or an internal note. Replies set the ticket to 'waiting' unless told otherwise."""
    t = get(tid)
    if not t:
        raise SupportError('No such request.')
    body = _clean(body, 8000)
    if not body:
        raise SupportError('Write something first.')
    con = db()
    _add_message(con, tid, 'note' if note else 'staff', who, body)
    new = status if status in STATUSES else (t['status'] if note else 'waiting')
    con.execute('UPDATE tickets SET status=?, updated_at=?, assignee=CASE WHEN assignee=\'\' THEN ? ELSE assignee END '
                'WHERE id=?', (new, time.time(), '' if note else who, tid))
    con.commit()
    if not note:
        mailer.send(t['email'], f"[BRB #{tid}] {t['subject']}",
                    f"{body}\n\n---\nReply to this email, or see the whole conversation: {ticket_url(t)}\n"
                    "- BRB Support (Happy Hacks)")
    return get(tid)


def set_status(tid, status, who=''):
    if status not in STATUSES:
        raise SupportError('Unknown status.')
    con = db()
    n = con.execute('UPDATE tickets SET status=?, updated_at=? WHERE id=?', (status, time.time(), tid)).rowcount
    con.commit()
    return n


def assign(tid, who):
    con = db()
    con.execute('UPDATE tickets SET assignee=? WHERE id=?', (_clean(who, 120), tid))
    con.commit()


def search(q='', status='', email=None, user_id=None, limit=50, offset=0):
    where, args = [], []
    if status == 'active':
        where.append("status IN ('new','open')")
    elif status in STATUSES:
        where.append('status=?')
        args.append(status)
    if email or user_id:
        where.append('(email=? OR user_id=?)')
        args += [(email or '-').lower(), user_id or '-']
    if q:
        like = f'%{q.strip().lower()}%'
        if q.strip().lstrip('#').isdigit():
            where.append('(id=? OR lower(subject) LIKE ?)')
            args += [int(q.strip().lstrip('#')), like]
        else:
            where.append('(lower(email) LIKE ? OR lower(name) LIKE ? OR lower(subject) LIKE ? OR id IN '
                         '(SELECT ticket_id FROM ticket_messages WHERE lower(body) LIKE ?))')
            args += [like] * 4
    # the WHERE parts are fixed strings from this function; every value is a bound parameter
    sql = 'SELECT * FROM tickets' + (' WHERE ' + ' AND '.join(where) if where else '')  # nosec B608
    total = db().execute(sql.replace('SELECT *', 'SELECT COUNT(*)', 1), args).fetchone()[0]
    rows = db().execute(sql + ' ORDER BY CASE status WHEN \'new\' THEN 0 WHEN \'open\' THEN 1 ELSE 2 END, updated_at DESC '
                        'LIMIT ? OFFSET ?', (*args, int(limit), int(offset))).fetchall()
    return [dict(r) for r in rows], total


def counts():
    return {r['status']: r['n'] for r in db().execute('SELECT status, COUNT(*) AS n FROM tickets GROUP BY status')}


def recent_from_ip(ip, window=3600):
    return db().execute('SELECT COUNT(*) FROM tickets WHERE ip=? AND created_at > ?', (ip, time.time() - window)).fetchone()[0]


# ------------------------------------------------------------------ ready-made replies
def canned():
    return [dict(r) for r in db().execute('SELECT * FROM canned_replies ORDER BY title')]


def save_canned(title, body, cid=None):
    title, body = _clean(title, 120), _clean(body, 4000)
    if not title or not body:
        raise SupportError('A ready-made reply needs a title and text.')
    con = db()
    if cid:
        con.execute('UPDATE canned_replies SET title=?, body=? WHERE id=?', (title, body, cid))
    else:
        con.execute('INSERT INTO canned_replies (title, body) VALUES (?,?)', (title, body))
    con.commit()


def delete_canned(cid):
    con = db()
    con.execute('DELETE FROM canned_replies WHERE id=?', (cid,))
    con.commit()


KEEP_CLOSED_DAYS = 3 * 365   # Privacy Policy: closed requests are deleted after three years


def purge_old(now=None):
    """Delete requests closed more than KEEP_CLOSED_DAYS ago, with their messages and files. -> how many."""
    import shutil
    cutoff = (now or time.time()) - KEEP_CLOSED_DAYS * 86400
    con = db()
    ids = [r['id'] for r in con.execute("SELECT id FROM tickets WHERE status='closed' AND updated_at < ?", (cutoff,))]
    for tid in ids:
        con.execute('DELETE FROM ticket_messages WHERE ticket_id=?', (tid,))
        con.execute('DELETE FROM tickets WHERE id=?', (tid,))
        shutil.rmtree(os.path.join(settings.data_dir(), 'support', str(int(tid))), ignore_errors=True)
    con.commit()
    return len(ids)
