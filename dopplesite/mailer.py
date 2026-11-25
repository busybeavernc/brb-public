"""Email from the site (support tickets), over plain SMTP - works with Postmark, Amazon SES, Microsoft 365, Google, etc.

Settings (.env):
  SMTP_HOST, SMTP_PORT (587 = STARTTLS, 465 = TLS), SMTP_USER, SMTP_PASSWORD
  MAIL_FROM          the From address, e.g. "BRB Support <support@happyhacks.io>"
  SUPPORT_NOTIFY     comma-separated staff addresses told about new tickets and customer replies
If SMTP_HOST isn't set, nothing is sent (the site still works; tickets are answered in Admin and seen on the site).
"""
import logging
import smtplib
import ssl
import threading
from email.message import EmailMessage
from email.utils import make_msgid

from . import settings

log = logging.getLogger('dopplesite')
sent = []   # tests read this (only filled when SMTP isn't set up, i.e. never in production)


def ready():
    return bool(settings.get('SMTP_HOST'))


def from_address():
    return settings.get('MAIL_FROM') or 'BRB Support <support@happyhacks.io>'


def staff():
    return [a.strip() for a in str(settings.get('SUPPORT_NOTIFY', '')).split(',') if '@' in a]


def send(to, subject, body, reply_to=None, background=True):
    """Send a plain-text email. Never raises: a mail problem must not break the page that triggered it."""
    to = [t for t in ([to] if isinstance(to, str) else list(to or [])) if t and '@' in t]
    if not to:
        return False
    msg = EmailMessage()
    msg['From'] = from_address()
    msg['To'] = ', '.join(to)
    msg['Subject'] = subject
    msg['Message-ID'] = make_msgid(domain='brb.happyhacks.io')
    if reply_to:
        msg['Reply-To'] = reply_to
    msg.set_content(body)
    if not ready():
        sent.append({'to': to, 'subject': subject, 'body': body})
        del sent[:-50]
        log.info('Email not sent (SMTP not set up): %s -> %s', subject, to)
        return False
    if background:
        threading.Thread(target=_deliver, args=(msg,), daemon=True).start()
        return True
    return _deliver(msg)


def _deliver(msg):
    host, port = settings.get('SMTP_HOST'), int(settings.get('SMTP_PORT', 587))
    user, password = settings.get('SMTP_USER'), settings.get('SMTP_PASSWORD')
    try:
        ctx = ssl.create_default_context()
        if port == 465:
            s = smtplib.SMTP_SSL(host, port, timeout=20, context=ctx)
        else:
            s = smtplib.SMTP(host, port, timeout=20)
            s.starttls(context=ctx)
        with s:
            if user:
                s.login(user, password or '')
            s.send_message(msg)
        return True
    except Exception as e:  # noqa: BLE001
        log.warning('Email to %s failed: %s', msg['To'], e)
        return False
