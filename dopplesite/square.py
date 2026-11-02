"""BRB Unlimited: a $5/month Square subscription that comes with a license key (same Square setup as zipzip).

Settings (.env). SQUARE_ENV picks which set is used; both can sit in .env side by side:
    SQUARE_ENV=sandbox                              sandbox (test cards, no real money) or production
    SQUARE_SANDBOX_ACCESS_TOKEN=...                 Developer Dashboard > your app > Sandbox > Credentials
    SQUARE_SANDBOX_LOCATION_ID=...                  Sandbox > Locations (empty: the first location)
    SQUARE_SANDBOX_WEBHOOK_SIGNATURE_KEY=...        Sandbox > Webhooks > Subscriptions (the subscription's signature key)
    SQUARE_PRODUCTION_ACCESS_TOKEN=...              the same three for production
    SQUARE_PRODUCTION_LOCATION_ID=...
    SQUARE_PRODUCTION_WEBHOOK_SIGNATURE_KEY=...
    SQUARE_WEBHOOK_URL=https://brb.happyhacks.io/api/square/webhook   (the exact URL Square posts to)
    SUBSCRIPTION_PRICE_CENTS=500
    EARLY_PRICE_CENTS=250                            early-bird price: subscribing while the computer still has free time

The webhook subscription should send: subscription.created, subscription.updated, invoice.payment_made.

How it works
  - Subscribe: a Square payment link for the monthly plan (created in Square's catalog on first use). The person is
    signed in with Happy Hacks, so their email is filled in; when they come back (or Square's webhook arrives) their
    subscription is found and a license key is made for it, shown on /license.
  - Cancel (on /license, or in Square): the key keeps working until the end of the month already paid for.
"""
import base64
import hashlib
import hmac
import json
import logging
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from urllib.parse import quote

from . import licenses, settings, users

log = logging.getLogger('dopplesite')
API_VERSION = '2024-12-18'
HOSTS = {'sandbox': 'https://connect.squareupsandbox.com', 'production': 'https://connect.squareup.com'}
PLAN_NAME = 'BRB Unlimited'

SCHEMA = """
CREATE TABLE IF NOT EXISTS square_catalog (env TEXT NOT NULL, price_cents INTEGER NOT NULL, variation_id TEXT NOT NULL,
  created_at REAL NOT NULL, plan_name TEXT NOT NULL DEFAULT 'Doppleganger Unlimited', PRIMARY KEY (variation_id));
CREATE TABLE IF NOT EXISTS square_checkouts (id TEXT PRIMARY KEY, env TEXT NOT NULL, user_id TEXT NOT NULL, email TEXT NOT NULL,
  link_id TEXT, url TEXT, status TEXT NOT NULL DEFAULT 'open', created_at REAL NOT NULL,
  price_cents INTEGER NOT NULL DEFAULT 0, device_id TEXT);
"""
_ready = set()


class SquareError(Exception):
    def __init__(self, message, status=502):
        super().__init__(message)
        self.status = status


def db():
    con = licenses.db()
    if id(con) not in _ready:
        con.executescript(SCHEMA)
        cols = [r[1] for r in con.execute('PRAGMA table_info(square_catalog)')]
        if 'plan_name' not in cols:   # before the rename to BRB: one plan per price, keyed by (env, price)
            con.executescript("""ALTER TABLE square_catalog RENAME TO square_catalog_old;
              CREATE TABLE square_catalog (env TEXT NOT NULL, price_cents INTEGER NOT NULL, variation_id TEXT NOT NULL,
                created_at REAL NOT NULL, plan_name TEXT NOT NULL DEFAULT 'Doppleganger Unlimited', PRIMARY KEY (variation_id));
              INSERT INTO square_catalog (env, price_cents, variation_id, created_at)
                SELECT env, price_cents, variation_id, created_at FROM square_catalog_old;
              DROP TABLE square_catalog_old;""")
        _ready.add(id(con))
    return con


def env():
    e = str(settings.get('SQUARE_ENV', 'sandbox')).strip().lower()
    return e if e in HOSTS else 'sandbox'


def _setting(name):
    return settings.get(f'SQUARE_{env().upper()}_{name}') or (settings.get(f'SQUARE_{name}') if env() == 'production' else None)


def ready():
    return bool(_setting('ACCESS_TOKEN'))


def price_cents():
    try:
        return int(settings.get('SUBSCRIPTION_PRICE_CENTS', 500))
    except ValueError:
        return 500


def early_price_cents():
    try:
        return int(settings.get('EARLY_PRICE_CENTS', price_cents() // 2))
    except ValueError:
        return price_cents() // 2


def price_text(cents=None):
    c = price_cents() if cents is None else cents
    return f'${c // 100}' if c % 100 == 0 else f'${c / 100:.2f}'


def call(method, path, body=None):
    if not ready():
        raise SquareError("Subscriptions aren't set up yet. Please check back soon.", 501)
    req = urllib.request.Request(HOSTS[env()] + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={'Authorization': f'Bearer {_setting("ACCESS_TOKEN")}', 'Square-Version': API_VERSION,
                                          'Accept': 'application/json', 'Content-Type': 'application/json',
                                          'User-Agent': 'doppleganger-site'})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return json.loads(r.read() or b'{}')
    except urllib.error.HTTPError as e:
        detail = e.read()[:400].decode('utf-8', 'replace')
        log.warning('Square %s %s (%s): HTTP %s %s', method, path, env(), e.code, detail)
        raise SquareError(f"Square ({env()}) didn't accept that (HTTP {e.code}).") from e
    except (urllib.error.URLError, OSError) as e:
        raise SquareError("Square couldn't be reached. Please try again in a moment.") from e


def _key():
    return str(uuid.uuid4())


def location():
    loc = _setting('LOCATION_ID')
    if loc:
        return loc
    locs = call('GET', '/v2/locations').get('locations') or []
    if not locs:
        raise SquareError('This Square account has no locations.')
    return locs[0]['id']


def plan_variation(cents=None):
    """The monthly plan in Square's catalog at this price (created there the first time)."""
    cents = price_cents() if cents is None else int(cents)
    row = db().execute('SELECT variation_id FROM square_catalog WHERE env=? AND price_cents=? AND plan_name=?',
                       (env(), cents, PLAN_NAME)).fetchone()
    if row:
        return row['variation_id']
    plan = call('POST', '/v2/catalog/object', {'idempotency_key': _key(), 'object': {
        'type': 'SUBSCRIPTION_PLAN', 'id': '#plan', 'subscription_plan_data': {'name': PLAN_NAME}}})['catalog_object']
    var = call('POST', '/v2/catalog/object', {'idempotency_key': _key(), 'object': {
        'type': 'SUBSCRIPTION_PLAN_VARIATION', 'id': '#variation', 'subscription_plan_variation_data': {
            'name': f'{PLAN_NAME} monthly ({price_text(cents)})', 'subscription_plan_id': plan['id'],
            'phases': [{'cadence': 'MONTHLY', 'ordinal': 0,
                        'pricing': {'type': 'STATIC', 'price_money': {'amount': cents, 'currency': 'USD'}}}]}}})['catalog_object']
    con = db()
    con.execute('INSERT INTO square_catalog (env, price_cents, variation_id, created_at, plan_name) VALUES (?,?,?,?,?)',
                (env(), cents, var['id'], time.time(), PLAN_NAME))
    con.commit()
    return var['id']


def checkout(user, return_url, early_device=None):
    """A Square checkout page for the monthly plan (at the early-bird price for early_device). -> url"""
    if not user.get('email'):
        raise SquareError('Add an email address to your Happy Hacks account first (Square sends receipts there).', 400)
    cid = 'sq' + uuid.uuid4().hex[:16]
    cents = early_price_cents() if early_device else price_cents()
    name = f'{PLAN_NAME} (early-bird)' if early_device else PLAN_NAME
    body = {'idempotency_key': _key(),
            'quick_pay': {'name': name, 'price_money': {'amount': cents, 'currency': 'USD'}, 'location_id': location()},
            'checkout_options': {'redirect_url': f'{return_url}?c={cid}', 'ask_for_shipping_address': False,
                                 'subscription_plan_id': plan_variation(cents)},
            'pre_populated_data': {'buyer_email': user['email']},
            'payment_note': f'BRB {user["id"]} {cid}'}
    link = call('POST', '/v2/online-checkout/payment-links', body).get('payment_link') or {}
    if not link.get('url'):
        raise SquareError("Square didn't return a checkout page.")
    con = db()
    con.execute('INSERT INTO square_checkouts (id, env, user_id, email, link_id, url, created_at, price_cents, device_id) '
                'VALUES (?,?,?,?,?,?,?,?,?)', (cid, env(), user['id'], user['email'].lower(), link.get('id'), link['url'][:500],
                                               time.time(), cents, early_device['id'] if early_device else None))
    con.commit()
    return link['url']


# ------------------------------------------------------------------ subscriptions -> license keys
def _day_end(d):
    """Square dates ('2026-12-01') -> the end of that day (UTC), as a timestamp."""
    try:
        y, m, dd = (int(x) for x in str(d)[:10].split('-'))
        return datetime(y, m, dd, 23, 59, 59, tzinfo=timezone.utc).timestamp()
    except (ValueError, TypeError):
        return None


def _expires_for(sub):
    st = (sub.get('status') or '').upper()
    if st in ('CANCELED', 'DEACTIVATED', 'PAUSED'):
        return _day_end(sub.get('charged_through_date') or sub.get('canceled_date')) or time.time()
    if sub.get('canceled_date'):   # cancelled, still paid up until then
        return _day_end(sub.get('charged_through_date') or sub.get('canceled_date'))
    return None


def apply(sub, user=None):
    """Make or update the license key for a Square subscription. -> the license, or None if it isn't ours."""
    if not sub.get('id'):
        return None
    con = db()
    row = con.execute('SELECT id FROM licenses WHERE square_subscription_id=?', (sub['id'],)).fetchone()
    expires = _expires_for(sub)
    if row:
        return licenses.update(row['id'], square_status=(sub.get('status') or '').upper(), expires_at=expires)
    var_ok = con.execute('SELECT 1 FROM square_catalog WHERE variation_id=?', (sub.get('plan_variation_id'),)).fetchone()
    if not var_ok:
        return None
    if user is None:
        user = _user_for_customer(sub.get('customer_id'))
    if user is None:
        return None
    var = con.execute('SELECT price_cents FROM square_catalog WHERE variation_id=?', (sub.get('plan_variation_id'),)).fetchone()
    early = var and var['price_cents'] < price_cents()
    lic = licenses.create(plan='subscription', max_devices=1, expires_at=expires, email=user.get('email') or '',
                          user_id=user['id'], by='square', square_subscription_id=sub['id'],
                          square_status=(sub.get('status') or '').upper(),
                          note='Square subscription' + (f' (early-bird {price_text(var["price_cents"])})' if early else ''))
    # the computer that bought it at the early-bird price goes straight onto the key
    ck = con.execute("SELECT device_id FROM square_checkouts WHERE user_id=? AND status='open' AND device_id IS NOT NULL "
                     'ORDER BY created_at DESC', (user['id'],)).fetchone()
    if ck and early:
        licenses.bind_device(lic['id'], ck['device_id'], discount=True)
    con.execute("UPDATE square_checkouts SET status='done' WHERE user_id=? AND status='open'", (user['id'],))
    con.commit()
    return licenses.get(lic['id'])


def _customer_email(customer_id):
    if not customer_id:
        return ''
    c = call('GET', f'/v2/customers/{quote(customer_id)}').get('customer') or {}
    return str(c.get('email_address') or '').lower()


def _user_for_customer(customer_id):
    email = _customer_email(customer_id)
    if not email:
        return None
    row = users.db().execute('SELECT id FROM users WHERE email=?', (email,)).fetchone()
    if row:
        return users.get(row['id'])
    row = db().execute('SELECT user_id FROM square_checkouts WHERE email=? ORDER BY created_at DESC', (email,)).fetchone()
    return users.get(row['user_id']) if row else None


def settle(user):
    """Look this person's subscriptions up in Square now (when they come back from checkout, before the webhook)."""
    if not ready() or not user.get('email'):
        return []
    found = call('POST', '/v2/customers/search', {'query': {'filter': {'email_address': {'exact': user['email']}}}, 'limit': 10})
    ids = [c['id'] for c in found.get('customers') or []]
    if not ids:
        return []
    subs = call('POST', '/v2/subscriptions/search', {'query': {'filter': {'customer_ids': ids}}}).get('subscriptions') or []
    return [lic for lic in (apply(s, user) for s in subs) if lic]


def cancel(lic):
    """Cancel at the end of the paid month. -> the license (with expires_at set)."""
    if not lic.get('square_subscription_id'):
        raise SquareError('This key is not a subscription.', 400)
    sub = call('POST', f'/v2/subscriptions/{quote(lic["square_subscription_id"])}/cancel', {}).get('subscription') or {}
    return apply(sub) or licenses.get(lic['id'])


# ------------------------------------------------------------------ webhooks
def verify(raw_body, signature, url):
    """Square signs each notice: base64(HMAC-SHA256(signature key, notification URL + body))."""
    key = _setting('WEBHOOK_SIGNATURE_KEY')
    if not key or not signature:
        return False
    want = base64.b64encode(hmac.new(key.encode(), url.encode() + raw_body, hashlib.sha256).digest()).decode()
    return hmac.compare_digest(want, signature)


def handle(event):
    kind = event.get('type') or ''
    obj = (event.get('data') or {}).get('object') or {}
    if kind in ('subscription.created', 'subscription.updated'):
        lic = apply(obj.get('subscription') or {})
        return f'license {lic["key"][-4:]} {lic.get("square_status")}' if lic else 'not one of ours'
    if kind == 'invoice.payment_made':
        sid = (obj.get('invoice') or {}).get('subscription_id')
        if sid:
            sub = call('GET', f'/v2/subscriptions/{quote(sid)}').get('subscription') or {}
            lic = apply(sub)
            return 'month paid' if lic else 'not one of ours'
    return 'ignored'
