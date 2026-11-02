import base64
import hashlib
import hmac
import json
import os
from unittest import mock

from dopplesite import licenses, square, users

from .test_site import Base

DEV_A = hashlib.sha256(b'computer A').hexdigest()   # like a real device id
DEV_B = hashlib.sha256(b'computer B').hexdigest()


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class LicenseBase(Base):
    extra_env = {'FREE_SECONDS': '3600'}

    def setUp(self):
        super().setUp()
        self.clock = Clock()
        self.tp = mock.patch.object(licenses.time, 'time', self.clock)
        self.tp.start()

    def tearDown(self):
        self.tp.stop()
        super().tearDown()

    def api(self, path, **body):
        body.setdefault('device_id', DEV_A)
        body.setdefault('device_name', 'Pat laptop')
        body.setdefault('platform', 'windows')
        body.setdefault('app_version', '0.1.40')
        return self.c.post('/api/v1/license/' + path, json=body)


class LicenseTests(LicenseBase):

    def test_free_hour_is_tracked_on_the_server(self):
        st = self.api('status').get_json()
        self.assertEqual((st['plan'], st['free_left'], st['can_loop']), ('free', 3600, True))
        self.assertTrue(st['offer'] and '/license?offer=' in st['license_url'])
        r = self.api('lease', action='start').get_json()
        self.assertTrue(r['ok'])
        self.assertEqual(r['lease_seconds'], 30)
        self.assertEqual(r['free_left'], 3570)   # charged up front
        self.clock.t += 10                      # back to LIVE after 10 s: the unused 20 s come back
        r = self.api('lease', action='release').get_json()
        self.assertEqual(round(r['free_left']), 3590)
        # renewals charge exactly the time that passed
        self.api('lease', action='start')
        for _ in range(4):
            self.clock.t += 25
            self.api('lease', action='renew')
        self.clock.t += 5
        r = self.api('lease', action='release').get_json()
        self.assertEqual(round(r['free_left']), 3590 - 105)

    def test_reinstall_doesnt_reset_and_time_runs_out(self):
        self.api('lease', action='start')    # the computer is recorded once it uses the LOOP
        self.api('lease', action='release')
        licenses.db().execute('UPDATE devices SET used_seconds=3590 WHERE id=?', (DEV_A,))
        licenses.db().commit()
        r = self.api('lease', action='start').get_json()   # same hardware id after a reinstall: same total
        self.assertEqual((r['ok'], r['lease_seconds']), (True, 10))
        self.clock.t += 10
        r = self.api('lease', action='renew').get_json()
        self.assertFalse(r['ok'])
        self.assertIn('used up', r['message'])
        self.assertFalse(r['can_loop'])
        self.assertNotIn('offer', r)   # no early-bird once the free time is gone

    def test_crashed_app_gets_unused_lease_back(self):
        self.api('lease', action='start')
        self.clock.t += 5           # the app crashed; next start renews: only 5 s were used
        r = self.api('lease', action='start').get_json()
        self.assertEqual(round(r['free_left']), 3600 - 5 - 30)

    def test_bad_device_id_refused(self):
        r = self.api('status', device_id='not-a-hash')
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()['code'], 'bad_device')

    def test_key_unlimited_one_computer(self):
        lic = licenses.create(note='test')
        typed = lic['key'].lower().replace('-', ' ').replace('0', 'o')   # people type keys sloppily
        r = self.api('lease', action='start', key=typed).get_json()
        self.assertTrue(r['unlimited'] and r['ok'])
        self.assertEqual(r['free_left'], 3600)   # unlimited: free time untouched
        r = self.api('status', key=lic['key'], device_id=DEV_B).get_json()
        self.assertEqual(r['plan'], 'free')
        self.assertIn('already in use on another computer', r['key_problem'])
        # admin moves it to the new computer
        licenses.remove_device(lic['id'], DEV_A)
        self.assertTrue(self.api('status', key=lic['key'], device_id=DEV_B).get_json()['unlimited'])
        self.assertFalse(self.api('status', key=lic['key']).get_json()['unlimited'])
        # or allows two
        licenses.update(lic['id'], max_devices=2)
        self.assertTrue(self.api('status', key=lic['key']).get_json()['unlimited'])

    def test_wrong_revoked_and_expired_keys(self):
        self.assertIn("isn't right", self.api('status', key='DG-0000-0000-0000-0000').get_json()['key_problem'])
        lic = licenses.create()
        licenses.update(lic['id'], status='revoked')
        self.assertIn('turned off', self.api('status', key=lic['key']).get_json()['key_problem'])
        licenses.update(lic['id'], status='active', expires_at=self.clock.t - 1)
        self.assertIn('expired', self.api('status', key=lic['key']).get_json()['key_problem'])

    def test_early_bird_offer(self):
        st = self.api('status').get_json()
        token = st['license_url'].split('offer=')[1]
        self.assertTrue(licenses.offer_device(token))
        self.assertIsNone(licenses.offer_device(token[:-2] + 'xx'))                # tampered
        page = self.c.get('/license?offer=' + token, follow_redirects=True).get_data(as_text=True)
        self.assertIn('Early-bird', page)
        self.assertIn('$2.50', page)
        self.clock.t += licenses.OFFER_TTL + 1
        self.assertIsNone(licenses.offer_device(token))                            # expired link
        self.clock.t -= licenses.OFFER_TTL + 1
        licenses.db().execute('UPDATE devices SET used_seconds=3600 WHERE id=?', (DEV_A,))
        licenses.db().commit()
        self.assertIsNone(licenses.offer_device(token))                            # free time already used up

    def test_square_subscription_makes_key_and_binds_early_device(self):
        st = self.api('status').get_json()
        token = st['license_url'].split('offer=')[1]
        self.sign_in_with_hh({'id': 'hh9', 'name': 'Pat', 'email': 'pat@example.com', 'email_verified': True})
        self.c.get('/license?offer=' + token)
        calls = []

        def fake_call(method, path, body=None):
            calls.append((method, path, body))
            if path == '/v2/locations':
                return {'locations': [{'id': 'L1'}]}
            if path == '/v2/catalog/object':
                t = body['object']['type']
                return {'catalog_object': {'id': 'PLAN1' if t == 'SUBSCRIPTION_PLAN' else 'VAR250'}}
            if path == '/v2/online-checkout/payment-links':
                return {'payment_link': {'id': 'PL1', 'url': 'https://square.test/pay'}}
            if path.startswith('/v2/customers/'):
                return {'customer': {'email_address': 'pat@example.com'}}
            raise AssertionError(path)

        with mock.patch.dict(os.environ, {'SQUARE_SANDBOX_ACCESS_TOKEN': 't'}), mock.patch.object(square, 'call', fake_call):
            with self.c.session_transaction() as s:
                csrf = s['csrf']
            r = self.c.post('/license/subscribe', data={'csrf': csrf, 'early': '1'})
            self.assertEqual(r.headers['Location'], 'https://square.test/pay')
            link = [b for m, p, b in calls if p == '/v2/online-checkout/payment-links'][0]
            self.assertEqual(link['quick_pay']['price_money']['amount'], 250)
            self.assertEqual(link['checkout_options']['subscription_plan_id'], 'VAR250')
            # Square's webhook: subscription created
            lic = square.handle({'type': 'subscription.created', 'data': {'object': {'subscription': {
                'id': 'SUB1', 'status': 'ACTIVE', 'plan_variation_id': 'VAR250', 'customer_id': 'C1'}}}})
        self.assertIn('ACTIVE', lic)
        st = self.api('status').get_json()
        self.assertTrue(st['unlimited'])             # the computer that bought it is already on the key
        self.assertNotIn('offer', st)
        self.assertIn('DG-', self.c.get('/license').get_data(as_text=True))
        # cancelled in Square: works until the paid month ends
        k = licenses.all_licenses()[0]
        square.apply({'id': 'SUB1', 'status': 'ACTIVE', 'canceled_date': '2027-02-01', 'charged_through_date': '2027-02-01'})
        self.assertTrue(licenses.get(k['id'])['valid'])
        self.clock.t = 1_900_000_000
        self.assertFalse(licenses.get(k['id'])['valid'])

    def test_webhook_signature(self):
        body = json.dumps({'type': 'subscription.updated', 'data': {'object': {'subscription': {'id': 'nope'}}}}).encode()
        url = 'https://dg.test/api/square/webhook'
        with mock.patch.dict(os.environ, {'SQUARE_SANDBOX_WEBHOOK_SIGNATURE_KEY': 'k', 'SQUARE_SANDBOX_ACCESS_TOKEN': 't'}):
            self.assertEqual(self.c.post('/api/square/webhook', data=body, headers={'x-square-hmacsha256-signature': 'x'}).status_code, 403)
            sig = base64.b64encode(hmac.new(b'k', url.encode() + body, hashlib.sha256).digest()).decode()
            r = self.c.post('/api/square/webhook', data=body, headers={'x-square-hmacsha256-signature': sig})
            self.assertEqual(r.status_code, 200)

    def test_admin_keys_and_devices(self):
        self.api('lease', action='start')
        self.api('lease', action='release')
        self.c.post('/admin/login', data={'password': 'test-pw'})
        with self.c.session_transaction() as s:
            h = {'X-CSRF-Token': s['csrf']}
        self.assertEqual(self.c.post('/admin/api/licenses', json={}).status_code, 400)   # no CSRF
        k = self.c.post('/admin/api/licenses', json={'email': 'x@example.com', 'note': 'gift', 'max_devices': 2, 'days': 30},
                        headers=h).get_json()['license']
        self.assertEqual((k['max_devices'], k['note'], k['valid']), (2, 'gift', True))
        self.assertTrue(k['expires_at'])
        k = self.c.post(f"/admin/api/licenses/{k['id']}", json={'status': 'revoked'}, headers=h).get_json()['license']
        self.assertFalse(k['valid'])
        keys = self.c.get('/admin/api/licenses?q=gift').get_json()['licenses']
        self.assertEqual(len(keys), 1)
        # give a key straight to a computer
        k2 = self.c.post('/admin/api/licenses', json={'note': 'by email', 'device_id': DEV_A}, headers=h).get_json()['license']
        self.assertEqual([d['id'] for d in k2['devices']], [DEV_A])
        devs = self.c.get('/admin/api/devices?q=pat').get_json()['devices']
        self.assertEqual(devs[0]['keys'], [k2['key']])
        self.c.post(f'/admin/api/devices/{DEV_A}/free', json={'minutes': 30}, headers=h)
        self.assertEqual(licenses.free_left(dict(licenses.db().execute('SELECT * FROM devices').fetchone())), 3600 + 1800)
        r = self.c.post(f"/admin/api/licenses/{k2['id']}/devices/{DEV_A}/remove", headers=h).get_json()
        self.assertEqual(r['license']['devices'], [])
        self.assertIn('License keys', self.c.get('/admin').get_data(as_text=True))

    def test_pricing_on_home_page(self):
        page = self.c.get('/').get_data(as_text=True)
        self.assertIn('1 hour of LOOP time', page)
        self.assertIn('$5', page)
        self.assertIn('50% off', page)
        page = self.c.get('/license').get_data(as_text=True)
        self.assertIn('Unlimited', page)


del Base   # don't run the base class's (empty) tests twice


class DeviceGuardTests(LicenseBase):
    def test_made_up_device_ids_are_refused(self):
        for bad in ('a' * 64, 'ab' * 32, 'not-a-hash'):
            r = self.api('status', device_id=bad)
            self.assertEqual(r.status_code, 400, bad)
            self.assertEqual(r.get_json()['code'], 'bad_device')
        self.assertTrue(licenses.plausible_device_id(DEV_A))

    def test_a_status_check_alone_records_nothing(self):
        st = self.api('status').get_json()
        self.assertEqual(st['free_left'], 3600)
        self.assertEqual(licenses.db().execute('SELECT COUNT(*) FROM devices').fetchone()[0], 0)
        self.assertTrue(self.api('lease', action='release').get_json()['ok'])
        self.assertEqual(licenses.db().execute('SELECT COUNT(*) FROM devices').fetchone()[0], 0)
        self.api('lease', action='start')
        self.assertEqual(licenses.db().execute('SELECT COUNT(*) FROM devices').fetchone()[0], 1)

    def test_offer_link_works_before_the_first_loop(self):
        st = self.api('status').get_json()
        token = st['license_url'].split('offer=')[1]
        self.assertEqual(licenses.offer_device(token)['id'], DEV_A)

    def test_new_computers_per_connection_are_capped(self):
        with mock.patch.dict(os.environ, {'NEW_DEVICES_PER_IP_DAY': '3'}):
            ids = [hashlib.sha256(f'pc {i}'.encode()).hexdigest() for i in range(4)]
            for i in ids[:3]:
                self.assertTrue(self.api('lease', action='start', device_id=i).get_json()['ok'])
            r = self.api('lease', action='start', device_id=ids[3])
            self.assertEqual((r.status_code, r.get_json()['code']), (429, 'too_many_devices'))
            self.assertTrue(self.api('lease', action='renew', device_id=ids[0]).get_json()['ok'])   # known ones fine
            self.clock.t += 86400 + 1
            self.assertTrue(self.api('lease', action='start', device_id=ids[3]).get_json()['ok'])   # next day

    def test_admin_can_delete_a_computer(self):
        self.api('lease', action='start')
        self.c.post('/admin/login', data={'password': 'test-pw'})
        with self.c.session_transaction() as s:
            csrf = s['csrf']
        h = {'X-CSRF-Token': csrf}
        self.assertEqual(self.c.post(f'/admin/api/devices/{DEV_A}/delete', headers=h).status_code, 200)
        self.assertEqual(licenses.db().execute('SELECT COUNT(*) FROM devices').fetchone()[0], 0)
        self.assertEqual(self.c.post(f'/admin/api/devices/{DEV_A}/delete', headers=h).status_code, 404)
