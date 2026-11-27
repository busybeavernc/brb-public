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
              'Your subscription renews automatically until you cancel. All payments are final: no refunds or partial',
              "refunds. Cancel any time on your account page to stop the next renewal; your key keeps working until the",
              "end of the period you've paid for.", '',
              "Square sends the payment receipt separately.", '', 'Thanks!', 'The BRB team at Happy Hacks']
    subject = 'Your BRB Unlimited license key' + (' (sandbox test)' if test else '')
    return mailer.send(to, subject, '\n'.join(lines))


def cancelled(lic, user=None):
    """Our own confirmation when a subscription is cancelled (Square sends its own short notice as well)."""
    to = lic.get('email') or (user or {}).get('email')
    if not to:
        return False
    site = settings.public_url()
    end = lic.get('expires_at')
    hi = f'Hi {_name(user)},' if _name(user) else 'Hi,'
    lines = [hi, '', 'This confirms that your BRB Unlimited subscription has been cancelled. It will not renew, and you '
             'will not be charged again.', '']
    if end and end > time.time():
        lines += [f"You've already paid for the current period, so your subscription stays active until the end of it: "
                  f'{_date(end)}. Until then, your key (ending {lic["key"][-4:]}) keeps working as usual, with unlimited '
                  'LOOP time. After that, BRB goes back to free LOOP time.', '']
    else:
        lines += [f'Your key (ending {lic["key"][-4:]}) has stopped giving unlimited LOOP time; BRB is back to free '
                  'LOOP time.', '']
    lines += ['You should also get a separate email from Square confirming the cancellation. That one is from our '
              "payment processor, so there's nothing more you need to do.", '',
              'Thank you for your business. We really appreciate you giving BRB a try, and we hope to see you again. '
              f'You can subscribe again any time: {site}/license', '',
              f'Questions, or something we could do better? {site}/support (or just reply to this email)', '',
              'Thanks again!', 'The BRB team at Happy Hacks']
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
             *([SQUARE_NOTICE, ''] if offer.get('how') != 'swap' else []),
             f'Manage your subscription any time: {site}/account',
             f'Questions: {site}/support (or just reply to this email)', '', 'The BRB team at Happy Hacks']
    return mailer.send(to, f'Your BRB discount: {offer["price"]} for your next renewal', '\n'.join(lines))


def _pay_lines(amount, url, site):
    return [f'Pay the {amount} now (you can use a different card, and save it for future renewals):', f'  {url}' if url else
            f'  {site}/account', '']


def payment_failed(lic, user, amount, url, works_until):
    to = lic.get('email') or (user or {}).get('email')
    if not to:
        return False
    site = settings.public_url()
    hi = f'Hi {_name(user)},' if _name(user) else 'Hi,'
    lines = [hi, '', f"Your BRB Unlimited renewal of {amount} didn't go through - usually an expired card or a bank "
             'check.', ''] + _pay_lines(amount, url, site) + [
             f'Your key keeps working until {_date(works_until)} while you sort it out. After that, unlimited LOOP time '
             'pauses until the payment goes through, and switches back on by itself when it does.', '',
             f'Your account: {site}/account', f'Questions: {site}/support (or just reply to this email)', '',
             'The BRB team at Happy Hacks']
    return mailer.send(to, "BRB: your payment didn't go through", '\n'.join(lines))


def payment_reminder(lic, user, amount, url, works_until):
    to = lic.get('email') or (user or {}).get('email')
    if not to:
        return False
    site = settings.public_url()
    hi = f'Hi {_name(user)},' if _name(user) else 'Hi,'
    lines = [hi, '', f'A reminder: your BRB Unlimited renewal of {amount} is still unpaid, and unlimited LOOP time pauses '
             f'on {_date(works_until)}.', ''] + _pay_lines(amount, url, site) + ['The BRB team at Happy Hacks']
    return mailer.send(to, 'BRB: unlimited LOOP time pauses soon', '\n'.join(lines))


def payment_paused(lic, user, amount, url, cancel_on):
    to = lic.get('email') or (user or {}).get('email')
    if not to:
        return False
    site = settings.public_url()
    hi = f'Hi {_name(user)},' if _name(user) else 'Hi,'
    lines = [hi, '', f'Your BRB Unlimited renewal of {amount} is still unpaid, so unlimited LOOP time is paused (BRB '
             'goes back to free time).', ''] + _pay_lines(amount, url, site) + [
             'It switches back on by itself as soon as the payment goes through.',
             f'If it stays unpaid, your subscription is cancelled on {_date(cancel_on)}.', '',
             'The BRB team at Happy Hacks']
    return mailer.send(to, 'BRB: unlimited LOOP time is paused', '\n'.join(lines))


def payment_recovered(lic, user):
    to = lic.get('email') or (user or {}).get('email')
    if not to:
        return False
    hi = f'Hi {_name(user)},' if _name(user) else 'Hi,'
    lines = [hi, '', 'Thanks - your BRB Unlimited payment went through, and unlimited LOOP time is on again.',
             f'Next renewal: {_date(lic["renews_at"])}' if lic.get('renews_at') else '', '', 'The BRB team at Happy Hacks']
    return mailer.send(to, 'BRB: payment received, all set', '\n'.join(lines))


SQUARE_NOTICE = ('You may also get an email from Square saying your subscription was "cancelled" or that a new one was '
                 'created. That is just Square moving your subscription to a new billing record: nothing else changes - '
                 'same key, same card, and you have nothing to do.')


def moved_to_regular(lic, user, price_words, starts):
    """The promo safeguard moved them onto the regular price (Square emails them about a "cancelled" subscription)."""
    to = lic.get('email') or (user or {}).get('email')
    if not to:
        return False
    site = settings.public_url()
    hi = f'Hi {_name(user)},' if _name(user) else 'Hi,'
    lines = [hi, '', 'Your BRB Unlimited subscription continues as usual - this is just a heads-up.', '',
             f'Your introductory price ends with your current period. From {_date(starts)}, your subscription renews at '
             f'the regular {price_words}, as agreed when you subscribed.', '', SQUARE_NOTICE, '',
             f'Manage your subscription any time: {site}/account (cancel there before {_date(starts)} if you don\'t want '
             'to renew).', f'Questions: {site}/support (or just reply to this email)', '', 'The BRB team at Happy Hacks']
    return mailer.send(to, 'Your BRB subscription continues (about the email from Square)', '\n'.join(lines))
