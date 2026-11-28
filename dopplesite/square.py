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

The webhook subscription should send: subscription.created, subscription.updated, invoice.payment_made,
invoice.scheduled_charge_failed (a failed renewal is also found by the hourly check, just later).

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
import os
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
CREATE TABLE IF NOT EXISTS square_swaps (sub_id TEXT PRIMARY KEY, variation_id TEXT NOT NULL, reason TEXT NOT NULL,
  created_at REAL NOT NULL);
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
        ck = [r[1] for r in con.execute('PRAGMA table_info(square_checkouts)')]
        if 'tax_state' not in ck:   # where the buyer said they are, and the sales tax added
            con.executescript("ALTER TABLE square_checkouts ADD COLUMN tax_state TEXT NOT NULL DEFAULT ''; "
                              "ALTER TABLE square_checkouts ADD COLUMN tax_zip TEXT NOT NULL DEFAULT ''; "
                              "ALTER TABLE square_checkouts ADD COLUMN tax_county TEXT NOT NULL DEFAULT ''; "
                              "ALTER TABLE square_checkouts ADD COLUMN tax_pct TEXT NOT NULL DEFAULT ''; "
                              "ALTER TABLE square_checkouts ADD COLUMN tax_cents INTEGER NOT NULL DEFAULT 0;")
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


def test_prices_on():
    """Staff test prices (TEST_PRICES, set in Admin > Pricing): small daily-billed options only staff see, to test real
    payments, renewals, the promo's price change and the stay offer in production within a day or two."""
    return str(settings.get('TEST_PRICES', '') or '').lower() in ('1', 'on', 'true', 'yes')


def test_offers():
    if not test_prices_on():
        return {}
    return {'test_daily': {'label': 'Staff test, daily', 'test': True, 'phases': [('DAILY', 100, None)], 'first': 100},
            'test_promo': {'label': 'Staff test, promo then daily', 'test': True,
                           'phases': [('DAILY', 100, 1), ('DAILY', 200, None)], 'first': 100}}


def stay_offers():
    """Variations for the "stay" offer: the next billing period at RETENTION_PCT off, then the normal price."""
    pct = retention_pct()
    if not pct:
        return {}
    out = {}
    kinds = [('MONTHLY', monthly_cents(), 'monthly'), ('ANNUAL', annual_cents(), 'annual')]
    if test_prices_on():
        kinds.append(('DAILY', 200, 'daily'))
    for cad, base, label in kinds:
        disc = max(100 if cad == 'DAILY' else 0, int(round(base * (100 - pct) / 100)))
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


class _CatalogLock:
    """One catalog change at a time, across all the site's worker processes (a lock file) and threads."""
    _thread = __import__('threading').Lock()

    def __enter__(self):
        import fcntl
        self._thread.acquire()
        self._f = open(os.path.join(settings.data_dir(), 'square-catalog.lock'), 'w')
        fcntl.flock(self._f, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        import fcntl
        fcntl.flock(self._f, fcntl.LOCK_UN)
        self._f.close()
        self._thread.release()


_sync_lock = _CatalogLock()


def sync_catalog():
    """(One at a time: two runs at once could each add the same variation.)"""
    with _sync_lock:
        return _sync_catalog()


def _sync_catalog():
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
        for key, o in {**offers(), **stay_offers(), **test_offers()}.items():
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
    unit = {'MONTHLY': 'month', 'ANNUAL': 'year', 'DAILY': 'day'}
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
    want = (env(), tuple(sorted(_spec(o['phases']) for o in {**offers(), **stay_offers(), **test_offers()}.values())),
            bool(ready()))
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


def checkout(user, return_url, option='annual', device=None, staff=False, where=None):
    """A Square checkout page for an option on sale ('annual' - with the promo while it runs - or 'monthly'). The key
    is put on `device` (the computer whose BRB opened the page) when the purchase goes through. `where`: salestax.quote()
    for the buyer; with a rate, the checkout is an order with that tax (Square then bills it on every renewal too,
    since the first order becomes the subscription's template). -> url"""
    if not user.get('email'):
        raise SquareError('Add an email address to your Happy Hacks account first (Square sends receipts there).', 400)
    o = offers().get(option) or (test_offers().get(option) if staff else None)
    if o is None:
        raise SquareError("That option isn't offered. Please reload the page.", 400)
    cid = 'sq' + uuid.uuid4().hex[:16]
    where = where or {}
    rate = where.get('rate') or 0
    item = f'{PLAN_NAME} - {o["label"]}'
    body = {'idempotency_key': _key(),
            'checkout_options': {'redirect_url': f'{return_url}?c={cid}', 'ask_for_shipping_address': False,
                                 'subscription_plan_id': plan_variation(option) if not o.get('test') else
                                 _variation_id_for(o)},
            'pre_populated_data': {'buyer_email': user['email']},
            'payment_note': f'BRB {user["id"]} {cid}'}
    if rate:
        body['order'] = {'location_id': location(), 'reference_id': cid,
                         'line_items': [{'name': item, 'quantity': '1',
                                         'base_price_money': {'amount': o['first'], 'currency': 'USD'}}],
                         'taxes': [{'uid': 'sales-tax', 'name': where.get('label') or 'Sales tax',
                                    'percentage': format(rate.normalize(), 'f') if hasattr(rate, 'normalize') else str(rate),
                                    'type': 'ADDITIVE', 'scope': 'ORDER'}]}
    else:
        body['quick_pay'] = {'name': item, 'price_money': {'amount': o['first'], 'currency': 'USD'},
                             'location_id': location()}
    link = call('POST', '/v2/online-checkout/payment-links', body).get('payment_link') or {}
    if not link.get('url'):
        raise SquareError("Square didn't return a checkout page.")
    con = db()
    from . import salestax
    con.execute('INSERT INTO square_checkouts (id, env, user_id, email, link_id, url, created_at, price_cents, device_id, '
                'tax_state, tax_zip, tax_county, tax_pct, tax_cents) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (cid, env(), user['id'], user['email'].lower(), link.get('id'), link['url'][:500], time.time(), o['first'],
                 device['id'] if device else None, where.get('state', ''), where.get('zip', ''), where.get('county', ''),
                 str(rate or ''), salestax.tax_cents(o['first'], rate) if rate else 0))
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
    return ('annual' if last.startswith('ANNUAL') else 'monthly' if last.startswith('MONTHLY') else
            'daily' if last.startswith('DAILY') else None)


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
            'unit': {'monthly': 'month', 'annual': 'year', 'daily': 'day'}[kind], 'renews_at': lic.get('renews_at'),
            'variation': o}


def replace_subscription(lic, phases, label, reason):
    """Move a subscription onto new pricing from its next billing day, same card, same key.
    Square can't change the price of a subscription bought through a checkout link (its first order keeps setting the
    price), so BRB ends the current one when its paid period ends and starts a new one that day, on the same card,
    with the new pricing. The key, the computer and the dates stay as they are. -> (license, new subscription)"""
    sid = lic.get('square_subscription_id') or ''
    sub = call('GET', f'/v2/subscriptions/{quote(sid)}').get('subscription') or {}
    start = sub.get('charged_through_date')
    if not (sub.get('card_id') and sub.get('customer_id') and start):
        raise SquareError("We couldn't change this subscription automatically (no card on file in Square). Please contact "
                          "support and we'll set it up for you.", 409)
    var_id = _variation_id_for({'label': label, 'phases': phases})
    con = db()
    con.execute('INSERT OR REPLACE INTO square_replacements (old_sub_id, new_sub_id, license_id, reason, created_at) '
                'VALUES (?, NULL, ?, ?, ?)', (sid, lic['id'], reason, time.time()))
    con.commit()
    cancelled = call('POST', f'/v2/subscriptions/{quote(sid)}/cancel', {})
    try:
        new = call('POST', '/v2/subscriptions', {
            'idempotency_key': f'{reason[:10]}-{sid}', 'location_id': sub.get('location_id') or location(),
            'plan_variation_id': var_id, 'customer_id': sub['customer_id'], 'card_id': sub['card_id'],
            'start_date': start}).get('subscription') or {}
        if not new.get('id'):
            raise SquareError("Square didn't start the new subscription.")
    except SquareError:
        for a in (cancelled.get('actions') or []):   # undo: the old subscription goes on as before
            try:
                call('DELETE', f'/v2/subscriptions/{quote(sid)}/actions/{quote(a["id"])}')
            except SquareError:
                log.exception('undoing the cancel of %s', sid)
        con.execute('DELETE FROM square_replacements WHERE old_sub_id=?', (sid,))
        con.commit()
        raise
    con.execute('UPDATE square_replacements SET new_sub_id=? WHERE old_sub_id=?', (new['id'], sid))
    con.execute('UPDATE licenses SET square_subscription_id=? WHERE id=?', (new['id'], lic['id']))
    con.commit()
    log.info('%s: subscription %s -> %s from %s (%s)', reason, sid, new['id'], start, _spec(phases))
    lic = licenses.update(lic['id'], expires_at=None, renews_at=_day_end(start) or lic.get('renews_at'),
                          square_status=(new.get('status') or 'PENDING').upper())
    return lic, new


def swap_plan(lic, phases, label, reason):
    """Move a subscription onto other pricing from its next billing day with Square's SwapPlan (same subscription:
    no cancellation). -> the scheduled action"""
    sid = lic.get('square_subscription_id') or ''
    var_id = _variation_id_for({'label': label, 'phases': phases})
    r = call('POST', f'/v2/subscriptions/{quote(sid)}/swap-plan', {'new_plan_variation_id': var_id})
    con = db()
    con.execute('INSERT OR REPLACE INTO square_swaps (sub_id, variation_id, reason, created_at) VALUES (?,?,?,?)',
                (sid, var_id, reason, time.time()))
    con.commit()
    acts = r.get('actions') or []
    log.info('%s: subscription %s swaps to %s (%s) on %s', reason, sid, var_id, _spec(phases),
             acts[0].get('effective_date') if acts else '?')
    return acts[0] if acts else {}


def from_checkout(sub):
    """Was this subscription bought through a checkout link? Then its first order is its billing template, and Square
    may keep billing that order's price whatever the plan says. One BRB made itself (through the API) has no template:
    Square bills the plan's own prices, so a plan swap works on it."""
    return bool(sub.get('order_template_id') or any(ph.get('order_template_id') for ph in (sub.get('phases') or [])))


def keep_with_offer(lic, offer):
    """Keep the subscription with the stay offer: the next renewal at the discount, then the normal price.
    A subscription without a checkout template is swapped onto the offer's plan from its next billing day (Square
    SwapPlan: nothing is cancelled, so Square sends no "cancelled" email). One bought through a checkout link is
    replaced (cancelled at the end of its paid period, a new one started that day) until swapping those is proven.
    Sets offer['how'] to 'swap' or 'replace'."""
    sid = lic.get('square_subscription_id') or ''
    if sid.startswith('sandbox-'):   # a sandbox test key: nothing in Square
        offer['how'] = 'swap'
        return licenses.update(lic['id'], renews_at=lic.get('renews_at') or time.time() + 30 * 86400,
                               note=(lic.get('note') or '') + f' · stay offer {offer["price"]}')
    phases, label = offer['variation']['phases'], offer['variation']['label']
    sub = call('GET', f'/v2/subscriptions/{quote(sid)}').get('subscription') or {}
    if sub and not from_checkout(sub):
        try:
            act = swap_plan(lic, phases, label, 'stay offer')
            offer['how'] = 'swap'
            log.info('stay offer: key %s swapped from %s', lic['key'][-4:], act.get('effective_date') or '?')
            return licenses.update(lic['id'], note=(lic.get('note') or '') + f' · stay offer {offer["price"]} once')
        except SquareError as e:
            log.warning('stay offer: Square refused the plan swap for key %s (%s) - replacing instead', lic['key'][-4:], e)
    lic, _new = replace_subscription(lic, phases, label, 'stay offer')
    offer['how'] = 'replace'
    return licenses.update(lic['id'], note=(lic.get('note') or '') + f' · stay offer {offer["price"]} once')


def _variation_id_for(o):
    """The Square variation with exactly this pricing (made in Square if it isn't there yet)."""
    with _sync_lock:
        return _variation_id_for_locked(o)


def _variation_id_for_locked(o):
    spec = _spec(o['phases'])
    q = 'SELECT variation_id FROM square_catalog WHERE env=? AND spec=? AND plan_name=?'
    row = db().execute(q, (env(), spec, PLAN_NAME)).fetchone()
    if row:
        return row['variation_id']
    found = _catalog_plans()
    for _p, vs in found:
        for v in vs:
            if _variation_spec(v) == spec:
                _remember(v['id'], o, spec)
                return v['id']
    plan = max(found, key=lambda pv: len(pv[1]))[0] if found else call('POST', '/v2/catalog/object', {
        'idempotency_key': _key(), 'object': {'type': 'SUBSCRIPTION_PLAN', 'id': '#plan',
                                              'subscription_plan_data': {'name': PLAN_NAME}}})['catalog_object']
    var = call('POST', '/v2/catalog/object', {'idempotency_key': _key(), 'object': {
        'type': 'SUBSCRIPTION_PLAN_VARIATION', 'id': '#variation', 'subscription_plan_variation_data': {
            'name': f'{o.get("label") or "BRB"} ({_spec_words(spec)})'[:255], 'subscription_plan_id': plan['id'],
            'phases': [{'cadence': c, 'ordinal': i, **({'periods': n} if n else {}), 'pricing': {
                'type': 'STATIC', 'price_money': {'amount': a, 'currency': 'USD'}}}
                       for i, (c, a, n) in enumerate(o['phases'])]}}})['catalog_object']
    _remember(var['id'], o, spec)
    return var['id']


def _remember(vid, o, spec):
    con = db()
    con.execute('INSERT OR REPLACE INTO square_catalog (env, price_cents, variation_id, created_at, plan_name, spec) '
                'VALUES (?,?,?,?,?,?)', (env(), o['phases'][0][1], vid, time.time(), PLAN_NAME, spec))
    con.commit()


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


PROMO_CHECK_DAYS = 14     # look at a promo subscription this long before its first renewal
_promo_check = {'at': 0.0, 'last': []}


def promo_safeguard(now=None):
    """Promo subscriptions (a cheaper first period, then the regular price) bought through a checkout link: Square may
    keep billing the first order's (promo) price after the promo ends. Shortly before the first renewal, move each onto
    the regular price from that renewal day (replace_subscription). -> how many were moved"""
    if not ready():
        return 0
    now = time.time() if now is None else now
    con = db()
    if not con.execute("SELECT 1 FROM square_catalog WHERE env=? AND spec LIKE '%x1|%'", (env(),)).fetchone():
        return 0   # no promo pricing ever sold
    rows = con.execute("SELECT * FROM licenses WHERE plan='subscription' AND status='active' AND expires_at IS NULL "
                       "AND square_subscription_id IS NOT NULL AND square_subscription_id NOT LIKE 'sandbox-%' "
                       "AND square_subscription_id NOT IN (SELECT new_sub_id FROM square_replacements "
                       "WHERE new_sub_id IS NOT NULL) AND square_subscription_id NOT IN (SELECT sub_id FROM square_swaps)"
                       ).fetchall()
    moved = 0
    seen = set()
    report = _promo_check['last'] = []
    for r in rows:
        lic = dict(r)
        if lic['id'] in seen:
            continue
        try:
            sub = call('GET', f'/v2/subscriptions/{quote(lic["square_subscription_id"])}').get('subscription') or {}
        except SquareError:
            continue
        cat = con.execute('SELECT spec FROM square_catalog WHERE variation_id=?', (sub.get('plan_variation_id'),)).fetchone()
        spec = cat['spec'] if cat else ''
        if '|' not in spec or sub.get('canceled_date') or (sub.get('status') or '').upper() not in ('ACTIVE', 'PENDING'):
            continue
        seen.add(lic['id'])
        due = _day_end(sub.get('charged_through_date'))
        if not due or due - now > PROMO_CHECK_DAYS * 86400:
            continue
        cad, rest = spec.split('|')[-1].split(':')
        regular = [(cad, int(rest.split('x')[0]), None)]
        if not _will_bill_too_little(sub, regular[0][1]):
            seen.add(lic['id'])
            report.append(f'key …{lic["key"][-4:]}: Square will bill the regular price itself - left alone')
            continue                       # Square will charge the regular price by itself: leave it alone
        if cad == 'DAILY' and test_prices_on():
            # Experiment (staff test subscriptions only): swap the plan instead of cancel-and-replace - no "cancelled"
            # email from Square, if Square then bills the new plan's price rather than the checkout order's.
            try:
                act = swap_plan(licenses.get(lic['id']), regular, 'Regular price', 'promo ends (swap test)')
                moved += 1
                report.append(f'key …{lic["key"][-4:]}: plan swap scheduled for {act.get("effective_date") or "?"}')
            except SquareError as e:
                log.warning('promo swap test for key %s: %s', lic['key'][-4:], e)
                report.append(f'key …{lic["key"][-4:]}: Square refused the plan swap: {e}')
            continue
        try:
            new_lic, _new = replace_subscription(licenses.get(lic['id']), regular, 'Regular price', 'promo ends')
            moved += 1
            try:
                from . import purchasemail
                purchasemail.moved_to_regular(new_lic, users.get(lic['user_id']) if lic.get('user_id') else None,
                                              _spec_words(_spec(regular)), _day_end(sub.get('charged_through_date')) - 86399)
            except Exception:  # noqa: BLE001
                log.exception('price switch email')
        except SquareError as e:
            log.warning('promo safeguard for key %s: %s', lic['key'][-4:], e)
            from . import mailer
            if mailer.staff():
                mailer.send(mailer.staff(), 'BRB: a promo subscription needs a look',
                            f'The promo subscription for {lic.get("email")} (key ending {lic["key"][-4:]}) renews on '
                            f'{sub.get("charged_through_date")}, and BRB could not move it onto the regular price: {e}\n\n'
                            'Please check it in Square so it renews at the regular price.', kind='alert')
    return moved


def _will_bill_too_little(sub, regular_cents):
    """Will Square's next bill for this subscription be below the regular price? Square bills from the subscription's
    order template when it has one (a checkout-made subscription does: its first, promo-priced order); without one, the
    plan's phase pricing applies, which is right. Only a template below the regular price needs fixing."""
    ids = [sub.get('order_template_id')] + [ph.get('order_template_id') for ph in (sub.get('phases') or [])
                                            if (ph.get('ordinal') or 0) >= 1]
    ids = [i for i in ids if i]
    for oid in ids:
        try:
            order = call('GET', f'/v2/orders/{quote(oid)}').get('order') or {}
        except SquareError:
            return True                    # can't tell: fix it, to be safe
        amount = ((order.get('total_money') or {}).get('amount'))
        tax = ((order.get('total_tax_money') or {}).get('amount')) or 0   # compare prices before sales tax
        if amount is not None and int(amount) - int(tax) < regular_cents:
            return True
    return False


def maybe_check_promos():
    """Called on requests: runs promo_safeguard about once an hour, in the background."""
    if settings.get('NO_AUTO_BACKUP') or not ready() or time.time() - _promo_check['at'] < 3600:
        return
    _promo_check['at'] = time.time()
    import threading

    def run():
        try:
            n = promo_safeguard()
            if n:
                log.info('promo safeguard: %d subscription(s) moved onto the regular price', n)
        except Exception:  # noqa: BLE001
            log.exception('promo safeguard')
        try:
            res = check_payments()
            if len(res) > 1:
                log.info('payment check: %s', res)
        except Exception:  # noqa: BLE001
            log.exception('payment check')
    threading.Thread(target=run, daemon=True, name='promo-check').start()


# ------------------------------------------------------------------ billing: next payment, card, history, failed payments
PAYMENT_GRACE_DAYS = 7     # a failed renewal: the key keeps working this long (PAYMENT_GRACE_DAYS)...
CANCEL_UNPAID_DAYS = 30    # ...and the subscription is cancelled after this long unpaid (CANCEL_UNPAID_DAYS)
_billing_cache = {}


def grace_days():
    return max(0, _cents('PAYMENT_GRACE_DAYS', PAYMENT_GRACE_DAYS))


def cancel_unpaid_days():
    return max(grace_days() + 1, _cents('CANCEL_UNPAID_DAYS', CANCEL_UNPAID_DAYS))


def _sub_ids(lic):
    sid = lic.get('square_subscription_id') or ''
    olds = [r['old_sub_id'] for r in db().execute('SELECT old_sub_id FROM square_replacements WHERE license_id=?', (lic['id'],))]
    return [x for x in [sid] + olds if x and not x.startswith('sandbox-')]


def _invoices(lic, sub):
    """This subscription's invoices (and those of the subscriptions it replaced), newest first."""
    ids = set(_sub_ids(lic))
    if not sub.get('customer_id'):
        return []
    body = {'query': {'filter': {'location_ids': [sub.get('location_id') or location()], 'customer_ids': [sub['customer_id']]},
                      'sort': {'field': 'INVOICE_SORT_DATE', 'order': 'DESC'}}, 'limit': 50}
    found = call('POST', '/v2/invoices/search', body).get('invoices') or []
    return [i for i in found if i.get('subscription_id') in ids]


def _due(inv):
    reqs = inv.get('payment_requests') or [{}]
    return reqs[0].get('due_date') or (inv.get('scheduled_at') or inv.get('created_at') or '')[:10]


def _amount(inv):
    reqs = inv.get('payment_requests') or [{}]
    m = reqs[0].get('computed_amount_money') or reqs[0].get('total_completed_amount_money') or {}
    return int(m.get('amount') or 0)


UNPAID = ('UNPAID', 'FAILED', 'PARTIALLY_PAID', 'PAYMENT_PENDING')
STATUS_WORDS = {'PAID': 'paid', 'UNPAID': 'not paid', 'FAILED': 'failed', 'PARTIALLY_PAID': 'partly paid',
                'PAYMENT_PENDING': 'processing', 'SCHEDULED': 'scheduled', 'REFUNDED': 'refunded',
                'PARTIALLY_REFUNDED': 'partly refunded', 'CANCELED': 'cancelled', 'DRAFT': 'draft'}


def _next_payment(lic, sub, invoices):
    """(date, cents) of the next charge, or None."""
    if sub.get('canceled_date') or (sub.get('status') or '').upper() in ('CANCELED', 'DEACTIVATED'):
        return None
    row = db().execute('SELECT spec FROM square_catalog WHERE variation_id=?', (sub.get('plan_variation_id'),)).fetchone()
    spec = row['spec'] if row else ''
    if not spec:
        return None
    phases = [p.split(':') for p in spec.split('|')]
    prices = [int(rest.split('x')[0]) for _c, rest in phases]
    if (sub.get('status') or '').upper() == 'PENDING':          # starts later (a stay offer / regular price switch)
        return sub.get('start_date'), prices[0]
    paid = sum(1 for i in invoices if i.get('subscription_id') == sub.get('id') and i.get('status') == 'PAID')
    return sub.get('charged_through_date'), prices[min(paid, len(prices) - 1)] if len(prices) > 1 else prices[0]


def billing(lic, fresh=False):
    """For the account page: {'next': (date, '$x'), 'card': {...}, 'history': [...], 'unpaid': {...} or None}."""
    sid = lic.get('square_subscription_id') or ''
    if not ready() or not sid or sid.startswith('sandbox-'):
        return None
    hit = _billing_cache.get(lic['id'])
    if hit and not fresh and time.time() - hit[0] < 60:
        return hit[1]
    try:
        sub = call('GET', f'/v2/subscriptions/{quote(sid)}').get('subscription') or {}
        invoices = _invoices(lic, sub)
        card = None
        if sub.get('card_id'):
            try:
                c = call('GET', f'/v2/cards/{quote(sub["card_id"])}').get('card') or {}
                card = {'brand': (c.get('card_brand') or 'Card').replace('_', ' ').title(), 'last4': c.get('last_4') or '',
                        'exp': f'{int(c.get("exp_month") or 0):02d}/{str(c.get("exp_year") or "")[-2:]}',
                        'exp_ts': _day_end(f'{c.get("exp_year")}-{int(c.get("exp_month") or 1):02d}-28') if c.get('exp_year') else None}
            except SquareError:
                card = None
        nxt = _next_payment(lic, sub, invoices)
        history = [{'date': _due(i), 'amount': price_text(_amount(i)), 'status': STATUS_WORDS.get(i.get('status'), (i.get('status') or '').lower()),
                    'ok': i.get('status') == 'PAID', 'url': i.get('public_url') or '', 'title': i.get('title') or 'BRB Unlimited'}
                   for i in invoices[:24]]
        unpaid = next(({'date': _due(i), 'amount': price_text(_amount(i)), 'url': i.get('public_url') or ''}
                       for i in invoices if i.get('status') in UNPAID and _due(i) <= time.strftime('%Y-%m-%d')), None)
        out = {'next': (nxt[0], price_text(nxt[1])) if nxt else None, 'card': card, 'history': history, 'unpaid': unpaid,
               'card_expires_first': bool(card and card.get('exp_ts') and nxt and _day_end(nxt[0]) and card['exp_ts'] < _day_end(nxt[0]))}
    except Exception as e:  # noqa: BLE001 - billing details must never break the account page
        log.info('billing for key %s: %s', lic['key'][-4:], e)
        out = {'error': "Billing details aren't available right now. Please try again in a few minutes."}
    _billing_cache[lic['id']] = (time.time(), out)
    return out


def check_payment(lic, now=None):
    """Keep a subscription's payment state in step with Square: a failed renewal is recorded (the key keeps working for
    the grace period, with emails), paid again clears it, and long unpaid cancels it. -> 'ok' / 'failing' / 'paused' /
    'cancelled' / 'recovered'"""
    from . import purchasemail
    now = time.time() if now is None else now
    sid = lic.get('square_subscription_id') or ''
    if not sid or sid.startswith('sandbox-'):
        return 'ok'
    sub = call('GET', f'/v2/subscriptions/{quote(sid)}').get('subscription') or {}
    invoices = _invoices(lic, sub)
    today = time.strftime('%Y-%m-%d', time.localtime(now))
    unpaid = [i for i in invoices if i.get('status') in UNPAID and _due(i) <= today]
    user = users.get(lic['user_id']) if lic.get('user_id') else None
    _billing_cache.pop(lic['id'], None)
    if not unpaid:
        if lic.get('payment_failed_at'):
            licenses.update(lic['id'], payment_failed_at=None, pay_url=None, payment_notice=None,
                            renews_at=_day_end(sub.get('charged_through_date')) or lic.get('renews_at'))
            purchasemail.payment_recovered(licenses.get(lic['id']), user)
            return 'recovered'
        return 'ok'
    inv = unpaid[-1]                                    # the oldest unpaid one
    since = _day_end(_due(inv)) - 86399 if _due(inv) else now
    url = inv.get('public_url') or ''
    days = (now - since) / 86400
    sent = lic.get('payment_notice') or ''
    if not lic.get('payment_failed_at'):
        licenses.update(lic['id'], payment_failed_at=since, pay_url=url, payment_notice='failed')
        purchasemail.payment_failed(licenses.get(lic['id']), user, price_text(_amount(inv)), url,
                                    since + grace_days() * 86400)
        return 'failing'
    if url and url != lic.get('pay_url'):
        licenses.update(lic['id'], pay_url=url)
    if days >= cancel_unpaid_days():
        cancel(licenses.get(lic['id']))
        licenses.update(lic['id'], expires_at=now, payment_notice='cancelled')
        return 'cancelled'
    if days >= grace_days() and sent != 'paused':
        licenses.update(lic['id'], payment_notice='paused')
        purchasemail.payment_paused(licenses.get(lic['id']), user, price_text(_amount(inv)), url,
                                    since + cancel_unpaid_days() * 86400)
        return 'paused'
    if grace_days() - 2 <= days < grace_days() and sent == 'failed':
        licenses.update(lic['id'], payment_notice='reminded')
        purchasemail.payment_reminder(licenses.get(lic['id']), user, price_text(_amount(inv)), url,
                                      since + grace_days() * 86400)
    return 'paused' if days >= grace_days() else 'failing'


def check_payments(now=None):
    """Hourly: subscriptions past their renewal date that Square hasn't moved on (a payment may have failed), and
    those already failing. -> {'checked': n, ...outcomes}"""
    if not ready():
        return {}
    now = time.time() if now is None else now
    rows = db().execute("SELECT id FROM licenses WHERE plan='subscription' AND status='active' AND "
                        "(payment_failed_at IS NOT NULL OR (renews_at IS NOT NULL AND renews_at < ? AND "
                        "(expires_at IS NULL OR expires_at > ?)))", (now, now)).fetchall()
    out = {'checked': 0}
    for r in rows:
        try:
            res = check_payment(licenses.get(r['id']), now)
        except SquareError as e:
            log.info('payment check: %s', e)
            continue
        out['checked'] += 1
        out[res] = out.get(res, 0) + 1
    return out


def check_subscription_payment(subscription_id):
    """A webhook said a subscription's invoice changed: check that one now."""
    row = db().execute('SELECT id FROM licenses WHERE square_subscription_id=?', (subscription_id,)).fetchone()
    if not row:
        rep = db().execute('SELECT license_id AS id FROM square_replacements WHERE old_sub_id=?', (subscription_id,)).fetchone()
        row = rep
    if row:
        return check_payment(licenses.get(row['id']))
    return 'not ours'


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
            if lic and lic.get('payment_failed_at'):
                check_subscription_payment(sid)          # paid after all: the key is back to normal
            return 'paid' if lic else 'not one of ours'
    if kind in ('invoice.scheduled_charge_failed', 'invoice.updated'):
        inv = obj.get('invoice') or {}
        if inv.get('subscription_id') and (kind != 'invoice.updated' or inv.get('status') in UNPAID + ('PAID',)):
            return 'payment ' + check_subscription_payment(inv['subscription_id'])
    return 'ignored'


def sales_by_state(days=365):
    """Completed checkouts (one per person) by the state buyers gave, for watching other states' sales-tax thresholds."""
    since = time.time() - days * 86400
    rows = db().execute("SELECT tax_state AS state, COUNT(DISTINCT user_id) AS buyers, SUM(price_cents) AS cents, "
                        "SUM(tax_cents) AS tax FROM square_checkouts WHERE status='done' AND env='production' "
                        "AND created_at>=? GROUP BY tax_state ORDER BY cents DESC", (since,)).fetchall()
    return [dict(r) for r in rows]
