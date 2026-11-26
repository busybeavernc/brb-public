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
    MONTHLY_PRICE_CENTS=999                          month to month
    ANNUAL_PRICE_CENTS=8388                          billed once a year ($6.99/month)
    PROMO_ANNUAL_PRICE_CENTS=5988                    a special first-year price on the annual plan ($4.99/month)...
    PROMO_ENDS=2026-12-31                            ...for orders up to the end of this day (Eastern); off = no promo
    PROMO_NAME=Holiday special

The webhook subscription should send: subscription.created, subscription.updated, invoice.payment_made.

How it works
  - Square's catalog is kept in step with these settings (sync_catalog): one "BRB Unlimited" subscription plan with a
    variation per option sold (monthly, annual, and the promo's annual: first year at the promo price, then the annual
    price) - checked after every start-up and price change,
    and from Admin > Overview > Square subscriptions. Nothing needs setting up by hand in Square.
  - Subscribe: a Square payment link for the monthly plan. The person is
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
  created_at REAL NOT NULL, plan_name TEXT NOT NULL DEFAULT 'Doppleganger Unlimited', spec TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (variation_id));
CREATE TABLE IF NOT EXISTS square_checkouts (id TEXT PRIMARY KEY, env TEXT NOT NULL, user_id TEXT NOT NULL, email TEXT NOT NULL,
  link_id TEXT, url TEXT, status TEXT NOT NULL DEFAULT 'open', created_at REAL NOT NULL,
  price_cents INTEGER NOT NULL DEFAULT 0, device_id TEXT);
CREATE TABLE IF NOT EXISTS square_replacements (old_sub_id TEXT PRIMARY KEY, new_sub_id TEXT, license_id TEXT NOT NULL,
  reason TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS cancel_feedback (id INTEGER PRIMARY KEY AUTOINCREMENT, license_id TEXT NOT NULL, user_id TEXT,
  email TEXT NOT NULL DEFAULT '', plan TEXT NOT NULL DEFAULT '', reason TEXT NOT NULL DEFAULT '',
  feedback TEXT NOT NULL DEFAULT '', outcome TEXT NOT NULL DEFAULT 'started', created_at REAL NOT NULL, updated_at REAL);
"""
_ready = set()


class SquareError(Exception):
    def __init__(self, message, status=502):
        super().__init__(message)
        self.status = status


def db():
    con = licenses.db()
    key = (id(con), users.path())   # a connection's id can be reused by a new one (e.g. a new database in tests)
    if key not in _ready:
        con.executescript(SCHEMA)
        cols = [r[1] for r in con.execute('PRAGMA table_info(square_catalog)')]
        if cols and 'spec' not in cols and 'plan_name' in cols:   # one row per option sold (its pricing as text)
            con.execute("ALTER TABLE square_catalog ADD COLUMN spec TEXT NOT NULL DEFAULT ''")
            con.execute("UPDATE square_catalog SET spec='MONTHLY:' || price_cents")
            con.commit()
        if 'plan_name' not in cols:   # before the rename to BRB: one plan per price, keyed by (env, price)
            con.executescript("""ALTER TABLE square_catalog RENAME TO square_catalog_old;
              CREATE TABLE square_catalog (env TEXT NOT NULL, price_cents INTEGER NOT NULL, variation_id TEXT NOT NULL,
                created_at REAL NOT NULL, plan_name TEXT NOT NULL DEFAULT 'Doppleganger Unlimited',
                spec TEXT NOT NULL DEFAULT '', PRIMARY KEY (variation_id));
              INSERT INTO square_catalog (env, price_cents, variation_id, created_at, spec)
                SELECT env, price_cents, variation_id, created_at, 'MONTHLY:' || price_cents FROM square_catalog_old;
              DROP TABLE square_catalog_old;""")
        _ready.add(key)
    return con


def env():
    e = str(settings.get('SQUARE_ENV', 'sandbox')).strip().lower()
    return e if e in HOSTS else 'sandbox'


def _setting(name):
    return settings.get(f'SQUARE_{env().upper()}_{name}') or (settings.get(f'SQUARE_{name}') if env() == 'production' else None)


def ready():
    return bool(_setting('ACCESS_TOKEN'))


def _cents(name, default):
    try:
        return max(0, int(float(settings.get(name, default))))
    except (TypeError, ValueError):
        return default


def monthly_cents():
    return _cents('MONTHLY_PRICE_CENTS', 999)


def annual_cents():
    return _cents('ANNUAL_PRICE_CENTS', 8388)


def promo_cents():
    return _cents('PROMO_ANNUAL_PRICE_CENTS', 5988)


def promo_name():
    return str(settings.get('PROMO_NAME', 'Holiday special') or 'Holiday special')


def promo_ends():
    """The promo's last day as a date, or None (no promo)."""
    raw = str(settings.get('PROMO_ENDS', '2026-12-31') or '').strip()   # 'off' = no promo
    try:
        return datetime.strptime(raw[:10], '%Y-%m-%d').date()
    except ValueError:
        return None


def promo_active(now=None):
    """The first-year annual promo is on: it ends at midnight Eastern after PROMO_ENDS."""
    end = promo_ends()
    if end is None or not 0 < promo_cents() < annual_cents():
        return False
    from zoneinfo import ZoneInfo
    stop = datetime(end.year, end.month, end.day, 23, 59, 59, tzinfo=ZoneInfo('America/New_York')).timestamp()
    return (time.time() if now is None else now) <= stop


def per_month(cents_a_year):
    return price_text(int(round(cents_a_year / 12)))


# (older names, still used by a few pages and the app's status)
def price_cents():
    return monthly_cents()


def early_price_cents():
    """The lowest monthly-equivalent price on offer right now."""
    return int(round((promo_cents() if promo_active() else annual_cents()) / 12))


def pricing():
    """The numbers the pages show."""
    end = promo_ends()
    on = promo_active()
    return {'monthly': price_text(monthly_cents()), 'annual_month': per_month(annual_cents()),
            'annual_total': price_text(annual_cents()), 'promo': on, 'promo_name': promo_name(),
            'promo_month': per_month(promo_cents()), 'promo_total': price_text(promo_cents()),
            'promo_ends': f'{end:%b} {end.day}' if end else '',
            'from': per_month(promo_cents() if on else annual_cents()),
            'save_pct': round(100 * (1 - annual_cents() / (12 * monthly_cents()))) if monthly_cents() else 0}


def offers():
    """What's for sale now, by option key. Each: label, phases [(cadence, cents, periods or None)], first payment."""
    out = {'annual': {'label': 'Annual', 'phases': [('ANNUAL', annual_cents(), None)], 'first': annual_cents()},
           'monthly': {'label': 'Monthly', 'phases': [('MONTHLY', monthly_cents(), None)], 'first': monthly_cents()}}
    if promo_active():
        out['annual'] = {'label': f'Annual, {promo_name().lower()}', 'promo': True,
                         'phases': [('ANNUAL', promo_cents(), 1), ('ANNUAL', annual_cents(), None)],
                         'first': promo_cents()}
    return out


def retention_pct():
    """The "stay" offer when someone cancels: this % off their next renewal (RETENTION_PCT, 0 = no offer)."""
    return max(0, min(90, _cents('RETENTION_PCT', 30)))


def stay_offers():
    """Variations for the "stay" offer: the next billing period at RETENTION_PCT off, then the normal price."""
    pct = retention_pct()
    if not pct:
        return {}
    out = {}
    for cad, base, label in (('MONTHLY', monthly_cents(), 'monthly'), ('ANNUAL', annual_cents(), 'annual')):
        disc = int(round(base * (100 - pct) / 100))
        out['stay_' + label] = {'label': f'Stay offer, {label} ({pct}% off one renewal)', 'stay': True, 'cadence': cad,
                                'phases': [(cad, disc, 1), (cad, base, None)], 'first': disc, 'then': base}
    return out


def _spec(phases):
    """A variation's pricing, as text: 'ANNUAL:5988x1|ANNUAL:8388'."""
    return '|'.join(f'{c}:{a}' + (f'x{n}' if n else '') for c, a, n in phases)


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


def wanted_prices():
    """(The first payment of each option on sale.)"""
    return [o['first'] for o in offers().values()]


def _variation_spec(v):
    phases = sorted((v.get('subscription_plan_variation_data') or {}).get('phases') or [],
                    key=lambda ph: ph.get('ordinal') or 0)
    out = []
    for ph in phases:
        money = ((ph.get('pricing') or {}).get('price_money')) or ph.get('recurring_price_money') or {}
        if (money.get('currency') or 'USD') != 'USD' or money.get('amount') is None:
            return None
        out.append(((ph.get('cadence') or '').upper(), int(money['amount']), ph.get('periods')))
    return _spec(out) if out else None


def _variation_price(v):
    """(First phase's price.)"""
    spec = _variation_spec(v)
    return int(spec.split('|')[0].split(':')[1].split('x')[0]) if spec else None


def _catalog_plans():
    """Square's BRB plans and their variations: [(plan, [variation, ...])]."""
    plans, variations, cursor = [], [], None
    while True:
        body = {'object_types': ['SUBSCRIPTION_PLAN'], 'include_related_objects': True}
        if cursor:
            body['cursor'] = cursor
        r = call('POST', '/v2/catalog/search', body)
        plans += [o for o in r.get('objects') or [] if not o.get('is_deleted')]
        variations += [o for o in r.get('related_objects') or []
                       if o.get('type') == 'SUBSCRIPTION_PLAN_VARIATION' and not o.get('is_deleted')]
        cursor = r.get('cursor')
        if not cursor:
            break
    ours = [p for p in plans if (p.get('subscription_plan_data') or {}).get('name') == PLAN_NAME]
    by_plan = {p['id']: [] for p in ours}
    for v in variations:
        pid = (v.get('subscription_plan_variation_data') or {}).get('subscription_plan_id')
        if pid in by_plan:
            by_plan[pid].append(v)
    for p in ours:   # variations Square nests in the plan itself
        for v in (p.get('subscription_plan_data') or {}).get('subscription_plan_variations') or []:
            if not v.get('is_deleted') and all(v['id'] != x['id'] for x in by_plan[p['id']]):
                by_plan[p['id']].append(v)
    return [(p, by_plan[p['id']]) for p in ours]


sync_status = {'at': None, 'error': '', 'report': None}


def sync_catalog():
    """Make Square's catalog match the website: one "BRB Unlimited" subscription plan with a variation for each option
    on sale (see offers). Safe to run any time: it reuses what's already in Square and only adds what's missing. A price
    change adds a new variation; the old one stays, so existing subscribers keep their price (Square doesn't move
    them) - it's just no longer offered. -> report"""
    try:
        found = _catalog_plans()
        plan = max(found, key=lambda pv: len(pv[1]))[0] if found else None
        created = []
        if plan is None:
            plan = call('POST', '/v2/catalog/object', {'idempotency_key': _key(), 'object': {
                'type': 'SUBSCRIPTION_PLAN', 'id': '#plan', 'subscription_plan_data': {'name': PLAN_NAME}}})['catalog_object']
            found = [(plan, [])]
            created.append(f'plan "{PLAN_NAME}"')
        have = {}
        for _p, vs in found:
            for v in vs:
                spec = _variation_spec(v)
                if spec:
                    have.setdefault(spec, v['id'])
        con = db()
        offered, wanted = [], set()
        for key, o in {**offers(), **stay_offers()}.items():
            spec = _spec(o['phases'])
            wanted.add(spec)
            vid = have.get(spec)
            if not vid:
                var = call('POST', '/v2/catalog/object', {'idempotency_key': _key(), 'object': {
                    'type': 'SUBSCRIPTION_PLAN_VARIATION', 'id': '#variation', 'subscription_plan_variation_data': {
                        'name': f'{o["label"]} ({describe(o)})'[:255], 'subscription_plan_id': plan['id'],
                        'phases': [{'cadence': c, 'ordinal': i, **({'periods': n} if n else {}), 'pricing': {
                            'type': 'STATIC', 'price_money': {'amount': a, 'currency': 'USD'}}}
                                   for i, (c, a, n) in enumerate(o['phases'])]}}})['catalog_object']
                vid = var['id']
                created.append(f'{o["label"]}: {describe(o)}')
            con.execute('INSERT OR REPLACE INTO square_catalog (env, price_cents, variation_id, created_at, plan_name, spec) '
                        'VALUES (?,?,?,?,?,?)', (env(), o['first'], vid, time.time(), PLAN_NAME, spec))
            offered.append({'key': key, 'label': o['label'], 'price': describe(o), 'variation_id': vid})
        con.commit()
        older = sorted(sp for sp in have if sp not in wanted)
        report = {'env': env(), 'plan_id': plan['id'], 'plan': PLAN_NAME, 'offered': offered, 'created': created,
                  'older_prices': [_spec_words(sp) for sp in older], 'duplicate_plans': max(0, len(found) - 1)}
        sync_status.update(at=time.time(), error='', report=report)
        if created:
            log.info('Square catalog (%s): added %s', env(), ', '.join(created))
        return report
    except SquareError as e:
        sync_status.update(at=time.time(), error=str(e))
        raise


def describe(o):
    """'$9.99/month', '$83.88/year', '$59.88 the first year, then $83.88/year'."""
    return _spec_words(_spec(o['phases']))


def _spec_words(spec):
    unit = {'MONTHLY': 'month', 'ANNUAL': 'year'}
    parts = []
    for ph in spec.split('|'):
        cad, rest = ph.split(':')
        amt, _, n = rest.partition('x')
        parts.append((unit.get(cad, cad.lower()), int(amt), int(n) if n else None))
    if len(parts) == 2 and parts[0][2] == 1:
        return f'{price_text(parts[0][1])} the first {parts[0][0]}, then {price_text(parts[1][1])}/{parts[1][0]}'
    return ', then '.join(f'{price_text(a)}/{u}' for u, a, _n in parts)


def plan_variation(option='annual'):
    """The Square plan variation for an option on sale now ('monthly' / 'annual'); the catalog is brought up to date
    if needed."""
    o = offers().get(option)
    if o is None:
        raise SquareError("That option isn't offered. Please reload the page.", 409)
    spec = _spec(o['phases'])
    q = 'SELECT variation_id FROM square_catalog WHERE env=? AND spec=? AND plan_name=?'
    row = db().execute(q, (env(), spec, PLAN_NAME)).fetchone()
    if row:
        return row['variation_id']
    sync_catalog()
    row = db().execute(q, (env(), spec, PLAN_NAME)).fetchone()
    if not row:
        raise SquareError("That option isn't offered any more. Please reload the page.", 409)
    return row['variation_id']


_synced_for = None


def sync_in_background():
    """Once per set of prices (startup, or after the .env prices change): bring Square's catalog up to date."""
    global _synced_for
    want = (env(), tuple(sorted(_spec(o['phases']) for o in {**offers(), **stay_offers()}.values())), bool(ready()))
    if _synced_for == want or not ready() or settings.get('NO_AUTO_BACKUP'):   # (tests: no background work)
        return
    _synced_for = want
    import threading

    def run():
        try:
            sync_catalog()
        except Exception:  # noqa: BLE001
            log.exception('Square catalog sync')
    threading.Thread(target=run, daemon=True, name='square-sync').start()


def checkout(user, return_url, option='annual', device=None):
    """A Square checkout page for an option on sale ('annual' - with the promo while it runs - or 'monthly'). The key
    is put on `device` (the computer whose BRB opened the page) when the purchase goes through. -> url"""
    if not user.get('email'):
        raise SquareError('Add an email address to your Happy Hacks account first (Square sends receipts there).', 400)
    o = offers().get(option)
    if o is None:
        raise SquareError("That option isn't offered. Please reload the page.", 400)
    cid = 'sq' + uuid.uuid4().hex[:16]
    body = {'idempotency_key': _key(),
            'quick_pay': {'name': f'{PLAN_NAME} - {o["label"]}', 'price_money': {'amount': o['first'], 'currency': 'USD'},
                          'location_id': location()},
            'checkout_options': {'redirect_url': f'{return_url}?c={cid}', 'ask_for_shipping_address': False,
                                 'subscription_plan_id': plan_variation(option)},
            'pre_populated_data': {'buyer_email': user['email']},
            'payment_note': f'BRB {user["id"]} {cid}'}
    link = call('POST', '/v2/online-checkout/payment-links', body).get('payment_link') or {}
    if not link.get('url'):
        raise SquareError("Square didn't return a checkout page.")
    con = db()
    con.execute('INSERT INTO square_checkouts (id, env, user_id, email, link_id, url, created_at, price_cents, device_id) '
                'VALUES (?,?,?,?,?,?,?,?,?)', (cid, env(), user['id'], user['email'].lower(), link.get('id'), link['url'][:500],
                                               time.time(), o['first'], device['id'] if device else None))
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
    rep = con.execute('SELECT license_id FROM square_replacements WHERE old_sub_id=?', (sub['id'],)).fetchone()
    if rep:   # an old subscription BRB replaced (the "stay" offer): its cancellation doesn't touch the key
        return licenses.get(rep['license_id'])
    row = con.execute('SELECT id FROM licenses WHERE square_subscription_id=?', (sub['id'],)).fetchone()
    if not row:
        for _ in range(10):   # BRB may be making this very subscription right now: let it record the id first
            busy = con.execute('SELECT 1 FROM square_replacements WHERE new_sub_id IS NULL AND created_at > ?',
                               (time.time() - 120,)).fetchone()
            if not busy:
                break
            time.sleep(0.5)
        row = con.execute('SELECT id FROM licenses WHERE square_subscription_id=?', (sub['id'],)).fetchone()
        row = row or con.execute('SELECT license_id AS id FROM square_replacements WHERE new_sub_id=?', (sub['id'],)).fetchone()
    expires = _expires_for(sub)
    if row:
        before = licenses.get(row['id'])
        lic = licenses.update(row['id'], square_status=(sub.get('status') or '').upper(), expires_at=expires,
                              renews_at=None if expires else _day_end(sub.get('charged_through_date')))
        if before and not before.get('expires_at') and expires:       # just cancelled: tell them once
            _mail_cancelled(lic)
        return lic
    var_ok = con.execute('SELECT 1 FROM square_catalog WHERE variation_id=?', (sub.get('plan_variation_id'),)).fetchone()
    if not var_ok:
        log.info('Square subscription %s: plan %s is not one of BRB\'s', sub.get('id'), sub.get('plan_variation_id'))
        return None
    if user is None:
        user = _user_for_customer(sub.get('customer_id'))
    if user is None:
        return None
    var = con.execute('SELECT spec, price_cents FROM square_catalog WHERE variation_id=?',
                      (sub.get('plan_variation_id'),)).fetchone()
    what = _spec_words(var['spec']) if var and var['spec'] else (price_text(var['price_cents']) + '/month' if var else '')
    lic = licenses.create(plan='subscription', max_devices=1, expires_at=expires, email=user.get('email') or '',
                          user_id=user['id'], by='square', square_subscription_id=sub['id'],
                          square_status=(sub.get('status') or '').upper(),
                          note='Square subscription' + (f' ({what})' if what else ''))
    # the computer whose BRB opened the checkout goes straight onto the key (the app then picks it up by itself)
    ck = con.execute("SELECT device_id FROM square_checkouts WHERE user_id=? AND status='open' AND device_id IS NOT NULL "
                     'ORDER BY created_at DESC', (user['id'],)).fetchone()
    if ck:
        licenses.bind_device(lic['id'], ck['device_id'])
    con.execute("UPDATE square_checkouts SET status='done' WHERE user_id=? AND status='open'", (user['id'],))
    con.commit()
    if not expires and sub.get('charged_through_date'):
        licenses.update(lic['id'], renews_at=_day_end(sub.get('charged_through_date')))
    _mail_confirmation(licenses.get(lic['id']), user, what, renews=_day_end(sub.get('charged_through_date')),
                       device_id=ck['device_id'] if ck else None)
    return licenses.get(lic['id'])


def _device_name(device_id):
    if not device_id:
        return None
    row = licenses.db().execute('SELECT name FROM devices WHERE id=?', (device_id,)).fetchone()
    return (row['name'] if row and row['name'] else 'your computer')


def _mail_confirmation(lic, user, what, renews=None, device_id=None, test=False):
    try:
        from . import purchasemail
        purchasemail.confirmation(lic, user, what, renews=renews, on_computer=_device_name(device_id), test=test)
    except Exception:  # noqa: BLE001 - mail must never break a purchase
        log.exception('purchase confirmation email')


def _mail_cancelled(lic):
    try:
        from . import purchasemail
        user = users.get(lic['user_id']) if lic.get('user_id') else None
        purchasemail.cancelled(lic, user)
    except Exception:  # noqa: BLE001
        log.exception('cancellation email')


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


def _sandbox_paid_checkouts(user):
    """Sandbox only. Square's sandbox checkout takes the test payment but never creates the subscription (a known
    sandbox limitation - production does). So that the rest can still be tested - key on the license page, BRB going
    unlimited by itself - a paid sandbox checkout gets a test key that ends after 2 days."""
    if env() != 'sandbox':
        return []
    out = []
    con = db()
    rows = con.execute("SELECT id, link_id, device_id FROM square_checkouts WHERE user_id=? AND env='sandbox' AND status='open' "
                       'AND link_id IS NOT NULL AND created_at > ?', (user['id'], time.time() - 86400)).fetchall()
    for r in rows:
        try:
            link = call('GET', f'/v2/online-checkout/payment-links/{quote(r["link_id"])}').get('payment_link') or {}
            order = call('GET', f'/v2/orders/{quote(link["order_id"])}').get('order') or {} if link.get('order_id') else {}
        except SquareError as e:
            log.info('sandbox checkout %s: %s', r['link_id'], e)
            continue
        if not order.get('tenders'):
            continue                                   # not paid (yet)
        sid = f'sandbox-{order["id"]}'
        if con.execute('SELECT 1 FROM licenses WHERE square_subscription_id=?', (sid,)).fetchone():
            continue
        lic = licenses.create(plan='subscription', max_devices=1, expires_at=time.time() + 2 * 86400,
                              email=user.get('email') or '', user_id=user['id'], by='square', square_subscription_id=sid,
                              square_status='SANDBOX', note='Sandbox test purchase (Square sandbox makes no subscription)')
        if r['device_id']:
            licenses.bind_device(lic['id'], r['device_id'])
        con.execute("UPDATE square_checkouts SET status='done' WHERE id=?", (r['id'],))
        con.commit()
        _mail_confirmation(licenses.get(lic['id']), user, 'Square sandbox test', device_id=r['device_id'], test=True)
        log.info('sandbox checkout %s paid: test key %s', r['link_id'], lic['key'][-4:])
        out.append(licenses.get(lic['id']))
    return out


def _checkout_customers(user):
    """Square customers behind this person's recent checkouts (found through each payment link's order) - so a
    purchase is found even if they typed a different email address at Square's checkout."""
    ids = []
    rows = db().execute("SELECT link_id FROM square_checkouts WHERE user_id=? AND env=? AND status='open' AND link_id IS NOT NULL "
                        'AND created_at > ? ORDER BY created_at DESC LIMIT 5', (user['id'], env(), time.time() - 3 * 86400))
    for r in rows.fetchall():
        try:
            link = call('GET', f'/v2/online-checkout/payment-links/{quote(r["link_id"])}').get('payment_link') or {}
            if not link.get('order_id'):
                continue
            order = call('GET', f'/v2/orders/{quote(link["order_id"])}').get('order') or {}
            if order.get('customer_id'):
                ids.append(order['customer_id'])
        except SquareError as e:
            log.info('settle: checkout %s: %s', r['link_id'], e)
    return ids


def settle(user):
    """Look this person's subscriptions up in Square now (when they come back from checkout, and while the license
    page waits - before or without the webhook). -> the licenses made or updated"""
    if not ready():
        return []
    ids = []
    if user.get('email'):
        found = call('POST', '/v2/customers/search',
                     {'query': {'filter': {'email_address': {'exact': user['email']}}}, 'limit': 10})
        ids = [c['id'] for c in found.get('customers') or []]
    ids += [c for c in _checkout_customers(user) if c not in ids]
    subs = call('POST', '/v2/subscriptions/search', {'query': {'filter': {'customer_ids': ids[:30]}}}).get('subscriptions') or [] \
        if ids else []
    out = [lic for lic in (apply(s, user) for s in subs) if lic]
    if not out:
        out = _sandbox_paid_checkouts(user)
    log.info('settle %s: %d customer(s), %d subscription(s), %d ours', user.get('email'), len(ids), len(subs), len(out))
    return out


# ------------------------------------------------------------------ cancelling, and the "stay" offer
def plan_kind(lic):
    """'monthly' / 'annual' for a subscription key (None if unknown)."""
    spec = ''
    sid = lic.get('square_subscription_id') or ''
    if sid and not sid.startswith('sandbox-') and ready():
        try:
            sub = call('GET', f'/v2/subscriptions/{quote(sid)}').get('subscription') or {}
            row = db().execute('SELECT spec FROM square_catalog WHERE variation_id=?', (sub.get('plan_variation_id'),)).fetchone()
            spec = row['spec'] if row else ''
        except SquareError:
            spec = ''
    spec = spec or ('ANNUAL' if '/year' in (lic.get('note') or '') else 'MONTHLY' if '/month' in (lic.get('note') or '') else '')
    last = spec.split('|')[-1]
    return 'annual' if last.startswith('ANNUAL') else 'monthly' if last.startswith('MONTHLY') else None


def stay_offer_for(lic):
    """The offer to show someone about to cancel, or None: once per subscription, for an active one that renews."""
    if not lic or lic.get('plan') != 'subscription' or not lic.get('valid') or lic.get('expires_at') or not retention_pct():
        return None
    if db().execute("SELECT 1 FROM cancel_feedback WHERE license_id=? AND outcome='kept'", (lic['id'],)).fetchone():
        return None
    kind = plan_kind(lic)
    o = stay_offers().get(f'stay_{kind}') if kind else None
    if not o:
        return None
    return {'kind': kind, 'pct': retention_pct(), 'price': price_text(o['first']), 'then': price_text(o['then']),
            'unit': 'month' if kind == 'monthly' else 'year', 'renews_at': lic.get('renews_at'), 'variation': o}


def keep_with_offer(lic, offer):
    """Keep the subscription with the stay offer: the next renewal at the discount, then the normal price.
    Square can't change the price of a subscription bought through a checkout link (its first order keeps setting the
    price), so BRB ends the current one when its paid period ends and starts a new one the same day, on the same card,
    with the offer's pricing. The key, the computer and the dates stay as they are. -> the license"""
    sid = lic.get('square_subscription_id') or ''
    if sid.startswith('sandbox-'):   # a sandbox test key: nothing in Square
        return licenses.update(lic['id'], renews_at=lic.get('renews_at') or time.time() + 30 * 86400,
                               note=(lic.get('note') or '') + f' · stay offer {offer["price"]}')
    sub = call('GET', f'/v2/subscriptions/{quote(sid)}').get('subscription') or {}
    start = sub.get('charged_through_date')
    if not (sub.get('card_id') and sub.get('customer_id') and start):
        raise SquareError("We couldn't apply the offer to this subscription automatically. Please contact support and "
                          "we'll set it up for you.", 409)
    var_id = _variation_id_for(offer['variation'])
    con = db()
    con.execute('INSERT OR REPLACE INTO square_replacements (old_sub_id, new_sub_id, license_id, reason, created_at) '
                "VALUES (?, NULL, ?, 'stay offer', ?)", (sid, lic['id'], time.time()))
    con.commit()
    cancelled = call('POST', f'/v2/subscriptions/{quote(sid)}/cancel', {})
    try:
        new = call('POST', '/v2/subscriptions', {
            'idempotency_key': f'stay-{sid}', 'location_id': sub.get('location_id') or location(),
            'plan_variation_id': var_id, 'customer_id': sub['customer_id'], 'card_id': sub['card_id'],
            'start_date': start}).get('subscription') or {}
        if not new.get('id'):
            raise SquareError("Square didn't start the new subscription.")
    except SquareError:
        for a in (cancelled.get('actions') or []):   # undo: the old subscription goes on as before
            try:
                call('DELETE', f'/v2/subscriptions/{quote(sid)}/actions/{quote(a["id"])}')
            except SquareError:
                log.exception('stay offer: undoing the cancel of %s', sid)
        con.execute('DELETE FROM square_replacements WHERE old_sub_id=?', (sid,))
        con.commit()
        raise
    con.execute('UPDATE square_replacements SET new_sub_id=? WHERE old_sub_id=?', (new['id'], sid))
    con.execute('UPDATE licenses SET square_subscription_id=? WHERE id=?', (new['id'], lic['id']))
    con.commit()
    log.info('stay offer: %s -> %s from %s', sid, new['id'], start)
    return licenses.update(lic['id'], expires_at=None, renews_at=_day_end(start) or lic.get('renews_at'),
                           square_status=(new.get('status') or 'PENDING').upper(),
                           note=(lic.get('note') or '') + f' · stay offer {offer["price"]} once')


def _variation_id_for(o):
    spec = _spec(o['phases'])
    q = 'SELECT variation_id FROM square_catalog WHERE env=? AND spec=? AND plan_name=?'
    row = db().execute(q, (env(), spec, PLAN_NAME)).fetchone()
    if not row:
        sync_catalog()
        row = db().execute(q, (env(), spec, PLAN_NAME)).fetchone()
    if not row:
        raise SquareError("That offer isn't set up in Square yet. Please try again in a minute.", 409)
    return row['variation_id']


def record_feedback(lic, user, reason, feedback, outcome='started', fid=None):
    con = db()
    now = time.time()
    if fid:
        con.execute('UPDATE cancel_feedback SET outcome=?, updated_at=? WHERE id=?', (outcome, now, fid))
        con.commit()
        return fid
    cur = con.execute('INSERT INTO cancel_feedback (license_id, user_id, email, plan, reason, feedback, outcome, created_at) '
                      'VALUES (?,?,?,?,?,?,?,?)', (lic['id'], (user or {}).get('id'), (user or {}).get('email') or '',
                                                   lic.get('note') or '', reason[:80], feedback[:4000], outcome, now))
    con.commit()
    return cur.lastrowid


def feedback_list(limit=100):
    rows = db().execute('SELECT * FROM cancel_feedback ORDER BY created_at DESC LIMIT ?', (limit,)).fetchall()
    counts = db().execute("SELECT reason, COUNT(*) AS n, SUM(outcome='kept') AS kept FROM cancel_feedback "
                          'GROUP BY reason ORDER BY n DESC').fetchall()
    return [dict(r) for r in rows], [dict(r) for r in counts]


def settle_if_pending(user):
    """On the license and account pages: if this person started a checkout in the last day that hasn't turned into a
    key yet, look in Square now - so the key shows up wherever they look first (the webhook does it too)."""
    if not user or not ready():
        return
    try:
        row = db().execute("SELECT 1 FROM square_checkouts WHERE user_id=? AND env=? AND status='open' AND created_at > ?",
                           (user['id'], env(), time.time() - 86400)).fetchone()
    except Exception:  # noqa: BLE001
        return
    if row:
        try:
            settle(user)
        except SquareError as e:
            log.info('settle on page view: %s', e)


def cancel(lic):
    """Cancel at the end of the period already paid for (month or year). -> the license (with expires_at set)."""
    if not lic.get('square_subscription_id'):
        raise SquareError('This key is not a subscription.', 400)
    if lic['square_subscription_id'].startswith('sandbox-'):      # a sandbox test key: nothing in Square to cancel
        out = licenses.update(lic['id'], square_status='CANCELED', expires_at=time.time())
        _mail_cancelled(out)
        return out
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
