"""Sales tax at checkout: NC buyers pay their county's rate (when switched on), everyone else none."""
import os
from decimal import Decimal
from unittest import mock

from dopplesite import salestax, settings, square

from .test_site import Base


class QuoteTests(Base):
    extra_env = {'SALES_TAX': 'on'}

    def test_nc_zip_picks_the_county_rate(self):
        q = salestax.quote('NC', '27514')                 # Chapel Hill
        self.assertEqual((q['county'], q['rate']), ('Orange', Decimal('7.5')))
        self.assertIn('Orange County, 7.5%', q['label'])
        self.assertEqual(salestax.quote('nc', '27401')['rate'], Decimal('6.75'))   # Greensboro
        self.assertEqual(salestax.quote('NC', '27262', county='Davidson')['county'], 'Davidson')   # buyer's choice wins

    def test_other_states_and_off(self):
        self.assertEqual(salestax.quote('VA', '')['rate'], 0)
        self.assertEqual(salestax.quote('XX')['rate'], 0)
        with mock.patch.dict(os.environ, {'SALES_TAX': 'off'}):
            self.assertEqual(salestax.quote('NC', '')['rate'], 0)
            self.assertEqual(salestax.quote('')['state'], '')

    def test_bad_input(self):
        for args in (('',), ('ZZ',), ('NC', '123'), ('NC', '90210')):
            with self.assertRaises(ValueError):
                salestax.quote(*args)

    def test_rate_overrides(self):
        self.assertEqual(salestax.parse_rates('wake county: 7.5%\n\n Durham:7.75'), 'Wake: 7.5\nDurham: 7.75')
        with self.assertRaises(ValueError):
            salestax.parse_rates('Atlantis: 5')
        with mock.patch.dict(os.environ, {'NC_TAX_RATES': 'Wake: 7.5'}):
            settings._saved['at'] = 0
            self.assertEqual(salestax.quote('NC', '27601')['rate'], Decimal('7.5'))

    def test_tax_cents_rounds_half_up(self):
        self.assertEqual(salestax.tax_cents(5988, Decimal('6.75')), 404)
        self.assertEqual(salestax.tax_cents(999, Decimal('7.25')), 72)


class CheckoutTests(Base):
    extra_env = {'SALES_TAX': 'on', 'SQUARE_ENVIRONMENT': 'sandbox', 'SQUARE_SANDBOX_ACCESS_TOKEN': 't',
                 'SQUARE_SANDBOX_LOCATION_ID': 'L1'}

    def setUp(self):
        super().setUp()
        self.bodies = []

        def fake(method, path, body=None):
            if path == '/v2/online-checkout/payment-links':
                self.bodies.append(body)
                return {'payment_link': {'id': 'PL', 'url': 'https://square.test/pay'}}
            if method == 'GET' or path.startswith(('/v2/customers', '/v2/subscriptions')):   # the license page's look-ups
                return {}
            raise AssertionError(path)
        self.p = mock.patch.object(square, 'call', fake)
        self.p.start()
        self.addCleanup(self.p.stop)
        self.pv = mock.patch.object(square, 'plan_variation', return_value='VAR1')
        self.pv.start()
        self.addCleanup(self.pv.stop)
        self.user = {'id': 'u1', 'email': 'pat@example.com'}

    def test_nc_checkout_is_an_order_with_tax(self):
        square.checkout(self.user, 'https://dg.test/r', 'annual', where=salestax.quote('NC', '27601'))
        b = self.bodies[-1]
        self.assertNotIn('quick_pay', b)
        self.assertEqual(b['checkout_options']['subscription_plan_id'], 'VAR1')
        tax = b['order']['taxes'][0]
        self.assertEqual((tax['percentage'], tax['type'], tax['scope']), ('7.25', 'ADDITIVE', 'ORDER'))
        self.assertIn('Wake County', tax['name'])
        row = square.db().execute('SELECT tax_state, tax_county, tax_pct, tax_cents, price_cents FROM square_checkouts').fetchone()
        self.assertEqual(tuple(row)[:3], ('NC', 'Wake', '7.25'))
        self.assertEqual(row['tax_cents'], salestax.tax_cents(row['price_cents'], Decimal('7.25')))

    def test_out_of_state_checkout_is_quick_pay(self):
        square.checkout(self.user, 'https://dg.test/r', 'monthly', where=salestax.quote('CA'))
        self.assertIn('quick_pay', self.bodies[-1])
        self.assertNotIn('order', self.bodies[-1])

    def test_promo_check_ignores_tax(self):
        orders = {'O1': {'total_money': {'amount': 8954}, 'total_tax_money': {'amount': 566}}}   # $83.88 + tax

        def fake(method, path, body=None):
            return {'order': orders[path.rsplit('/', 1)[1]]}
        with mock.patch.object(square, 'call', fake):
            self.assertFalse(square._will_bill_too_little({'order_template_id': 'O1'}, 8388))
            orders['O1'] = {'total_money': {'amount': 6392}, 'total_tax_money': {'amount': 404}}   # promo + tax
            self.assertTrue(square._will_bill_too_little({'order_template_id': 'O1'}, 8388))


class PageTests(Base):
    extra_env = {'SALES_TAX': 'on'}

    def test_tax_data_and_faq(self):
        d = self.c.get('/license/tax-data').get_json()
        self.assertEqual(d['zips']['27514'], 'Orange')
        self.assertEqual(d['rates']['Mecklenburg'], '8.25')
        self.assertIn('Do you charge sales tax?', self.c.get('/').get_data(as_text=True))


class SubscribeRouteTests(CheckoutTests):
    def test_license_page_asks_and_route_charges_tax(self):
        self.sign_in_with_hh({'id': 5, 'email': 'nc@example.com', 'email_verified': True, 'name': 'N C'})
        page = self.c.get('/license').get_data(as_text=True)
        self.assertIn('data-where', page)
        self.assertIn('North Carolina', page)
        with self.c.session_transaction() as s:
            csrf = s['csrf']
        r = self.c.post('/license/subscribe', data={'csrf': csrf, 'plan': 'annual', 'state': 'NC', 'zip': '1'})
        self.assertEqual(r.status_code, 400)
        self.assertIn('5-digit ZIP', r.get_data(as_text=True))
        r = self.c.post('/license/subscribe', data={'csrf': csrf, 'plan': 'annual', 'state': 'NC', 'zip': '27401'})
        self.assertEqual(r.status_code, 303)
        self.assertIn('Guilford', self.bodies[-1]['order']['taxes'][0]['name'])
        self.assertIn('value="27401"', self.c.get('/license').get_data(as_text=True))   # remembered
