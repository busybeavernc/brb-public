"""Email from the site (support tickets), over plain SMTP - works with Postmark, Amazon SES, Microsoft 365, Google, etc.

Settings (.env):
  SMTP_HOST, SMTP_PORT (587 = STARTTLS, 465 = TLS), SMTP_USER, SMTP_PASSWORD
  Who emails come from (the SMTP service must allow them: in SMTP2GO, verify the whole happyhacks.io domain):
    MAIL_FROM            support requests: confirmations and replies      (default BRB Support <support@happyhacks.io>)
    MAIL_FROM_ACCOUNTS   license key, subscription and payment emails     (default BRB <accounts@happyhacks.io>;
                         replies go to the support address)
    MAIL_FROM_ALERTS     staff and admin alerts                           (default BRB Alerts <no-reply@happyhacks.io>)
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


def from_address(kind='support'):
    if kind == 'account':
        return settings.get('MAIL_FROM_ACCOUNTS') or 'BRB <accounts@happyhacks.io>'
    if kind == 'alert':
        return settings.get('MAIL_FROM_ALERTS') or 'BRB Alerts <no-reply@happyhacks.io>'
    return settings.get('MAIL_FROM') or 'BRB Support <support@happyhacks.io>'


def our_addresses():
    """Every address the site sends from (lower case), so mail from ourselves is never taken in as a request."""
    from email.utils import parseaddr
    return {parseaddr(from_address(k))[1].lower() for k in ('support', 'account', 'alert')}


def staff():
    return [a.strip() for a in str(settings.get('SUPPORT_NOTIFY', '')).split(',') if '@' in a]


def send(to, subject, body, reply_to=None, background=True, kind='support'):
    """Send a plain-text email. Never raises: a mail problem must not break the page that triggered it.
    kind: 'support' (requests), 'account' (license and billing; replies go to support) or 'alert' (staff)."""
    to = [t for t in ([to] if isinstance(to, str) else list(to or [])) if t and '@' in t]
    if not to:
        return False
    msg = EmailMessage()
    msg['From'] = from_address(kind)
    msg['To'] = ', '.join(to)
    msg['Subject'] = subject
    msg['Message-ID'] = make_msgid(domain='brb.happyhacks.io')
    if reply_to or kind == 'account':
        msg['Reply-To'] = reply_to or from_address('support')
    msg.set_content(body)
    if not ready():
        sent.append({'to': to, 'subject': subject, 'body': body, 'from': msg['From'], 'reply_to': msg['Reply-To']})
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
