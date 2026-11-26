"""Square's catalog follows the website's prices."""
import os
from unittest import mock

from dopplesite import square

from .test_site import Base


class FakeCatalog:
    def __init__(self):
        self.objects, self.n = [], 0

    def __call__(self, method, path, body=None):
        if path == '/v2/catalog/search':
            plans = [o for o in self.objects if o['type'] == 'SUBSCRIPTION_PLAN']
            return {'objects': plans, 'related_objects': [o for o in self.objects if o['type'] != 'SUBSCRIPTION_PLAN']}
        if path == '/v2/catalog/object':
            self.n += 1
            o = {**body['object'], 'id': f'ID{self.n}'}
            self.objects.append(o)
            return {'catalog_object': o}
        raise AssertionError(path)

    def prices(self):
        return sorted(square._variation_price(o) for o in self.objects if o['type'] == 'SUBSCRIPTION_PLAN_VARIATION')


class SquareSyncTests(Base):
    def setUp(self):
        super().setUp()
        self.sq = FakeCatalog()
        self.p = mock.patch.object(square, 'call', self.sq)
        self.p.start()
        self.e = mock.patch.dict(os.environ, {'RETENTION_PCT': '0'})   # (stay offers tested on their own)
        self.e.start()
        self.addCleanup(self.e.stop)

    def tearDown(self):
        self.p.stop()
        super().tearDown()

    def specs(self):
        return sorted(square._variation_spec(o) for o in self.sq.objects if o['type'] == 'SUBSCRIPTION_PLAN_VARIATION')

    def test_creates_the_plan_and_each_option_once(self):
        with mock.patch.dict(os.environ, {'PROMO_ENDS': '2099-12-31'}):
            r = square.sync_catalog()
            self.assertEqual(self.specs(), ['ANNUAL:5988x1|ANNUAL:8388', 'MONTHLY:999'])
            self.assertEqual(len(r['created']), 3)
            self.assertEqual(square.sync_catalog()['created'], [])          # nothing new the second time
            self.assertEqual(len(self.sq.objects), 3)
            mid = [o['variation_id'] for o in r['offered'] if o['key'] == 'monthly'][0]
            self.assertEqual(square.plan_variation('monthly'), mid)

    def test_after_the_promo_the_regular_annual_is_added_and_the_promo_kept(self):
        with mock.patch.dict(os.environ, {'PROMO_ENDS': '2099-12-31'}):
            square.sync_catalog()
        with mock.patch.dict(os.environ, {'PROMO_ENDS': '2020-12-31'}):
            r = square.sync_catalog()
        self.assertEqual(self.specs(), ['ANNUAL:5988x1|ANNUAL:8388', 'ANNUAL:8388', 'MONTHLY:999'])
        self.assertEqual(r['older_prices'], ['$59.88 the first year, then $83.88/year'])
        self.assertEqual(sum(o['type'] == 'SUBSCRIPTION_PLAN' for o in self.sq.objects), 1)

    def test_a_price_change_adds_a_variation(self):
        with mock.patch.dict(os.environ, {'PROMO_ENDS': 'off'}):
            square.sync_catalog()
            with mock.patch.dict(os.environ, {'MONTHLY_PRICE_CENTS': '1099'}):
                square.sync_catalog()
        self.assertEqual(self.specs(), ['ANNUAL:8388', 'MONTHLY:1099', 'MONTHLY:999'])

    def test_checkout_option_not_yet_in_square_syncs_first(self):
        with mock.patch.dict(os.environ, {'PROMO_ENDS': 'off'}):
            self.assertTrue(square.plan_variation('annual').startswith('ID'))
        self.assertEqual(self.specs(), ['ANNUAL:8388', 'MONTHLY:999'])


class PricingAdminTests(Base):
    def test_admin_changes_prices_and_they_win_over_env(self):
        from dopplesite import settings
        self.admin_login() if hasattr(self, 'admin_login') else None
        settings.save({'MONTHLY_PRICE_CENTS': '1299', 'PROMO_ENDS': 'off'}, by='test')
        self.assertEqual(square.monthly_cents(), 1299)
        self.assertFalse(square.promo_active())
        page = self.c.get('/').get_data(as_text=True)
        self.assertIn('$12.99', page)
        settings.save({'MONTHLY_PRICE_CENTS': ''})                     # cleared: back to .env / built-in
        self.assertEqual(square.monthly_cents(), 999)
        with self.assertRaises(KeyError):
            settings.save({'SECRET_KEY': 'x'})                        # only the pricing settings

    def test_pricing_api(self):
        self.c.post('/admin/login', data={'password': 'test-pw'})
        with self.c.session_transaction() as s:
            h = {'X-CSRF-Token': s['csrf']}
        d = self.c.get('/admin/api/pricing').get_json()
        self.assertEqual(d['values']['monthly'], 9.99)
        r = self.c.post('/admin/api/pricing', headers=h, json={'monthly': '11.99', 'annual': '95.88',
                        'promo_price': '59.88', 'promo_ends': '2099-01-31', 'promo_name': 'Spring sale'})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(r.get_json()['values']['monthly'], 11.99)
        self.assertIn('$7.99', self.c.get('/').get_data(as_text=True))        # 95.88 / 12
        bad = self.c.post('/admin/api/pricing', headers=h, json={'monthly': '9.99', 'annual': '50',
                          'promo_price': '59.88', 'promo_ends': '2099-01-31'})
        self.assertEqual(bad.status_code, 400)                                 # promo not lower than annual
        self.assertIn('pricing', [e['action'] for e in self.c.get('/admin/api/audit').get_json()['entries']])


class SettleTests(Base):
    def test_key_found_through_the_checkout_even_with_another_email_at_square(self):
        from dopplesite import licenses
        self.sign_in_with_hh({'id': 'hh5', 'name': 'Lee', 'email': 'lee@example.com', 'email_verified': True})
        calls = []

        def fake(method, path, body=None):
            calls.append(path)
            if path == '/v2/catalog/search':
                return {'objects': []}
            if path == '/v2/catalog/object':
                o = body['object']
                if o['type'] == 'SUBSCRIPTION_PLAN':
                    return {'catalog_object': {'id': 'PLAN1'}}
                return {'catalog_object': {'id': 'VAR' + str(o['subscription_plan_variation_data']['phases'][0]['pricing']['price_money']['amount'])}}
            if path == '/v2/locations':
                return {'locations': [{'id': 'L1'}]}
            if path == '/v2/online-checkout/payment-links':
                return {'payment_link': {'id': 'PL9', 'url': 'https://square.test/pay'}}
            if path == '/v2/customers/search':
                return {}                                    # nobody with lee@example.com at Square
            if path == '/v2/online-checkout/payment-links/PL9':
                return {'payment_link': {'id': 'PL9', 'order_id': 'O1'}}
            if path == '/v2/orders/O1':
                return {'order': {'id': 'O1', 'customer_id': 'C77'}}
            if path == '/v2/subscriptions/search':
                assert body['query']['filter']['customer_ids'] == ['C77']
                return {'subscriptions': [{'id': 'S9', 'status': 'ACTIVE', 'customer_id': 'C77',
                                           'plan_variation_id': 'VAR999'}]}
            raise AssertionError(path)
        with mock.patch.dict(os.environ, {'SQUARE_SANDBOX_ACCESS_TOKEN': 't'}), mock.patch.object(square, 'call', fake):
            with self.c.session_transaction() as s:
                csrf = s['csrf']
            self.c.post('/license/subscribe', data={'csrf': csrf, 'plan': 'monthly'})
            self.assertEqual(self.c.get('/license/check').get_json()['keys'], 1)
        self.assertEqual(len(licenses.all_licenses()), 1)
        page = self.c.get('/license?n=thanks').get_data(as_text=True)
        self.assertIn('Your license key is below', page)

    def test_thank_you_page_waits_for_the_key(self):
        self.sign_in_with_hh({'id': 'hh6', 'name': 'Kim', 'email': 'kim@example.com', 'email_verified': True})
        page = self.c.get('/license?n=thanks').get_data(as_text=True)
        self.assertIn('/license/check', page)
        self.assertIn('in a moment', page)

    def test_sandbox_paid_checkout_gets_a_test_key(self):
        from dopplesite import licenses
        self.sign_in_with_hh({'id': 'hh7', 'name': 'Ana', 'email': 'ana@example.com', 'email_verified': True})

        def fake(method, path, body=None):
            if path == '/v2/catalog/search':
                return {'objects': []}
            if path == '/v2/catalog/object':
                return {'catalog_object': {'id': 'X' + square._spec([(p['cadence'], p['pricing']['price_money']['amount'], p.get('periods'))
                                                                   for p in body['object'].get('subscription_plan_variation_data', {}).get('phases', [])])}}
            if path == '/v2/locations':
                return {'locations': [{'id': 'L1'}]}
            if path == '/v2/online-checkout/payment-links':
                return {'payment_link': {'id': 'PL5', 'url': 'https://square.test/pay'}}
            if path == '/v2/customers/search':
                return {}
            if path == '/v2/online-checkout/payment-links/PL5':
                return {'payment_link': {'id': 'PL5', 'order_id': 'O5'}}
            if path == '/v2/orders/O5':
                return {'order': {'id': 'O5', 'state': 'OPEN', 'tenders': [{'id': 'T1'}]}}   # paid, no customer, no subscription
            raise AssertionError(path)
        with mock.patch.dict(os.environ, {'SQUARE_SANDBOX_ACCESS_TOKEN': 't'}), mock.patch.object(square, 'call', fake):
            with self.c.session_transaction() as s:
                csrf = s['csrf']
            self.c.post('/license/subscribe', data={'csrf': csrf, 'plan': 'monthly'})
            self.assertEqual(self.c.get('/license/check').get_json()['keys'], 1)
            self.assertEqual(self.c.get('/license/check').get_json()['keys'], 1)          # only once
            lic = licenses.all_licenses()[0]
            self.assertEqual(lic['square_status'], 'SANDBOX')
            square.cancel(lic)
        self.assertFalse(licenses.get(lic['id'])['valid'])

    def test_confirmation_and_cancellation_emails_once(self):
        from dopplesite import mailer, users
        mailer.sent.clear()
        self.sign_in_with_hh({'id': 'hh8', 'name': 'Robin Lee', 'email': 'robin@example.com', 'email_verified': True})
        user = users.db().execute("SELECT * FROM users WHERE email='robin@example.com'").fetchone()
        con = square.db()
        con.execute("INSERT INTO square_catalog (env, price_cents, variation_id, created_at, plan_name, spec) "
                    "VALUES ('sandbox', 5988, 'V1', 0, ?, 'ANNUAL:5988x1|ANNUAL:8388')", (square.PLAN_NAME,))
        con.commit()
        sub = {'id': 'S1', 'status': 'ACTIVE', 'plan_variation_id': 'V1', 'customer_id': 'C1',
               'charged_through_date': '2027-11-26'}
        lic = square.apply(sub, dict(user))
        square.apply(sub, dict(user))                                         # the webhook again: no second email
        conf = [m for m in mailer.sent if 'license key' in m['subject']]
        self.assertEqual(len(conf), 1)
        body = conf[0]['body']
        self.assertIn('Hi Robin,', body)
        self.assertIn(lic['key'], body)
        self.assertIn('$59.88 the first year, then $83.88/year', body)
        self.assertIn('Next renewal: November 26, 2027', body)
        square.apply({**sub, 'canceled_date': '2027-11-26'})
        square.apply({**sub, 'canceled_date': '2027-11-26'})
        canc = [m for m in mailer.sent if 'cancelled' in m['subject']]
        self.assertEqual(len(canc), 1)
        self.assertIn('keeps working until November 26, 2027', canc[0]['body'])

    def test_account_page_shows_each_keys_dates(self):
        from dopplesite import licenses, users
        self.sign_in_with_hh({'id': 'hh11', 'name': 'Sam', 'email': 'sam2@example.com', 'email_verified': True})
        user = dict(users.db().execute("SELECT * FROM users WHERE email='sam2@example.com'").fetchone())
        con = square.db()
        con.execute("INSERT INTO square_catalog (env, price_cents, variation_id, created_at, plan_name, spec) "
                    "VALUES ('sandbox', 999, 'VM', 0, ?, 'MONTHLY:999')", (square.PLAN_NAME,))
        con.commit()
        sub = {'id': 'S2', 'status': 'ACTIVE', 'plan_variation_id': 'VM', 'charged_through_date': '2027-01-05'}
        square.apply(sub, user)
        licenses.create(plan='unlimited', email='sam2@example.com', user_id=user['id'])
        page = self.c.get('/account').get_data(as_text=True)
        self.assertIn('renews Jan 5, 2027', page)
        self.assertIn('never expires', page)
        square.apply({**sub, 'canceled_date': '2027-01-05'})
        page = self.c.get('/account').get_data(as_text=True)
        self.assertIn('cancelled · works until Jan 5, 2027', page)
        self.assertIn('cancelled · works until Jan 5, 2027', self.c.get('/license').get_data(as_text=True))


class CancelFlowTests(Base):
    def setUp(self):
        super().setUp()
        from dopplesite import users
        self.sign_in_with_hh({'id': 'hh20', 'name': 'Jo Park', 'email': 'jo@example.com', 'email_verified': True})
        self.user = dict(users.db().execute("SELECT * FROM users WHERE email='jo@example.com'").fetchone())
        con = square.db()
        for vid, spec in (('VM', 'MONTHLY:999'), ('VSTAY', 'MONTHLY:699x1|MONTHLY:999'), ('VSTAYA', 'ANNUAL:5872x1|ANNUAL:8388')):
            con.execute("INSERT INTO square_catalog (env, price_cents, variation_id, created_at, plan_name, spec) "
                        "VALUES ('sandbox', 0, ?, 0, ?, ?)", (vid, square.PLAN_NAME, spec))
        con.commit()
        self.calls = []
        self.subs = {'S1': {'id': 'S1', 'status': 'ACTIVE', 'plan_variation_id': 'VM', 'customer_id': 'C1', 'card_id': 'K1',
                            'location_id': 'L1', 'charged_through_date': '2027-01-05'}}

        def fake(method, path, body=None):
            self.calls.append((method, path, body))
            if method == 'GET' and path.startswith('/v2/subscriptions/'):
                return {'subscription': self.subs[path.rsplit('/', 1)[1]]}
            if path == '/v2/subscriptions/S1/cancel':
                self.subs['S1'] = {**self.subs['S1'], 'canceled_date': '2027-01-05'}
                return {'subscription': self.subs['S1'], 'actions': [{'id': 'A1', 'type': 'CANCEL'}]}
            if method == 'POST' and path == '/v2/subscriptions':
                return {'subscription': {'id': 'S2', 'status': 'PENDING', 'plan_variation_id': body['plan_variation_id']}}
            raise AssertionError(path)
        self.p = mock.patch.object(square, 'call', fake)
        self.p.start()
        self.addCleanup(self.p.stop)
        self.env = mock.patch.dict(os.environ, {'SQUARE_SANDBOX_ACCESS_TOKEN': 't'})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.lic = square.apply(self.subs['S1'], self.user)
        with self.c.session_transaction() as s:
            self.csrf = s['csrf']

    def test_reason_then_offer_then_keep(self):
        from dopplesite import licenses, mailer
        mailer.sent.clear()
        page = self.c.get('/account').get_data(as_text=True)
        self.assertIn(f'/account/cancel/{self.lic["id"]}', page)
        self.assertIn('Why are you cancelling?', self.c.get(f'/account/cancel/{self.lic["id"]}').get_data(as_text=True))
        r = self.c.post(f'/account/cancel/{self.lic["id"]}', data={'csrf': self.csrf, 'reason': 'price', 'feedback': 'pricey'})
        page = r.get_data(as_text=True)
        self.assertIn('$6.99', page)                         # 30% off $9.99
        self.assertIn('regular $9.99/month', page)
        r = self.c.post(f'/account/cancel/{self.lic["id"]}/keep', data={'csrf': self.csrf})
        self.assertIn('Thank you for staying', r.get_data(as_text=True))
        new = [b for m, p, b in self.calls if m == 'POST' and p == '/v2/subscriptions'][0]
        self.assertEqual((new['plan_variation_id'], new['card_id'], new['start_date']), ('VSTAY', 'K1', '2027-01-05'))
        lic = licenses.get(self.lic['id'])
        self.assertEqual(lic['square_subscription_id'], 'S2')
        self.assertTrue(lic['valid'] and not lic['expires_at'])
        # Square then reports the old subscription cancelled: the key is untouched
        square.apply(self.subs['S1'])
        self.assertFalse(licenses.get(self.lic['id'])['expires_at'])
        self.assertEqual(len(licenses.all_licenses()), 1)
        self.assertTrue(any('discount' in m['subject'] for m in mailer.sent))
        # the offer is once per subscription: next time straight to "cancel?"
        r = self.c.post(f'/account/cancel/{self.lic["id"]}', data={'csrf': self.csrf, 'reason': 'price'})
        self.assertIn('Cancel your subscription?', r.get_data(as_text=True))
        fb, counts = square.feedback_list()
        self.assertEqual(counts[0]['reason'], 'price')
        self.assertIn('kept', [f['outcome'] for f in fb])

    def test_decline_offer_cancels(self):
        from dopplesite import licenses
        self.c.post(f'/account/cancel/{self.lic["id"]}', data={'csrf': self.csrf, 'reason': 'not_using'})
        r = self.c.post(f'/account/cancel/{self.lic["id"]}/confirm', data={'csrf': self.csrf})
        self.assertIn('n=canceled', r.headers['Location'])
        self.assertTrue(licenses.get(self.lic['id'])['expires_at'])
        fb, _ = square.feedback_list()
        self.assertEqual(fb[0]['outcome'], 'cancelled')

    def test_reason_is_required(self):
        r = self.c.post(f'/account/cancel/{self.lic["id"]}', data={'csrf': self.csrf, 'reason': ''})
        self.assertEqual(r.status_code, 400)


class PromoSafeguardTests(Base):
    def test_promo_moves_to_regular_price_before_first_renewal_only(self):
        import datetime as dt
        from dopplesite import licenses, users
        self.sign_in_with_hh({'id': 'hh30', 'name': 'Pat', 'email': 'pat3@example.com', 'email_verified': True})
        user = dict(users.db().execute("SELECT * FROM users WHERE email='pat3@example.com'").fetchone())
        con = square.db()
        con.execute("INSERT INTO square_catalog (env, price_cents, variation_id, created_at, plan_name, spec) VALUES "
                    "('sandbox', 5988, 'VP', 0, ?, 'ANNUAL:5988x1|ANNUAL:8388'), ('sandbox', 8388, 'VA', 0, ?, 'ANNUAL:8388')",
                    (square.PLAN_NAME, square.PLAN_NAME))
        con.commit()
        due = dt.date.today() + dt.timedelta(days=200)
        subs = {'P1': {'id': 'P1', 'status': 'ACTIVE', 'plan_variation_id': 'VP', 'customer_id': 'C', 'card_id': 'K',
                       'location_id': 'L', 'charged_through_date': due.isoformat(), 'order_template_id': 'T1'}}
        made = []
        template = {'amount': 8388}

        def fake(method, path, body=None):
            if path == '/v2/orders/T1':
                return {'order': {'id': 'T1', 'total_money': {'amount': template['amount'], 'currency': 'USD'}}}
            if method == 'GET' and path.startswith('/v2/subscriptions/'):
                return {'subscription': subs[path.rsplit('/', 1)[1]]}
            if path.endswith('/cancel'):
                return {'subscription': subs['P1'], 'actions': [{'id': 'A'}]}
            if method == 'POST' and path == '/v2/subscriptions':
                made.append(body)
                subs['N1'] = {'id': 'N1', 'status': 'PENDING', 'plan_variation_id': body['plan_variation_id']}
                return {'subscription': subs['N1']}
            raise AssertionError(path)
        with mock.patch.dict(os.environ, {'SQUARE_SANDBOX_ACCESS_TOKEN': 't'}), mock.patch.object(square, 'call', fake):
            lic = square.apply(subs['P1'], user)
            self.assertEqual(square.promo_safeguard(), 0)                      # 200 days away: not yet
            subs['P1']['charged_through_date'] = (dt.date.today() + dt.timedelta(days=10)).isoformat()
            self.assertEqual(square.promo_safeguard(), 0)                      # Square will bill $83.88 itself: untouched
            self.assertEqual(made, [])
            template['amount'] = 5988                                          # ...but stuck at the promo price: fixed
            self.assertEqual(square.promo_safeguard(), 1)
            self.assertEqual((made[0]['plan_variation_id'], made[0]['start_date'], made[0]['card_id']),
                             ('VA', subs['P1']['charged_through_date'], 'K'))
            self.assertEqual(licenses.get(lic['id'])['square_subscription_id'], 'N1')
            self.assertEqual(square.promo_safeguard(), 0)                      # once
            self.assertTrue(licenses.get(lic['id'])['valid'])

    def test_staff_test_options_only_for_staff_and_when_on(self):
        with mock.patch.dict(os.environ, {'TEST_PRICES': 'off'}):
            self.assertEqual(square.test_offers(), {})
        with mock.patch.dict(os.environ, {'TEST_PRICES': 'on'}):
            self.assertEqual(square.describe(square.test_offers()['test_promo']), '$1 the first day, then $2/day')
            self.assertIn('stay_daily', square.stay_offers())
            with self.assertRaises(square.SquareError):
                square.checkout({'id': 'u', 'email': 'a@b.c'}, 'https://x/r', option='test_daily', staff=False)
            self.sign_in_with_hh({'id': 'hh31', 'name': 'Al', 'email': 'al@example.com', 'email_verified': True})
            self.assertNotIn('Staff only', self.c.get('/license').get_data(as_text=True))

    def test_staff_see_test_subscriptions(self):
        from dopplesite import users
        self.sign_in_with_hh({'id': 'hh32', 'name': 'Bo', 'email': 'bo@example.com', 'email_verified': True})
        users.db().execute("UPDATE users SET role='admin' WHERE email='bo@example.com'")
        users.db().commit()
        with mock.patch.dict(os.environ, {'TEST_PRICES': 'on', 'SQUARE_SANDBOX_ACCESS_TOKEN': 't'}):
            page = self.c.get('/license').get_data(as_text=True)
        self.assertIn('Staff only', page)
        self.assertIn('$1 the first day, then $2/day', page)
