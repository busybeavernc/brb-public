"""Email in: read the support mailbox (e.g. support@happyhacks.io) over IMAP and turn mail into support requests.

- A reply to one of our emails ("[BRB #12] ..." in the subject) from that request's email address is added to it.
- Any other new email becomes a new request (topic "Something else"), so writing to support@ works like the form.
- Auto-replies, bounces and our own messages are skipped. The mailbox itself is left as it is: nothing is deleted,
  moved or marked read, so you can still read it in any mail app. BRB remembers which messages it has taken.

Settings (.env): IMAP_HOST (e.g. mail.happyhacks.io), IMAP_PORT (993), IMAP_USER / IMAP_PASSWORD (default: the
SMTP_USER / SMTP_PASSWORD), IMAP_FOLDER (INBOX). Checked every MAILIN_MINUTES (2) while the site runs, by one worker.
"""
import email
import email.policy
import email.utils
import imaplib
import logging
import os
import re
import ssl
import threading
import time

from . import settings, support, users

log = logging.getLogger('dopplesite')
SUBJECT_NUMBER = re.compile(r'\[BRB #(\d+)\]', re.I)
SCHEMA = """
CREATE TABLE IF NOT EXISTS mailin_state (folder TEXT PRIMARY KEY, uidvalidity INTEGER NOT NULL, last_uid INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS mailin_seen (message_id TEXT PRIMARY KEY, at REAL NOT NULL, ticket_id INTEGER);
"""
_ready = set()
_started = False
_lock = threading.Lock()
status = {'last_check': None, 'last_error': '', 'taken': 0}


def ready():
    return bool(settings.get('IMAP_HOST'))


def db():
    con = users.db()
    key = (id(con), users.path())
    if key not in _ready:
        con.executescript(SCHEMA)
        _ready.add(key)
    return con


# ------------------------------------------------------------------ reading a message
class _Upload:
    """Looks like a Flask upload, for support._save_attachment."""

    def __init__(self, filename, data):
        self.filename, self._data = filename, data

    def read(self, n=-1):
        return self._data if n < 0 else self._data[:n]


def _text(msg):
    part = msg.get_body(preferencelist=('plain', 'html'))
    if part is None:
        return ''
    body = part.get_content()
    if part.get_content_type() == 'text/html':
        body = re.sub(r'(?is)<(script|style).*?</\1>', '', body)
        body = re.sub(r'(?i)<br\s*/?>|</p>|</div>', '\n', body)
        body = re.sub(r'<[^>]+>', '', body)
        body = re.sub(r'&nbsp;', ' ', body).replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>')
    return body


def strip_quoted(body):
    """Just what they wrote: drop the quoted earlier messages and our own footer."""
    out = []
    for line in body.replace('\r\n', '\n').split('\n'):
        s = line.strip()
        if (re.match(r'^On .{3,200} wrote:$', s) or s.startswith('-----Original Message') or
                re.match(r'^From: .+', s) and out and not out[-1].strip() or s == '---' or
                s.startswith('Reply to this email, or see the whole conversation')):
            break
        if s.startswith('>'):
            continue
        out.append(line)
    return '\n'.join(out).strip()


def _attachment(msg):
    for part in msg.iter_attachments():
        name = part.get_filename() or ''
        if os.path.splitext(name)[1].lower() in support.ATTACH_TYPES:
            data = part.get_payload(decode=True) or b''
            if 0 < len(data) <= support.MAX_ATTACHMENT:
                return _Upload(name, data)
    return None


def _is_automatic(msg, sender):
    auto = (msg.get('Auto-Submitted', 'no').lower() != 'no' or msg.get('X-Autoreply') or msg.get('X-Autorespond') or
            msg.get('Precedence', '').lower() in ('bulk', 'junk', 'list', 'auto_reply') or msg.get('List-Id'))
    ours = sender == email.utils.parseaddr(settings.get('MAIL_FROM') or 'support@happyhacks.io')[1].lower()
    daemon = re.match(r'^(mailer-daemon|postmaster|no-?reply|bounces?)[@+]', sender)
    return bool(auto or ours or daemon)


def take(raw):
    """One email -> ('reply', ticket) / ('new', ticket) / ('skip', reason)."""
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    mid = (msg.get('Message-ID') or '').strip()[:300] or None
    if mid and db().execute('SELECT 1 FROM mailin_seen WHERE message_id=?', (mid,)).fetchone():
        return 'skip', 'already taken'
    name, sender = email.utils.parseaddr(msg.get('Reply-To') or msg.get('From') or '')
    sender = sender.lower()
    subject = str(msg.get('Subject') or '').strip()
    result = ('skip', 'automatic or our own message')
    if sender and not _is_automatic(msg, sender):
        body = strip_quoted(_text(msg)) or '(no message text)'
        m = SUBJECT_NUMBER.search(subject)
        t = support.get(int(m.group(1))) if m else None
        if t and t['email'] == sender:
            support.customer_reply(t, body, _attachment(msg))
            result = ('reply', support.get(t['id']))
        elif support.valid_email(sender):
            if len(body) < 10:
                body = body + '\n\n(sent by email)'
            clean_subject = SUBJECT_NUMBER.sub('', re.sub(r'^\s*((re|fwd?|aw):\s*)+', '', subject, flags=re.I)).strip()
            t = support.create(sender, clean_subject, body, name=name, topic='other', ip='email',
                               upload=_attachment(msg), confirm=False)
            result = ('new', t)
    if mid:
        con = db()
        con.execute('INSERT OR IGNORE INTO mailin_seen (message_id, at, ticket_id) VALUES (?,?,?)',
                    (mid, time.time(), result[1]['id'] if result[0] != 'skip' else None))
        con.commit()
    return result


# ------------------------------------------------------------------ the mailbox
def check():
    """Read new messages since last time. -> how many became requests or replies."""
    host = settings.get('IMAP_HOST')
    port = int(settings.get('IMAP_PORT', 993))
    user = settings.get('IMAP_USER') or settings.get('SMTP_USER')
    password = settings.get('IMAP_PASSWORD') or settings.get('SMTP_PASSWORD')
    folder = settings.get('IMAP_FOLDER') or 'INBOX'
    taken = 0
    with imaplib.IMAP4_SSL(host, port, ssl_context=ssl.create_default_context(), timeout=30) as m:
        m.login(user, password or '')
        typ, data = m.select(f'"{folder}"', readonly=True)        # read-only: never changes the mailbox
        if typ != 'OK':
            raise RuntimeError(f'could not open {folder}')
        validity = int(m.untagged_responses.get('UIDVALIDITY', [b'0'])[0])
        con = db()
        row = con.execute('SELECT * FROM mailin_state WHERE folder=?', (folder,)).fetchone()
        if not row or row['uidvalidity'] != validity:
            # first run (or the mailbox was rebuilt): start from mail received in the last 2 days
            since = time.strftime('%d-%b-%Y', time.gmtime(time.time() - 2 * 86400))
            typ, data = m.uid('SEARCH', None, 'SINCE', since)
        else:
            typ, data = m.uid('SEARCH', None, f'UID {row["last_uid"] + 1}:*')
        uids = [int(u) for u in (data[0] or b'').split() if not row or int(u) > row['last_uid']]
        last = row['last_uid'] if row and row['uidvalidity'] == validity else 0
        for uid in sorted(uids)[:200]:
            typ, parts = m.uid('FETCH', str(uid), '(BODY.PEEK[])')    # PEEK: doesn't mark it read
            raw = next((p[1] for p in parts if isinstance(p, tuple)), None)
            if raw:
                try:
                    kind, _ = take(raw)
                    taken += kind != 'skip'
                except Exception:  # noqa: BLE001
                    log.exception('Support mailbox: could not take message %s', uid)
            last = max(last, uid)
        con.execute('INSERT INTO mailin_state (folder, uidvalidity, last_uid) VALUES (?,?,?) ON CONFLICT(folder) '
                    'DO UPDATE SET uidvalidity=excluded.uidvalidity, last_uid=excluded.last_uid', (folder, validity, last))
        con.commit()
    status.update(last_check=time.time(), last_error='', taken=status['taken'] + taken)
    return taken


def _loop():
    every = max(1.0, float(settings.get('MAILIN_MINUTES', 2))) * 60
    lock_path = os.path.join(settings.data_dir(), 'mailin.lock')
    try:
        import fcntl
        fh = open(lock_path, 'w')   # noqa: SIM115 - held for the life of the process
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)   # only one worker reads the mailbox
    except (ImportError, OSError):
        return
    while True:
        try:
            n = check()
            if n:
                log.info('Support mailbox: %d new request(s) or reply(ies)', n)
        except Exception as e:  # noqa: BLE001
            status.update(last_check=time.time(), last_error=str(e)[:300])
            log.warning('Support mailbox check failed: %s', e)
        time.sleep(every)


def start():
    """Start reading the mailbox in the background (once per process; one process wins the lock)."""
    global _started
    if not ready() or settings.flag('NO_MAILIN'):
        return
    with _lock:
        if _started:
            return
        _started = True
    threading.Thread(target=_loop, daemon=True, name='mailin').start()
