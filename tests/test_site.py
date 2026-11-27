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
            'DOWNLOADS_REQUIRE_SIGNIN': '0', 'NO_AUTO_BACKUP': '1', 'ADMIN_RECOVERY': '1', **self.extra_env})
        self.env.start()
        users.reset()
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

    def unlock(self, email, at=None):
        """Set up an authenticator for this (signed-in, staff) account and unlock admin with it."""
        from dopplesite import adminlock
        uid = users.db().execute('SELECT id FROM users WHERE email=?', (email,)).fetchone()['id']
        secret = adminlock.new_secret()
        users.db().execute('UPDATE users SET totp_secret=?, totp_last_step=0 WHERE id=?', (secret, uid))
        users.db().commit()
        code = adminlock.code_at(secret, int((at or time.time()) // 30))
        with self.c.session_transaction() as s:
            csrf = s.get('csrf') or ''
        if not csrf:
            self.c.get('/account')
            with self.c.session_transaction() as s:
                csrf = s['csrf']
        return self.c.post('/account/admin-unlock', data={'csrf': csrf, 'code': code}), secret, code


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
        self.assertIn('I%27d%20like', it)          # the apostrophe survives (no HTML escaping inside the email)
        self.assertIn('Using a work computer?', page)
        self.assertIn('Using a work computer?', self.c.get('/guide').get_data(as_text=True))
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
        self.assertEqual(self.c.get('/admin').status_code, 404)
        self.assertEqual(self.c.get('/admin/api/updates').status_code, 404)
        self.assertEqual(self.c.post('/admin/api/updates').status_code, 404)

    def test_ordinary_user_is_not_admin(self):
        self.sign_in_with_hh({'id': 'hh5', 'name': 'Pat', 'email': 'pat@example.com', 'email_verified': True})
        self.assertEqual(self.c.get('/admin').status_code, 404)
        page = self.c.get('/account').get_data(as_text=True)
        self.assertNotIn('admin-unlock', page)                  # nothing in the page hints at admin
        with self.c.session_transaction() as s:
            csrf = s['csrf']
        self.assertEqual(self.c.post('/account/admin-unlock', data={'csrf': csrf, 'code': '123456'}).status_code, 404)

    def test_admin_contact_becomes_admin_only_when_confirmed(self):
        self.sign_in_with_hh({'id': 'hh6', 'name': 'Boss', 'email': 'boss@example.com', 'email_verified': False})
        self.assertEqual(self.c.get('/admin').status_code, 404)
        self.sign_in_with_hh({'id': 'hh6', 'name': 'Boss', 'email': 'Boss@Example.com', 'email_verified': True})
        # an admin, but admin stays locked (and out of the menu) until they enter an authenticator code
        r = self.c.get('/admin')
        self.assertEqual((r.status_code, '/account' in r.headers['Location']), (302, True))
        page = self.c.get('/account').get_data(as_text=True)
        self.assertIn('id="admin-unlock"', page)
        self.assertNotIn('>Admin</a>', page)
        r, _, _ = self.unlock('boss@example.com')
        self.assertEqual(r.status_code, 303)
        self.assertTrue(r.headers['Location'].endswith('/admin'))
        self.assertEqual(self.c.get('/admin').status_code, 200)
        self.assertIn('>Admin</a>', self.c.get('/').get_data(as_text=True))


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


class SecurityTest(Base):
    def test_security_txt(self):
        for path in ('/.well-known/security.txt', '/security.txt'):
            r = self.c.get(path)
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.mimetype == 'text/plain')
            body = r.get_data(as_text=True)
            self.assertIn('Contact: mailto:support@happyhacks.io', body)
            self.assertIn('/security\n', body)
            exp = re.search(r'Expires: (\d{4}-\d\d-\d\d)T', body).group(1)
            days = (time.mktime(time.strptime(exp, '%Y-%m-%d')) - time.time()) / 86400
            self.assertTrue(100 < days < 365, days)

    def test_security_page(self):
        page = self.c.get('/security').get_data(as_text=True)
        self.assertIn('support@happyhacks.io', page)
        self.assertIn('brb.happyhacks.io/it', page)
        self.assertIn('sigstore verify github', page)
        self.assertIn('/security', self.c.get('/').get_data(as_text=True))   # footer link

    def test_headers_and_nonce(self):
        r = self.c.get('/')
        csp = r.headers['Content-Security-Policy']
        nonce = re.search(r"'nonce-([^']+)'", csp).group(1)
        page = r.get_data(as_text=True)
        scripts = re.findall(r'<script[^>]*>', page)
        self.assertTrue(scripts)
        for tag in scripts:
            self.assertIn(f'nonce="{nonce}"', tag)
        self.assertNotRegex(page, r'\son[a-z]+="')   # no inline handlers: the policy would block them
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertNotEqual(nonce, re.search(r"'nonce-([^']+)'", self.c.get('/').headers['Content-Security-Policy']).group(1))
        for name in ('X-Content-Type-Options', 'X-Frame-Options', 'Referrer-Policy', 'Permissions-Policy'):
            self.assertIn(name, r.headers)
        self.assertNotIn('Strict-Transport-Security', r.headers)   # plain http (tests): no HSTS
        self.assertIn('Strict-Transport-Security', self.c.get('/', base_url='https://dg.test').headers)


class SignOutTests(Base):
    def test_sign_out_also_signs_out_of_happy_hacks(self):
        self.sign_in_with_hh({'id': 'hh1', 'name': 'Pat', 'email': 'pat@example.com', 'email_verified': True})
        with self.c.session_transaction() as s:
            csrf = s['csrf']
        r = self.c.post('/signout', data={'csrf': csrf})
        self.assertEqual(r.status_code, 303)
        self.assertIn('/account/signout?return_to=', r.headers['Location'])
        with self.c.session_transaction() as s:
            self.assertNotIn('uid', s)


class NameShownTests(Base):
    def test_an_email_in_the_name_field_isnt_shown_as_a_name(self):
        self.sign_in_with_hh({'id': 'hh1', 'name': 'brash@example.com', 'email': 'busy@example.com', 'email_verified': True})
        page = self.c.get('/').get_data(as_text=True)
        self.assertIn('>Account</a>', page)
        self.assertNotIn('brash@example.com', page)
        self.assertIn('Signed in as busy@example.com.', self.c.get('/account').get_data(as_text=True))


class HiddenAdminTests(Base):
    extra_env = {'ADMIN_RECOVERY': '0', 'SUPPORT_NOTIFY': 'alerts@example.com'}

    def setUp(self):
        super().setUp()
        from dopplesite import mailer
        self.mail = mailer.sent
        self.mail.clear()

    def boss(self):
        self.sign_in_with_hh({'id': 'hh6', 'name': 'Boss', 'email': 'boss@example.com', 'email_verified': True})

    def test_totp_matches_rfc_6238(self):
        from dopplesite import adminlock
        secret = 'GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ'      # "12345678901234567890"
        self.assertEqual(adminlock.code_at(secret, 59 // 30), '287082')
        self.assertEqual(adminlock.code_at(secret, 1111111109 // 30), '081804')
        self.assertEqual(adminlock.check_code(secret, '287 082', 0, now=59), 1)
        self.assertIsNone(adminlock.check_code(secret, '287082', 1, now=59))   # already used
        self.assertIsNone(adminlock.check_code(secret, '000000', 0, now=59))

    def test_password_is_off_without_recovery(self):
        self.assertEqual(self.c.get('/admin/login').status_code, 404)
        self.assertEqual(self.c.post('/admin/login', data={'password': 'test-pw'}).status_code, 404)
        self.assertEqual(self.c.get('/admin').status_code, 404)

    def test_recovery_password_works_only_while_switched_on(self):
        with mock.patch.dict(os.environ, {'ADMIN_RECOVERY': '1'}):
            self.assertEqual(self.c.post('/admin/login', data={'password': 'test-pw'}).status_code, 302)
            self.assertEqual(self.c.get('/admin').status_code, 200)
        self.assertTrue(any('recovery password was used' in m['subject'] for m in self.mail))
        self.assertEqual(self.c.get('/admin').status_code, 404)    # switched off again: the session stops working

    def test_unlock_codes_alerts_and_expiry(self):
        self.boss()
        r, secret, code = self.unlock('boss@example.com')
        self.assertEqual(r.status_code, 303)
        unlocked = [m for m in self.mail if 'Admin was unlocked' in m['subject']][0]
        self.assertEqual(sorted(unlocked['to']), ['alerts@example.com', 'boss@example.com'])
        # lock again; the same code can't be used twice
        with self.c.session_transaction() as s:
            csrf = s['csrf']
        self.c.post('/admin/logout', data={'csrf': csrf})
        self.assertEqual(self.c.get('/admin/api/me').status_code, 401)
        r = self.c.post('/account/admin-unlock', data={'csrf': csrf, 'code': code})
        self.assertIn("didn't+work", r.headers['Location'])
        # 30 minutes without use: locked
        from dopplesite import adminlock
        uid = users.db().execute("SELECT id FROM users WHERE email='boss@example.com'").fetchone()['id']
        users.db().execute('UPDATE users SET totp_last_step=0 WHERE id=?', (uid,))
        users.db().commit()
        self.c.post('/account/admin-unlock', data={'csrf': csrf, 'code': adminlock.code_at(secret, int(time.time() // 30))})
        self.assertEqual(self.c.get('/admin').status_code, 200)
        with self.c.session_transaction() as s:
            s['admin_unlock'] = {**s['admin_unlock'], 'until': time.time() - 1}
        r = self.c.get('/admin')
        self.assertIn('n=admin_locked', r.headers['Location'])
        self.assertIn('locked itself', self.c.get('/account?n=admin_locked').get_data(as_text=True))

    def test_too_many_wrong_codes(self):
        self.boss()
        _, secret, _ = self.unlock('boss@example.com')
        with self.c.session_transaction() as s:
            s.pop('admin_unlock')
            csrf = s['csrf']
        uid = users.db().execute("SELECT id FROM users WHERE email='boss@example.com'").fetchone()['id']
        users.db().execute('UPDATE users SET totp_last_step=0 WHERE id=?', (uid,))
        users.db().commit()
        for _ in range(5):
            self.c.post('/account/admin-unlock', data={'csrf': csrf, 'code': '000000'})
        self.assertTrue(any('blocked after wrong codes' in m['subject'] for m in self.mail))
        from dopplesite import adminlock
        r = self.c.post('/account/admin-unlock', data={'csrf': csrf, 'code': adminlock.code_at(secret, int(time.time() // 30))})
        self.assertIn('Too+many', r.headers['Location'])
        self.assertEqual(self.c.get('/admin').status_code, 302)

    def test_no_authenticator_yet_and_server_setup(self):
        self.boss()
        page = self.c.get('/account').get_data(as_text=True)
        self.assertIn('python -m dopplesite.users totp boss@example.com', page)
        with mock.patch('builtins.print'):
            self.assertEqual(users.main(['totp', 'boss@example.com']), 0)
            self.assertEqual(users.main(['totp', 'nobody@example.com']), 1)
        self.assertIn('Authenticator code', self.c.get('/account').get_data(as_text=True))
        self.assertTrue(any('set up an admin authenticator' in m['subject'] for m in self.mail))

    def test_big_actions_are_emailed(self):
        self.boss()
        self.unlock('boss@example.com')
        with self.c.session_transaction() as s:
            h = {'X-CSRF-Token': s['csrf']}
        self.c.post('/admin/api/team', json={'who': 'boss@example.com', 'role': 'admin'}, headers=h)
        self.assertTrue(any('changed a team role' in m['subject'] for m in self.mail))
