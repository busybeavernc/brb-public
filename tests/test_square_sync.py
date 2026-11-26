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

    def test_creates_the_plan_and_both_prices_once(self):
        r = square.sync_catalog()
        self.assertEqual(self.sq.prices(), [250, 500])
        self.assertEqual(len(r['created']), 3)
        self.assertEqual(square.sync_catalog()['created'], [])          # nothing new the second time
        self.assertEqual(len(self.sq.objects), 3)
        self.assertEqual(square.plan_variation(250), r['offered'][1]['variation_id'])

    def test_a_price_change_adds_a_variation_and_keeps_the_old_one(self):
        square.sync_catalog()
        with mock.patch.dict(os.environ, {'SUBSCRIPTION_PRICE_CENTS': '700', 'EARLY_PRICE_CENTS': '350'}):
            r = square.sync_catalog()
            self.assertEqual(self.sq.prices(), [250, 350, 500, 700])
            self.assertEqual(r['older_prices'], ['$2.50', '$5'])
            self.assertEqual(sum(o['type'] == 'SUBSCRIPTION_PLAN' for o in self.sq.objects), 1)

    def test_checkout_price_not_yet_in_square_syncs_first(self):
        self.assertTrue(square.plan_variation(500).startswith('ID'))
        self.assertEqual(self.sq.prices(), [250, 500])

    def test_no_early_bird_variation_without_a_discount(self):
        with mock.patch.dict(os.environ, {'EARLY_PRICE_CENTS': '500'}):
            square.sync_catalog()
        self.assertEqual(self.sq.prices(), [500])
