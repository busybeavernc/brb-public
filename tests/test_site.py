import json
import os
import re
import tempfile
import time
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from dopplesite import admin, create_app, hhauth, releases, users

RELEASE = {'version': '0.1.30', 'published': '2026-11-01T12:00:00Z', 'url': 'https://github.com/x/y/releases/tag/v0.1.30',
           'assets': {'DopplegangerSetup.exe': {'url': 'https://dl/win.exe', 'size': 118_000_000},
                      'Doppleganger-mac-arm64.dmg': {'url': 'https://dl/arm.dmg', 'size': 300_000_000}}}


class Base(unittest.TestCase):
    extra_env = {}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {
            'DATA_DIR': self.tmp.name, 'SECRET_KEY': 'test-secret', 'ADMIN_PASSWORD': 'test-pw', 'UPDATES_DIR': self.tmp.name,
            'UPDATE_COMMAND': 'echo deploying; echo done', 'HAPPYHACKS_URL': 'https://hh.test', 'HAPPYHACKS_CLIENT_ID': 'doppleganger',
            'HAPPYHACKS_CLIENT_SECRET': 'shh', 'PUBLIC_URL': 'https://dg.test', 'ADMIN_CONTACTS': 'boss@example.com',
            'DOWNLOADS_REQUIRE_SIGNIN': '0', **self.extra_env})
        self.env.start()
        users.reset()
        admin._failures.clear()
        self.rel = mock.patch.object(releases, '_fetch', return_value=RELEASE)
        self.rel.start()
        releases.reset()
        app = create_app()
        app.config['SESSION_COOKIE_SECURE'] = False
        self.c = app.test_client()

    def tearDown(self):
        self.rel.stop()
        self.env.stop()
        users.reset()
        self.tmp.cleanup()

    def sign_in_with_hh(self, hh_user, next_path='/'):
        r = self.c.get('/auth/happyhacks?next=' + next_path)
        self.assertEqual(r.status_code, 303)
        q = parse_qs(urlsplit(r.headers['Location']).query)
        self.assertEqual(q['client_id'], ['doppleganger'])
        self.assertEqual(q['redirect_uri'], ['https://dg.test/auth/happyhacks/callback'])
        self.assertEqual(q['code_challenge_method'], ['S256'])
        sent = {}

        def fake_post(url, body):
            sent.update(url=url, body=parse_qs(body))
            return 200, json.dumps({'user': hh_user}).encode()

        with mock.patch.object(hhauth, '_post', fake_post):
            r = self.c.get(f"/auth/happyhacks/callback?code={'c' * 30}&state={q['state'][0]}")
        self.assertEqual(sent['url'], 'https://hh.test/account/token')
        self.assertEqual(sent['body']['client_secret'], ['shh'])
        return r


class SiteTests(Base):
    def test_pages(self):
        r = self.c.get('/')
        self.assertEqual(r.status_code, 200)
        page = r.get_data(as_text=True)
        self.assertIn('Be right back.', page)
        self.assertIn('Need a moment?', page)
        self.assertNotIn('Doppleganger<', page)
        self.assertIn('not</b> for faking attendance', page)
        self.assertIn('/eula', page)
        self.assertIn('Latest version 0.1.30', page)
        self.assertIn('/download/windows', page)
        self.assertIn('Sign in', page)
        self.assertIn('Download Free', page)
        self.assertIn('id="dl-modal"', page)
        self.assertIn('href="/#pricing"', page)
        self.assertIn('Like you never left', page)
        see = self.c.get('/see-it-in-action')
        self.assertEqual(see.status_code, 200)
        self.assertIn('Meanwhile, in the meeting', see.get_data(as_text=True))
        it = self.c.get('/it')
        self.assertEqual(it.status_code, 200)
        it = it.get_data(as_text=True)
        self.assertIn('Never leave the computer', it)
        self.assertIn('548A881E-7657-4231-8FEA-A9877D9743C9', it)
        self.assertIn('mailto:?subject=Software%20request', it)
        self.assertIn('/it', page)
        self.assertEqual(self.c.get('/for-it').status_code, 200)
        pp = self.c.get('/privacy').get_data(as_text=True)
        self.assertIn('without notice', pp)
        self.assertIn('never see or store your card details', pp)
        self.assertIn('/privacy', page)
        self.assertNotIn('fonts.googleapis.com', page)   # no third-party font service
        self.assertEqual(self.c.get('/guide').status_code, 200)
        eula = self.c.get('/eula').get_data(as_text=True)
        self.assertIn('What you must not use it for', eula)
        self.assertEqual(self.c.get('/terms').status_code, 200)
        self.assertEqual(self.c.get('/healthz').get_json(), {'status': 'ok'})
        self.assertEqual(self.c.get('/nope').status_code, 404)

    def test_downloads_redirect_and_count(self):
        r = self.c.get('/download/windows')
        self.assertEqual((r.status_code, r.headers['Location']), (302, 'https://dl/win.exe'))
        self.c.get('/download/windows')
        self.c.get('/download/mac-arm64')
        self.assertEqual(self.c.get('/download/mac-intel').status_code, 404)   # not in this release
        self.assertEqual(self.c.get('/download/linux').status_code, 404)
        st = users.download_stats()
        self.assertEqual(st['by_platform'], {'windows': 2, 'mac-arm64': 1})
        self.assertEqual(st['total'], 3)

    def test_brb_build_preferred_over_old_name(self):
        rel = dict(RELEASE, assets=dict(RELEASE['assets'], **{'BRBSetup.exe': {'url': 'https://dl/brb.exe', 'size': 1}}))
        releases.reset()
        with mock.patch.object(releases, '_fetch', return_value=rel):
            self.assertEqual(self.c.get('/download/windows').headers['Location'], 'https://dl/brb.exe')

    def test_github_down_still_downloads(self):
        releases.reset()
        with mock.patch.object(releases, '_fetch', side_effect=OSError('down')):
            r = self.c.get('/download/mac-intel')
        self.assertEqual(r.status_code, 302)
        self.assertIn('/releases/latest/download/BRB-mac-x86_64.dmg', r.headers['Location'])

    def test_sign_in_with_happy_hacks(self):
        r = self.sign_in_with_hh({'id': 'hh1', 'name': 'Pat Doe', 'email': 'pat@example.com', 'email_verified': True}, '/guide')
        self.assertEqual((r.status_code, r.headers['Location']), (303, '/guide'))
        page = self.c.get('/').get_data(as_text=True)
        self.assertIn('Pat', page)
        self.assertIn('Sign out', page)
        u = users.db().execute('SELECT * FROM users').fetchone()
        self.assertEqual((u['hh_id'], u['email'], u['role']), ('hh1', 'pat@example.com', 'user'))
        # a second sign-in finds the same user
        self.c.post('/signout', data={'csrf': 'x'})
        self.sign_in_with_hh({'id': 'hh1', 'name': 'Pat D', 'email': 'pat@example.com', 'email_verified': True})
        self.assertEqual(users.db().execute('SELECT COUNT(*) FROM users').fetchone()[0], 1)

    def test_callback_rejects_bad_state_and_foreign_next(self):
        r = self.c.get(f"/auth/happyhacks/callback?code={'c' * 30}&state=forged")
        self.assertEqual(r.status_code, 400)
        r = self.sign_in_with_hh({'id': 'hh2', 'name': 'X', 'email': 'x@example.com', 'email_verified': True}, '//evil.test')
        self.assertEqual(r.headers['Location'], '/')

    def test_signin_required_setting(self):
        with mock.patch.dict(os.environ, {'DOWNLOADS_REQUIRE_SIGNIN': '1'}):
            r = self.c.get('/download/windows')
            self.assertEqual(r.status_code, 303)
            self.assertIn('/auth/happyhacks', r.headers['Location'])
            self.assertIn('Sign in with Happy Hacks to download', self.c.get('/').get_data(as_text=True))
            self.sign_in_with_hh({'id': 'hh3', 'name': 'Sam', 'email': 's@example.com', 'email_verified': True})
            self.assertEqual(self.c.get('/download/windows').status_code, 302)


class AdminTests(Base):
    def test_requires_sign_in(self):
        self.assertEqual(self.c.get('/admin').status_code, 302)
        self.assertEqual(self.c.get('/admin/api/updates').status_code, 401)
        self.assertEqual(self.c.post('/admin/api/updates').status_code, 401)

    def test_ordinary_user_is_not_admin(self):
        self.sign_in_with_hh({'id': 'hh5', 'name': 'Pat', 'email': 'pat@example.com', 'email_verified': True})
        self.assertEqual(self.c.get('/admin').status_code, 302)
        self.assertIn("isn't a BRB admin", self.c.get('/admin/login').get_data(as_text=True))

    def test_admin_contact_becomes_admin_only_when_confirmed(self):
        self.sign_in_with_hh({'id': 'hh6', 'name': 'Boss', 'email': 'boss@example.com', 'email_verified': False})
        self.assertEqual(self.c.get('/admin').status_code, 302)
        self.sign_in_with_hh({'id': 'hh6', 'name': 'Boss', 'email': 'Boss@Example.com', 'email_verified': True})
        self.assertEqual(self.c.get('/admin').status_code, 200)
        self.assertIn('Admin', self.c.get('/').get_data(as_text=True))

    def test_password_lockout(self):
        for _ in range(5):
            self.assertEqual(self.c.post('/admin/login', data={'password': 'nope'}).status_code, 200)
        self.assertEqual(self.c.post('/admin/login', data={'password': 'test-pw'}).status_code, 429)

    def test_deploy_runs_and_logs(self):
        r = self.c.post('/admin/login', data={'password': 'test-pw'})
        self.assertEqual(r.status_code, 302)
        with self.c.session_transaction() as s:
            csrf = s['csrf']
        self.assertEqual(self.c.post('/admin/api/updates').status_code, 400)   # no CSRF header
        r = self.c.post('/admin/api/updates', headers={'X-CSRF-Token': csrf})
        if r.status_code == 409:   # this test folder isn't a git clone (e.g. an installed copy): nothing to run
            self.assertIn('Git clone', r.get_json()['error'])
            return
        self.assertEqual(r.status_code, 200)
        for _ in range(50):
            st = self.c.get('/admin/api/updates/log').get_json()
            if not st['running'] and st['exit'] is not None:
                break
            time.sleep(0.1)
        self.assertEqual(st['exit'], 0)
        self.assertIn('deploying', st['log'])

    def test_app_status(self):
        self.c.post('/admin/login', data={'password': 'test-pw'})
        self.c.get('/download/windows')
        a = self.c.get('/admin/api/app').get_json()
        self.assertEqual(a['release']['version'], '0.1.30')
        self.assertEqual([p['ready'] for p in a['platforms']], [True, True, False])
        self.assertEqual(a['stats']['by_platform'], {'windows': 1})
        self.assertTrue(a['sso'])
        page = self.c.get('/admin').get_data(as_text=True)
        self.assertIn('doppleganger-deploy', page)
        self.assertTrue(re.search(r'const CSRF = "[^"]+"', page))


if __name__ == '__main__':
    unittest.main()
