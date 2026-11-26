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
                return {'catalog_object': {'id': 'X' + str(len(path))}}
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
