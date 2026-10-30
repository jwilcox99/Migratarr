"""auth.py and the UI's sign-in: one login, hashed password, signed session cookies (0.2.0)."""
import http.client
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
import unittest
from urllib.parse import urlencode

from migratarr import auth
import migratarr_ui
from test_ui import Fixture, RUN, STORAGE, fake_usage

PASSWORD = 'correct horse battery'


class AccountTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.now = [1_800_000_000]
        self.accounts = auth.Accounts(Path(tmp.name, 'config', 'auth.json'), clock=lambda: self.now[0])

    def test_password_is_hashed_and_file_is_owner_only(self):
        self.accounts.set('josh', PASSWORD)
        text = self.accounts.path.read_text()
        self.assertNotIn(PASSWORD, text)
        self.assertTrue(json.loads(text)['password'].startswith('pbkdf2_sha256$600000$'))
        self.assertEqual(stat.S_IMODE(self.accounts.path.stat().st_mode), 0o600)
        self.assertFalse(list(self.accounts.path.parent.glob('.*.tmp')))

    def test_verify(self):
        self.accounts.set('josh', PASSWORD)
        self.assertTrue(self.accounts.verify('josh', PASSWORD))
        self.assertTrue(self.accounts.verify(' josh ', PASSWORD))
        self.assertFalse(self.accounts.verify('josh', PASSWORD + 'x'))
        self.assertFalse(self.accounts.verify('someone', PASSWORD))
        self.assertFalse(auth.Accounts(self.accounts.path.with_name('none.json')).verify('josh', PASSWORD))

    def test_create_refuses_to_overwrite_and_checks_input(self):
        self.accounts.set('josh', PASSWORD)
        with self.assertRaisesRegex(auth.AuthError, 'already exists'):
            self.accounts.set('other', PASSWORD)
        for name, password in (('', PASSWORD), ('josh', 'short'), ('a' * 65, PASSWORD), ('jo\nsh', PASSWORD)):
            with self.subTest(name=name), self.assertRaises(auth.AuthError):
                self.accounts.set(name, password, replace=True)
        self.assertEqual(auth.check_new_login('josh', PASSWORD, PASSWORD + 'x'), "The two passwords don't match.")

    def test_session_round_trip_expiry_and_tampering(self):
        self.accounts.set('jo.sh', PASSWORD)
        cookie = self.accounts.issue()
        self.assertEqual(self.accounts.session_user(cookie), 'jo.sh')
        head, signature = cookie.rsplit('.', 1)
        for bad in ('', 'x', head + '.' + 'A' * len(signature), cookie + 'x', cookie.replace('v1.', 'v2.')):
            self.assertIsNone(self.accounts.session_user(bad), bad)
        self.now[0] += auth.SESSION_DAYS * 86400 + 1
        self.assertIsNone(self.accounts.session_user(cookie))

    def test_changing_the_password_ends_sessions(self):
        self.accounts.set('josh', PASSWORD)
        cookie = self.accounts.issue()
        self.accounts.set('josh', PASSWORD + '!', replace=True)
        self.assertIsNone(self.accounts.session_user(cookie))

    def test_damaged_file_is_an_error_not_an_open_door(self):
        self.accounts.path.parent.mkdir(parents=True)
        self.accounts.path.write_text('{"username": 1}')
        with self.assertRaisesRegex(auth.AuthError, 'damaged'):
            self.accounts.exists()
        self.assertIsNone(self.accounts.session_user('v1.a.b.c'))

    def test_throttle(self):
        now = [0.0]
        throttle = auth.Throttle(clock=lambda: now[0])
        for _ in range(auth.Throttle.LIMIT):
            self.assertEqual(throttle.blocked_for('1.2.3.4'), 0)
            throttle.failed('1.2.3.4')
        self.assertGreater(throttle.blocked_for('1.2.3.4'), 0)
        self.assertEqual(throttle.blocked_for('5.6.7.8'), 0)
        now[0] += auth.Throttle.WINDOW
        self.assertEqual(throttle.blocked_for('1.2.3.4'), 0)

    def test_safe_next(self):
        for value, expected in (('/run/x', '/run/x'), ('', '/'), ('//evil.example/', '/'),
                                ('https://evil.example', '/'), ('/\\evil', '/'), ('/a\nb', '/')):
            self.assertEqual(migratarr_ui.safe_next(value), expected, value)


class SignInServerTests(unittest.TestCase):
    """The real handler over HTTP, listening on the network (require_login) with actions on."""
    require_login = True

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        Fixture(Path(tmp.name))
        self.accounts = auth.Accounts(Path(tmp.name, 'config', 'auth.json'))
        self.app = migratarr_ui.App(Path(tmp.name), STORAGE, {}, fake_usage, actions=True)
        self.server = migratarr_ui.ThreadingHTTPServer(
            ('127.0.0.1', 0), migratarr_ui.make_handler(self.app, self.accounts, self.require_login))
        self.server.RequestHandlerClass.log_message = lambda *a: None
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]
        self.host = f'media-host.lan:{self.port}'
        self.cookie = None

    def request(self, method, path, form=None, host=None, origin=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=30)
        headers = {'Host': host or self.host}
        body = None
        if form is not None:
            body = urlencode(form)
            headers['Content-Type'] = 'application/x-www-form-urlencoded'
            headers['Origin'] = origin or 'http://' + (host or self.host)
        if self.cookie:
            headers['Cookie'] = 'migratarr_session=' + self.cookie
        conn.request(method, path, body=body, headers=headers)
        response = conn.getresponse()
        data = response.read()
        conn.close()
        cookie = response.getheader('Set-Cookie')
        if cookie and cookie.startswith('migratarr_session='):
            self.cookie = cookie.split(';', 1)[0].split('=', 1)[1] or None
        return response, data.decode()

    def create(self):
        return self.request('POST', '/account/new', dict(username='josh', password=PASSWORD,
                                                          confirm=PASSWORD, next='/run/' + RUN))

    def test_first_visit_creates_the_login(self):
        response, _ = self.request('GET', '/run/' + RUN)
        self.assertEqual(response.status, 303)
        self.assertTrue(response.getheader('Location').startswith('/account/new?next='))
        self.assertEqual(self.request('GET', '/login')[0].getheader('Location'), '/account/new')
        response, body = self.request('GET', '/account/new')
        self.assertEqual(response.status, 200)
        self.assertIn('Create your login', body)
        self.assertIn("form-action 'self'", response.getheader('Content-Security-Policy'))
        response, body = self.request('POST', '/account/new', dict(username='josh', password=PASSWORD,
                                                                   confirm='different'))
        self.assertEqual(response.status, 400)
        self.assertIn('don&#x27;t match', body)
        self.assertFalse(self.accounts.exists())
        response, _ = self.create()
        self.assertEqual((response.status, response.getheader('Location')), (303, '/run/' + RUN))
        cookie = response.getheader('Set-Cookie')
        for part in ('HttpOnly', 'SameSite=Lax', 'Path=/', 'Max-Age='):
            self.assertIn(part, cookie)
        response, body = self.request('GET', '/run/' + RUN)
        self.assertEqual(response.status, 200)
        self.assertIn('Sign out', body)
        self.assertIn('>josh<', body)
        # Once it exists, nobody can create another from the browser.
        self.cookie = None
        self.assertEqual(self.request('POST', '/account/new', dict(username='x', password=PASSWORD,
                                                                   confirm=PASSWORD))[0].status, 409)

    def test_sign_in_sign_out(self):
        self.accounts.set('josh', PASSWORD)
        self.assertEqual(self.request('GET', '/')[0].getheader('Location'), '/login')
        response, body = self.request('POST', '/login', dict(username='josh', password='nope', next='/'))
        self.assertEqual(response.status, 401)
        self.assertIn('Wrong username or password', body)
        self.assertNotIn('nope', body)
        self.assertIsNone(self.cookie)
        response, _ = self.request('POST', '/login', dict(username='josh', password=PASSWORD,
                                                          next='//evil.example/x'))
        self.assertEqual(response.getheader('Location'), '/')
        response, body = self.request('GET', '/')
        self.assertEqual(response.status, 200)
        token = body.split('action="/logout"><input type="hidden" name="token" value="', 1)[1].split('"', 1)[0]
        response, _ = self.request('POST', '/logout', dict(token=token))
        self.assertEqual(response.getheader('Location'), '/login')
        self.assertIsNone(self.cookie)
        self.assertEqual(self.request('GET', '/')[0].status, 303)

    def test_every_page_api_and_action_needs_a_session(self):
        self.accounts.set('josh', PASSWORD)
        for path in ('/', '/run/' + RUN, '/settings', '/execution/' + RUN + '-0001'):
            self.assertEqual(self.request('GET', path)[0].status, 303, path)
        response, body = self.request('GET', '/api/runs')
        self.assertEqual(response.status, 401)
        self.assertEqual(json.loads(body), {'error': 'sign in first'})
        response, body = self.request('POST', '/plan', dict(token=self.app.token))
        self.assertEqual(response.status, 401)
        self.assertEqual(self.request('GET', '/static/app.css')[0].status, 200)
        # A forged or expired cookie is no session.
        self.cookie = 'v1.am9zaA.9999999999.forged'
        self.assertEqual(self.request('GET', '/')[0].status, 303)

    def test_wrong_passwords_are_slowed_down(self):
        self.accounts.set('josh', PASSWORD)
        for _ in range(auth.Throttle.LIMIT):
            self.assertEqual(self.request('POST', '/login', dict(username='josh', password='x'))[0].status, 401)
        response, body = self.request('POST', '/login', dict(username='josh', password=PASSWORD))
        self.assertEqual(response.status, 429)
        self.assertIn('Too many wrong passwords', body)
        self.assertIsNone(self.cookie)

    def test_cross_site_posts_are_refused(self):
        self.accounts.set('josh', PASSWORD)
        self.request('POST', '/login', dict(username='josh', password=PASSWORD))
        self.assertIsNotNone(self.cookie)
        response, _ = self.request('POST', '/logout', dict(token='x'), origin='http://evil.example')
        self.assertEqual(response.status, 403)
        response, _ = self.request('POST', '/login', dict(username='josh', password=PASSWORD),
                                   origin='http://localhost:' + str(self.port))
        self.assertEqual(response.status, 403)  # on the network, only the page's own origin counts

    def test_any_host_name_is_served_on_the_network(self):
        for host in ('media-host.lan:8765', '192.168.1.20:8765', 'localhost'):
            self.assertEqual(self.request('GET', '/login', host=host)[0].status, 303, host)  # to /account/new


class LocalOnlyTests(SignInServerTests):
    """Listening on localhost only: no login needed until one is set; localhost Host names only."""
    require_login = False

    def setUp(self):
        super().setUp()
        self.host = f'localhost:{self.port}'

    def test_first_visit_creates_the_login(self):
        self.assertEqual(self.request('GET', '/')[0].status, 200)  # as before 0.2.0
        self.assertEqual(self.request('GET', '/account/new')[0].getheader('Location'), '/')
        self.assertEqual(self.request('POST', '/account/new', dict(username='josh', password=PASSWORD,
                                                                   confirm=PASSWORD))[0].status, 409)
        self.assertFalse(self.accounts.exists())

    def test_any_host_name_is_served_on_the_network(self):
        self.assertEqual(self.request('GET', '/', host='media-host.lan:8765')[0].status, 421)

    def test_cross_site_posts_are_refused(self):
        self.accounts.set('josh', PASSWORD)
        response, _ = self.request('POST', '/login', dict(username='josh', password=PASSWORD),
                                   origin='http://evil.example')
        self.assertEqual(response.status, 403)
        response, _ = self.request('POST', '/login', dict(username='josh', password=PASSWORD),
                                   origin='http://127.0.0.1:' + str(self.port))
        self.assertEqual(response.status, 303)  # any localhost name, as before


class MainTests(unittest.TestCase):
    def run_main(self, argv, environ):
        import contextlib
        import io
        from unittest.mock import patch
        seen = {}

        class Fake:
            def __init__(self, address, handler):
                seen['address'] = address

            def serve_forever(self):
                raise KeyboardInterrupt

            def server_close(self):
                pass
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as home, \
                patch.object(migratarr_ui, 'ThreadingHTTPServer', Fake), \
                patch.object(migratarr_ui, 'load_context', return_value=(Path('.'), {}, {})), \
                patch.dict(os.environ, {'MIGRATARR_HOME': home}), contextlib.redirect_stdout(out):
            self.assertEqual(migratarr_ui.main(argv, environ=environ), 0)
        return seen['address'], out.getvalue()

    def test_host_and_port_from_the_environment(self):
        address, out = self.run_main([], {'MIGRATARR_HOST': '0.0.0.0', 'MIGRATARR_PORT': '7878'})
        self.assertEqual(address, ('0.0.0.0', 7878))
        self.assertIn('first visit creates it', out)
        self.assertIn("Don't expose this port to the internet", out)

    def test_flags_override_the_environment(self):
        address, _ = self.run_main(['--host', '127.0.0.1', '--port', '9000'], {'MIGRATARR_HOST': '0.0.0.0'})
        self.assertEqual(address, ('127.0.0.1', 9000))

    def test_bad_port_variable(self):
        with self.assertRaises(SystemExit):
            migratarr_ui.main([], environ={'MIGRATARR_PORT': 'eighty'})


if __name__ == '__main__':
    unittest.main()
