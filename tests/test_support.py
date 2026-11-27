import io
import os
import re
from unittest import mock

from dopplesite import audit, backup, customers, licenses, mailer, support, users

from .test_licenses import DEV_A, DEV_B, LicenseBase


class SupportTests(LicenseBase):
    extra_env = {'FREE_SECONDS': '3600', 'SUPPORT_NOTIFY': 'help@example.com', 'NO_AUTO_BACKUP': '1'}

    def setUp(self):
        super().setUp()
        mailer.sent.clear()

    def csrf(self):
        self.c.get('/support')
        with self.c.session_transaction() as s:
            return s['csrf']

    def send(self, **form):
        data = {'csrf': self.csrf(), 'email': 'pat@example.com', 'name': 'Pat', 'topic': 'camera',
                'body': 'Zoom shows a black picture when I pick the BRB camera.', **form}
        return self.c.post('/support', data=data, content_type='multipart/form-data')

    def admin(self):
        self.c.post('/admin/login', data={'password': 'test-pw'})
        with self.c.session_transaction() as s:
            return {'X-CSRF-Token': s['csrf']}

    def test_customer_writes_in_and_gets_answers(self):
        r = self.c.get('/support?v=0.1.130&os=windows')
        self.assertIn('From BRB 0.1.130 on windows', r.get_data(as_text=True))
        r = self.send(app_version='0.1.130', platform='windows',
                      attachment=(io.BytesIO(b'2026 log line'), 'brb.log'))
        self.assertEqual(r.status_code, 303)
        token = r.headers['Location'].split('/support/t/')[1].split('?')[0]
        t = support.by_token(token)
        self.assertEqual((t['email'], t['topic'], t['status'], t['app_version']), ('pat@example.com', 'camera', 'new', '0.1.130'))
        self.assertIn('Zoom shows a black picture', self.c.get(r.headers['Location']).get_data(as_text=True))
        # the customer got a copy with their private link; staff were told
        self.assertEqual(mailer.sent[0]['to'], ['pat@example.com'])
        self.assertIn(f'/support/t/{token}', mailer.sent[0]['body'])
        self.assertEqual(mailer.sent[1]['to'], ['help@example.com'])
        # the attachment is there, for the link holder and for admins only
        stored = support.messages(t['id'])[0]['attachment']
        self.assertEqual(self.c.get(f'/support/t/{token}/file/{stored}').data, b'2026 log line')
        self.assertEqual(self.c.get(f'/support/t/{token}/file/../../x').status_code, 404)
        self.assertEqual(self.c.get('/support/t/not-a-token').status_code, 404)
        # staff answer (emailed), and an internal note (not shown, not emailed)
        h = self.admin()
        lst = self.c.get('/admin/api/tickets').get_json()
        self.assertEqual((lst['total'], lst['counts']), (1, {'new': 1}))
        d = self.c.post(f"/admin/api/tickets/{t['id']}/reply", json={'body': 'Pick HappyHacks Virtual Camera.'}, headers=h).get_json()
        self.assertEqual(d['ticket']['status'], 'waiting')
        self.c.post(f"/admin/api/tickets/{t['id']}/reply", json={'body': 'secret note', 'note': True}, headers=h)
        self.assertEqual(mailer.sent[-1]['to'], ['pat@example.com'])
        self.assertIn('Pick HappyHacks Virtual Camera.', mailer.sent[-1]['body'])
        page = self.c.get(f'/support/t/{token}').get_data(as_text=True)
        self.assertIn('Pick HappyHacks Virtual Camera.', page)
        self.assertNotIn('secret note', page)
        self.assertEqual(self.c.get(f"/admin/support/{t['id']}/file/{stored}").data, b'2026 log line')
        # the customer answers back: open again
        self.c.post(f'/support/t/{token}', data={'csrf': self.csrf(), 'body': 'That fixed it, thanks!'})
        self.assertEqual(support.get(t['id'])['status'], 'open')
        self.assertTrue(any(e['action'] == 'support_reply' for e in audit.recent()))

    def test_bad_input_spam_and_floods(self):
        self.assertIn('valid email', self.send(email='nope').get_data(as_text=True))
        self.assertIn('.log, .txt, .png or .jpg', self.send(attachment=(io.BytesIO(b'MZ'), 'x.exe')).get_data(as_text=True))
        self.assertEqual(self.db_count(), 0)
        r = self.send(website='http://spam')                       # the hidden field: quietly ignored
        self.assertEqual((r.status_code, self.db_count()), (303, 0))
        r = self.c.post('/support', data={'email': 'pat@example.com', 'body': 'long enough body text'})   # no CSRF
        self.assertIn('open too long', r.get_data(as_text=True))
        for _ in range(5):
            self.send()
        self.assertIn('Too many requests', self.send().get_data(as_text=True))
        self.assertEqual(self.db_count(), 5)

    def db_count(self):
        return support.db().execute('SELECT COUNT(*) FROM tickets').fetchone()[0]

    def test_support_role_can_help_but_not_run_the_site(self):
        self.sign_in_with_hh({'id': 'hh9', 'name': 'Sam Help', 'email': 'sam@example.com', 'email_verified': True})
        users.set_role('sam@example.com', 'support')
        with self.c.session_transaction() as s:
            h = {'X-CSRF-Token': s['csrf']}
        self.assertEqual(self.c.get('/admin').status_code, 200)
        self.assertEqual(self.c.get('/admin/api/me').get_json()['role'], 'support')
        self.assertEqual(self.c.post('/admin/api/licenses', json={'note': 'x'}, headers=h).status_code, 403)
        self.assertEqual(self.c.post('/admin/api/updates', headers=h).status_code, 403)
        self.assertEqual(self.c.get('/admin/export/keys.csv').status_code, 403)
        self.api('lease', action='start')
        self.assertEqual(self.c.post(f'/admin/api/devices/{DEV_A}/free', json={'minutes': 30}, headers=h).status_code, 200)
        self.assertEqual(self.c.post(f'/admin/api/devices/{DEV_A}/free', json={'minutes': 6000}, headers=h).status_code, 403)
        entry = audit.recent()[0]
        self.assertEqual((entry['who'], entry['action'], entry['target']), ('Sam Help', 'add_free_time', DEV_A))

    def test_linking_a_computer_and_moving_a_key_yourself(self):
        self.api('lease', action='start')
        link = self.api('status').get_json()['link_url']
        self.assertIn('/account?d=', link)
        self.sign_in_with_hh({'id': 'hh1', 'name': 'Pat', 'email': 'pat@example.com', 'email_verified': True})
        r = self.c.get('/account?d=' + link.split('d=')[1])
        self.assertEqual(r.status_code, 303)
        page = self.c.get('/account').get_data(as_text=True)
        self.assertIn('is now linked to your account', page)
        self.assertIn('Pat laptop', page)
        uid = users.db().execute('SELECT id FROM users').fetchone()['id']
        self.assertEqual(licenses.user_devices(uid)[0]['id'], DEV_A)
        # a key of theirs on computer A: they move it themselves, up to 3 times a month
        lic = licenses.create(email='pat@example.com')
        licenses.bind_device(lic['id'], DEV_A)
        page = self.c.get('/account').get_data(as_text=True)
        self.assertIn('…' + lic['key'][-4:], page)     # the computer shows its key, not free time
        self.assertIn('Move key', page)
        self.assertNotIn('min left', page)
        with self.c.session_transaction() as s:
            csrf = s['csrf']
        for i in range(licenses.SELF_MOVES_PER_MONTH):
            licenses.bind_device(lic['id'], DEV_A)
            r = self.c.post(f"/account/keys/{lic['id']}/computers/{DEV_A}/remove", data={'csrf': csrf})
            self.assertIn('n=moved', r.headers['Location'])
        licenses.bind_device(lic['id'], DEV_A)
        r = self.c.post(f"/account/keys/{lic['id']}/computers/{DEV_A}/remove", data={'csrf': csrf})
        self.assertIn('times+a+month', r.headers['Location'])
        # someone else's key: refused
        other = licenses.create(email='someone@example.com')
        licenses.bind_device(other['id'], DEV_B)
        with self.assertRaises(licenses.LicenseError):
            licenses.self_remove_device(users.get(uid), other['id'], DEV_B)

    def test_admin_search_customer_filters_numbers_exports_and_bulk(self):
        self.api('lease', action='start')                       # computer A, no account (e.g. from GitHub)
        self.api('lease', action='start', device_id=DEV_B, device_name='Office PC')
        lic = licenses.create(email='pat@example.com', note='gift')
        licenses.bind_device(lic['id'], DEV_B)
        self.send()
        h = self.admin()
        res = self.c.get('/admin/api/search?q=pat').get_json()['results']
        self.assertEqual({r['type'] for r in res}, {'key', 'computer', 'request'})
        c = self.c.get('/admin/api/customer?who=pat@example.com').get_json()['customer']
        self.assertEqual(([k['note'] for k in c['keys']], [d['name'] for d in c['devices']], len(c['tickets'])),
                         (['gift'], ['Office PC'], 1))
        self.assertEqual(self.c.get('/admin/api/customer?who=nobody@example.com').status_code, 404)
        rows = self.c.get('/admin/api/filters/no_account').get_json()['rows']
        self.assertEqual([r['id'] for r in rows], [DEV_A])
        self.assertEqual(self.c.get('/admin/api/filters/at_limit').get_json()['rows'][0]['id'], lic['id'])
        m = self.c.get('/admin/api/metrics').get_json()['metrics']
        self.assertEqual((m['computers'], m['open_requests'], m['no_account']), (2, 1, 2))
        csv = self.c.get('/admin/export/computers.csv')
        self.assertEqual(csv.mimetype, 'text/csv')
        self.assertIn('Office PC', csv.get_data(as_text=True))
        self.assertIn('pat@example.com', csv.get_data(as_text=True))
        d = self.c.post('/admin/api/bulk', json={'action': 'free_time', 'minutes': 15, 'devices': [DEV_A, DEV_B]}, headers=h)
        self.assertEqual(d.get_json()['count'], 2)
        self.assertEqual(audit.recent()[0]['action'], 'bulk_free_time')
        team = self.c.post('/admin/api/team', json={'who': 'nobody@example.com', 'role': 'support'}, headers=h)
        self.assertEqual(team.status_code, 404)

    def test_github_downloads_counted(self):
        self.admin()
        with mock.patch.object(__import__('dopplesite').releases, '_fetch',
                               return_value={**__import__('tests.test_site', fromlist=['x']).RELEASE,
                                             'github_downloads': 42, 'github_releases_counted': 3}):
            __import__('dopplesite').releases.reset()
            a = self.c.get('/admin/api/app').get_json()
        self.assertEqual(a['github'], {'downloads': 42, 'releases': 3})

    def test_backup(self):
        support.db()
        path = backup.run()
        self.assertTrue(os.path.exists(path) and re.search(r'doppleganger-\d{4}-\d\d-\d\d\.sqlite$', path))
        self.assertEqual(backup.latest()[0], path)

    def test_old_closed_requests_are_deleted(self):
        self.send()
        t = support.search()[0][0]
        support.set_status(t['id'], 'closed')
        self.assertEqual(support.purge_old(), 0)
        self.assertEqual(support.purge_old(now=t['updated_at'] + (support.KEEP_CLOSED_DAYS + 1) * 86400), 1)
        self.assertIsNone(support.get(t['id']))
