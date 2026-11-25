from email.message import EmailMessage
from unittest import mock

from dopplesite import mailer, mailin, support

from .test_licenses import LicenseBase


def mail(frm, subject, body, mid, **headers):
    m = EmailMessage()
    m['From'], m['To'], m['Subject'], m['Message-ID'] = frm, 'support@happyhacks.io', subject, mid
    for k, v in headers.items():
        m[k.replace('_', '-')] = v
    m.set_content(body)
    return m


class MailInTests(LicenseBase):
    extra_env = {'NO_AUTO_BACKUP': '1', 'NO_MAILIN': '1', 'MAIL_FROM': 'BRB Support <support@happyhacks.io>'}

    def setUp(self):
        super().setUp()
        mailer.sent.clear()

    def test_new_email_becomes_a_request_and_replies_join_it(self):
        kind, t = mailin.take(mail('Pat Lee <pat@example.com>', 'Black screen in Teams',
                                   'Teams shows black when I choose the BRB camera.', '<a1@x>').as_bytes())
        self.assertEqual((kind, t['email'], t['name'], t['subject']), ('new', 'pat@example.com', 'Pat Lee', 'Black screen in Teams'))
        self.assertFalse([s for s in mailer.sent if s['to'] == ['pat@example.com']])   # no automatic answer by email
        support.staff_reply(t['id'], 'Sam', 'Choose HappyHacks Virtual Camera.')
        reply = mail('pat@example.com', f"Re: [BRB #{t['id']}] Black screen in Teams",
                     f"Thanks, that worked!\n\nOn Tue, Nov 25, 2026 at 3:00 PM BRB Support <support@happyhacks.io> wrote:\n> Choose HappyHacks",
                     '<a2@x>')
        reply.add_attachment(b'log text', maintype='text', subtype='plain', filename='brb.log')
        kind, t2 = mailin.take(reply.as_bytes())
        self.assertEqual((kind, t2['id'], t2['status']), ('reply', t['id'], 'open'))
        last = support.messages(t['id'])[-1]
        self.assertEqual(last['body'], 'Thanks, that worked!')
        self.assertTrue(last['attachment'].endswith('brb.log'))
        self.assertEqual(mailin.take(reply.as_bytes()), ('skip', 'already taken'))

    def test_someone_else_using_the_number_starts_a_new_request(self):
        _, t = mailin.take(mail('pat@example.com', 'Help', 'Something broke after updating today.', '<b1@x>').as_bytes())
        kind, t2 = mailin.take(mail('eve@example.com', f"Re: [BRB #{t['id']}] Help", 'Let me see it too please.', '<b2@x>').as_bytes())
        self.assertEqual(kind, 'new')
        self.assertNotEqual(t2['id'], t['id'])
        self.assertEqual(t2['subject'], 'Help')

    def test_automatic_and_own_mail_is_ignored(self):
        for m in (mail('pat@example.com', 'Out of office', 'Away until Monday', '<c1@x>', Auto_Submitted='auto-replied'),
                  mail('MAILER-DAEMON@mx.example.com', 'Undelivered', 'failed', '<c2@x>'),
                  mail('support@happyhacks.io', '[BRB #1] hi', 'our own copy', '<c3@x>'),
                  mail('news@example.com', 'Newsletter', 'deals', '<c4@x>', List_Id='<news.example.com>')):
            self.assertEqual(mailin.take(m.as_bytes())[0], 'skip')
        self.assertEqual(support.search()[1], 0)

    def test_reading_the_mailbox_only_takes_new_messages_and_changes_nothing(self):
        msgs = {101: mail('pat@example.com', 'One', 'First message body here.', '<d1@x>').as_bytes(),
                102: mail('kim@example.com', 'Two', 'Second message body here.', '<d2@x>').as_bytes()}
        calls = []

        class FakeIMAP:
            def __init__(self, *a, **k):
                self.untagged_responses = {'UIDVALIDITY': [b'7']}

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def login(self, u, p):
                calls.append(('login', u))

            def select(self, folder, readonly=False):
                calls.append(('select', readonly))
                return 'OK', [b'2']

            def uid(self, cmd, *args):
                calls.append((cmd, args))
                if cmd == 'SEARCH':
                    lo = int(args[1].split()[1].split(':')[0]) if args[1].startswith('UID') else 0
                    return 'OK', [b' '.join(str(u).encode() for u in msgs if u >= lo)]
                assert args[1] == '(BODY.PEEK[])'        # never marks anything read
                return 'OK', [(b'x', msgs[int(args[0])])]

        with mock.patch.dict('os.environ', {'IMAP_HOST': 'mail.example.com', 'SMTP_USER': 'support@happyhacks.io'}), \
                mock.patch.object(mailin.imaplib, 'IMAP4_SSL', FakeIMAP):
            self.assertEqual(mailin.check(), 2)
            self.assertEqual(mailin.check(), 0)          # nothing new: nothing taken twice
            msgs[103] = mail('lee@example.com', 'Three', 'Third message body here.', '<d3@x>').as_bytes()
            self.assertEqual(mailin.check(), 1)
        self.assertIn(('select', True), calls)
        self.assertEqual(support.search()[1], 3)
