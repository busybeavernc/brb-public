"""Emails about a purchase, from the site (besides Square's own receipt): a confirmation with the license key when a
subscription's key is made, and a note when a subscription is cancelled. Sent over the support mailbox's SMTP settings
(mailer.py); never blocks or breaks the purchase if mail isn't working."""
import time

from . import mailer, settings


def _date(ts):
    t = time.localtime(ts)
    return time.strftime('%B ', t) + str(t.tm_mday) + time.strftime(', %Y', t)


def _name(user):
    n = str((user or {}).get('name') or '').strip()
    return n.split()[0] if n and '@' not in n and not any(c.isdigit() for c in n) else ''


def confirmation(lic, user, what='', renews=None, on_computer=None, test=False):
    """lic: the new license; what: e.g. "$59.88 the first year, then $83.88/year"; renews: next renewal (timestamp);
    on_computer: the computer it was put on (name), if bought from BRB."""
    to = lic.get('email') or (user or {}).get('email')
    if not to:
        return False
    site = settings.public_url()
    hi = f'Hi {_name(user)},' if _name(user) else 'Hi,'
    lines = [hi, '',
             'Thank you for subscribing to BRB Unlimited: unlimited LOOP time, on one computer.' if not test else
             'This is a Square SANDBOX test purchase - no real money was charged. The test key below ends in 2 days.', '']
    if what:
        lines.append(f'Your plan: {what}' + (', renews automatically' if not test else ''))
    if renews and not test:
        lines.append(f'Next renewal: {_date(renews)}')
    lines += ['', f'Your license key:  {lic["key"]}', '']
    if on_computer:
        lines += [f'It is already active on {on_computer}: BRB switched to unlimited by itself. Nothing to type.', '']
    else:
        lines += ['To use it: in BRB, click Get License Key (or Settings > License), paste the key and click Apply.', '']
    lines += ['Manage your subscription:',
              f'  - Your computers (move the key to a new one): {site}/account',
              f'  - Billing and cancelling: {site}/license',
              f'  - Help: {site}/support (or just reply to this email)', '',
              "Square sends the payment receipt separately.", '', 'Thanks!', 'The BRB team at Happy Hacks']
    subject = 'Your BRB Unlimited license key' + (' (sandbox test)' if test else '')
    return mailer.send(to, subject, '\n'.join(lines))


def cancelled(lic, user=None):
    to = lic.get('email') or (user or {}).get('email')
    if not to:
        return False
    site = settings.public_url()
    end = lic.get('expires_at')
    hi = f'Hi {_name(user)},' if _name(user) else 'Hi,'
    lines = [hi, '', 'Your BRB Unlimited subscription is cancelled, and it will not renew.', '']
    if end and end > time.time():
        lines += [f'Your key (ending {lic["key"][-4:]}) keeps working until {_date(end)}. After that, BRB goes back to '
                  'free LOOP time.', '']
    lines += [f'Changed your mind? You can subscribe again any time: {site}/license',
              f'Questions: {site}/support (or just reply to this email)', '', 'Thanks for using BRB!',
              'The BRB team at Happy Hacks']
    return mailer.send(to, 'Your BRB subscription is cancelled', '\n'.join(lines))


def kept(lic, user, offer):
    """They took the "stay" offer."""
    to = lic.get('email') or (user or {}).get('email')
    if not to:
        return False
    site = settings.public_url()
    hi = f'Hi {_name(user)},' if _name(user) else 'Hi,'
    when = f' on {_date(lic["renews_at"])}' if lic.get('renews_at') else ''
    lines = [hi, '', 'Thank you for staying with BRB!', '',
             f'Your next renewal{when} is {offer["price"]} ({offer["pct"]}% off). After that, your subscription renews at '
             f'the regular {offer["then"]}/{offer["unit"]}.', '',
             'Your key and computer stay exactly as they are - nothing to do.', '',
             f'Manage your subscription any time: {site}/account',
             f'Questions: {site}/support (or just reply to this email)', '', 'The BRB team at Happy Hacks']
    return mailer.send(to, f'Your BRB discount: {offer["price"]} for your next renewal', '\n'.join(lines))
