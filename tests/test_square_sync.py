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
