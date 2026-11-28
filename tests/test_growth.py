"""Funnel counting, crash reports, and the welcome / check-in emails."""
import hashlib
import time
from unittest import mock

from dopplesite import growth, mailer, users

from .test_site import Base

H = lambda n: hashlib.sha256(n.encode()).hexdigest()  # noqa: E731
DEV = {'device_id': H('a'), 'device_name': 'PC', 'platform': 'windows', 'app_version': '1.0.145'}


class FunnelTests(Base):
    def test_status_and_lease_fill_the_funnel(self):
        self.assertEqual(self.c.post('/api/v1/license/status', json=DEV).status_code, 200)
        f = growth.funnel(30)
        self.assertEqual((f['installs'], f['loop'], f['on_air']), (1, 0, 0))
        self.c.post('/api/v1/license/status', json={**DEV, 'has_loop': True})
        self.assertEqual(growth.funnel(30)['loop'], 1)
        r = self.c.post('/api/v1/license/lease', json={**DEV, 'action': 'start'})
        self.assertTrue(r.get_json()['ok'])
        f = growth.funnel(30)
        self.assertEqual((f['installs'], f['on_air'], f['unlimited']), (1, 1, 0))
        # a second computer that only opened BRB
        self.c.post('/api/v1/license/status', json={**DEV, 'device_id': H('b')})
        self.assertEqual(growth.funnel(30)['installs'], 2)

    def test_ran_out_is_counted(self):
        growth.note_status(DEV, {'unlimited': False, 'plan': 'free', 'free_left': 0})
        self.assertEqual(growth.funnel(30)['ran_out'], 1)
        growth.note_status(DEV, {'unlimited': True, 'plan': 'subscription', 'free_left': 0})
        self.assertEqual(growth.funnel(30)['unlimited'], 1)

    def test_admin_funnel_card(self):
        self.c.post('/admin/login', data={'password': 'test-pw'})
        self.c.post('/api/v1/license/status', json=DEV)
        d = self.c.get('/admin/api/funnel?days=7').get_json()
        self.assertEqual(d['installs'], 1)
        self.assertIn('site_downloads', d)


class CrashTests(Base):
    REPORT = ('Traceback (most recent call last):\n'
              '  File "C:\\Users\\Ann Smith\\AppData\\Local\\BRB\\engine.py", line 10, in run\n'
              '    x = 1 / 0\nZeroDivisionError: division by zero\n')

    def test_report_is_scrubbed_and_grouped(self):
        r = self.c.post('/api/v1/crash', json={**DEV, 'kind': 'crash', 'report': self.REPORT})
        self.assertEqual(r.status_code, 200)
        self.c.post('/api/v1/crash', json={**DEV, 'device_id': H('c'), 'kind': 'crash', 'report': self.REPORT})
        rows = growth.crashes()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]['count'], rows[0]['computers']), (2, 2))
        self.assertEqual(rows[0]['summary'], 'ZeroDivisionError: division by zero')
        full = growth.crash(rows[0]['id'])
        self.assertNotIn('Ann', full['report'])
        self.assertIn('C:\\Users\\~\\AppData', full['report'])

    def test_empty_report_refused(self):
        self.assertEqual(self.c.post('/api/v1/crash', json={**DEV, 'report': ' '}).status_code, 400)

    def test_admin_crash_card(self):
        self.c.post('/api/v1/crash', json={**DEV, 'kind': 'crash', 'report': self.REPORT})
        self.c.post('/admin/login', data={'password': 'test-pw'})
        rows = self.c.get('/admin/api/crashes').get_json()['rows']
        self.assertEqual(rows[0]['status'], 'new')
        cid = rows[0]['id']
        self.assertIn('ZeroDivisionError', self.c.get(f'/admin/api/crashes/{cid}').get_json()['report'])
        self.assertEqual(growth.crash(cid)['status'], 'seen')
        self.assertIn('Crash reports', self.c.get('/admin').get_data(as_text=True))


class EmailTests(Base):
    HH = {'id': 77, 'email': 'pat@example.com', 'email_verified': True, 'name': 'Pat Lee'}

    def setUp(self):
        super().setUp()
        mailer.sent.clear()

    def subjects(self):
        return [m['subject'] for m in mailer.sent]

    def test_welcome_once_on_first_sign_in(self):
        self.sign_in_with_hh(self.HH)
        self.assertEqual(self.subjects(), ['Welcome to BRB'])
        body = mailer.sent[0]['body']
        self.assertIn('Hi Pat,', body)
        self.assertIn('HappyHacks Virtual Camera', body)
        self.assertIn('/email/unsubscribe?u=', body)
        self.c.post('/signout')
        self.sign_in_with_hh(self.HH)
        self.assertEqual(self.subjects(), ['Welcome to BRB'])

    def test_checkin_after_a_week_and_unsubscribe(self):
        self.sign_in_with_hh(self.HH)
        u = users.db().execute('SELECT * FROM users').fetchone()
        self.assertEqual(growth.send_checkins(), 0)            # too soon
        week = time.time() + 7 * 86400 + 60
        mailer.sent.clear()
        with mock.patch.object(mailer, 'send', wraps=mailer.send) as send:
            growth.send_checkins(now=week)
            self.assertEqual(send.call_args.kwargs['headers']['List-Unsubscribe-Post'], 'List-Unsubscribe=One-Click')
        self.assertEqual(self.subjects(), ['How is BRB working for you?'])
        self.assertIn('#pricing', mailer.sent[0]['body'])
        self.assertEqual(growth.send_checkins(now=week + 3600), 0)   # only once
        # unsubscribe: GET asks, POST does it; a wrong token does nothing
        url = growth.unsubscribe_url(u['id']).split('dg.test')[1]
        self.assertIn('Stop BRB tips?', self.c.get(url).get_data(as_text=True))
        self.assertEqual(self.c.post('/email/unsubscribe', data={'u': u['id'], 't': 'wrong'}).status_code, 400)
        r = self.c.post('/email/unsubscribe', data={'u': u['id'], 't': growth._token(u['id'])})
        self.assertIn("You're unsubscribed", r.get_data(as_text=True))
        self.assertEqual(users.db().execute('SELECT emails_off FROM users').fetchone()[0], 1)

    def test_old_accounts_never_get_a_checkin(self):
        self.sign_in_with_hh(self.HH)
        self.assertEqual(growth.send_checkins(now=time.time() + 30 * 86400), 0)
