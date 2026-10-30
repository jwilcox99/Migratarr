"""migratarr_ui.py: read-only web view. Never writes or locks; loopback only; GET only."""
import builtins
import contextlib
import fcntl
import http.client
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import migratarr_ui
import read_api
from test_read_api import RUN, Fixture, eid

TB = 1024 ** 4
STORAGE = {d: {'local_path': '/mnt/nas/' + d} for d in ('media03', 'media04')}
USAGE = {'/mnt/nas/media03': (2.7 * TB, 1.2 * TB, 1.5 * TB), '/mnt/nas/media04': (2.7 * TB, 1.1 * TB, 1.6 * TB)}


def fake_usage(path):
    return USAGE[path]


class UiTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.fx = Fixture(self.base)
        self.app = migratarr_ui.App(self.base, STORAGE, {'media03': 50, 'media04': 50}, fake_usage)

    def get(self, path):
        status, kind, body = self.app.respond(path)
        return status, kind, body.decode()

    def test_runs_page_lists_the_run_with_progress_and_flags(self):
        status, kind, body = self.get('/')
        self.assertEqual((status, kind), (200, 'text/html; charset=utf-8'))
        self.assertIn(f'href="/run/{RUN}"', body)
        self.assertIn('2 of 11 moved', body)
        self.assertIn('4 needs attention', body)
        self.assertIn('1 in progress or interrupted', body)

    def test_run_page_groups_moves_by_what_to_do_about_them(self):
        status, _, body = self.get('/run/' + RUN)
        self.assertEqual(status, 200)
        order = [body.index(h) for h in ('In progress', 'Needs attention', 'Not approved',
                                          'Moved <span', 'Blocked by the planner')]
        self.assertEqual(order, sorted(order))
        self.assertIn('Source folder not found', body)          # blocked row, humanized code
        self.assertIn('Moved (recovered)', body)               # -0009
        self.assertIn('Stopped before changing anything', body)  # -0004
        self.assertIn('http-equiv="refresh"', body)            # -0002 is unfinished
        self.assertIn(f'href="/execution/{eid(9)}"', body)

    def test_disk_projection_counts_only_unfinished_cross_disk_moves(self):
        view = migratarr_ui.run_view(self.base, RUN, STORAGE, {}, fake_usage)
        unfinished = sum(n for n in range(1, 12) if n not in (1, 9))  # 1 and 9 succeeded
        disks = {d['disk']: d for d in view['disks']}
        self.assertAlmostEqual(disks['media03']['change_gb'], unfinished)
        self.assertAlmostEqual(disks['media04']['change_gb'], -unfinished)
        self.assertAlmostEqual(disks['media04']['projected_free_gb'], 1.6 * 1024 - unfinished)

    def test_unreadable_disk_is_reported_not_fatal(self):
        def broken(path):
            raise PermissionError(13, 'Permission denied')
        view = migratarr_ui.run_view(self.base, RUN, STORAGE, {}, broken)
        self.assertEqual({d['error'] for d in view['disks']}, {'Permission denied'})
        self.assertIn('Not readable: Permission denied', migratarr_ui.render_run(view))

    def test_run_header_omits_code_when_there_is_no_commit(self):
        view = migratarr_ui.run_view(self.base, RUN, STORAGE, {}, fake_usage)
        view['git_commit'] = 'abcdef0123456789'
        self.assertIn(', code abcdef0, ', migratarr_ui.render_run(view))
        view['git_commit'] = None  # an installed copy plans without a git checkout
        body = migratarr_ui.render_run(view)
        self.assertNotIn('code ,', body)
        self.assertIn(RUN + ', ' + str(len(view['rows'])) + ' moves in the manifest', body)

    def test_execution_timeline_and_long_reasons(self):
        status, _, body = self.get('/execution/' + eid(9))
        self.assertEqual(status, 200)
        for text in ('Started (live)', 'Copy starting', 'Recovery started', 'Done', '1h 0m later'):
            self.assertIn(text, body)
        self.assertNotIn('manifest sha256', body)
        status, _, body = self.get('/execution/' + eid(6))
        self.assertIn('has not been started yet', body)

    def test_refusal_traceback_in_an_older_journal_shows_as_one_plain_line(self):
        reason = ('NAS check failed or is uncertain: Traceback (most recent call last):\n'
                  '  File "<string>", line 9, in <module>\n  File "<string>", line 4, in nas_operation\n'
                  'Refused: NAS filesystems are not distinct [exit status 1]')
        journal = self.base / 'execution_logs' / (eid(4) + '.jsonl')
        journal.write_text(journal.read_text().rsplit('\n', 2)[0] + '\n'
                           + json.dumps(dict(event='STOPPED', error_type='Refused', reason=reason)) + '\n')
        status, _, body = self.get('/execution/' + eid(4))
        self.assertEqual(status, 200)
        self.assertNotIn('Traceback', body)
        self.assertIn('NAS check refused: the source and target disks are on the same filesystem', body)

    def test_json_api_matches_the_pages(self):
        for path in ('/api/runs', '/api/run/' + RUN, '/api/execution/' + eid(1)):
            status, kind, body = self.get(path)
            self.assertEqual((status, kind), (200, 'application/json'), path)
            json.loads(body)
        rows = json.loads(self.get('/api/run/' + RUN)[2])['rows']
        self.assertEqual({r['execution_id']: r['group'] for r in rows}[eid(2)], 'running')

    def test_unknown_or_malformed_addresses_are_404(self):
        for path in ('/nope', '/run/../../etc', '/run/2026', '/execution/' + RUN, '/run/20990101T000000Z',
                     '/static/../migratarr_ui.py'):
            self.assertEqual(self.get(path)[0], 404, path)

    def test_integrity_failure_shows_no_rows(self):
        manifest = self.base / 'manifests' / RUN / 'execution_manifest.csv'
        manifest.write_bytes(manifest.read_bytes() + b'\n')
        _, _, body = self.get('/run/' + RUN)
        self.assertIn('failed its integrity check', body)
        self.assertNotIn('Show 1', body)

    def test_titles_and_reasons_are_escaped(self):
        row = dict(execution_id=eid(1), title='<script>alert(1)</script>', media_type='TV', current='Library',
                   recommended='Archive', size_gb='1', transfer_type='CROSS_DISK_TRANSFER', source_disk='a',
                   target_disk='b', approval='APPROVED', execution='STOPPED_SAFE', last_event='STOPPED',
                   last_utc='x', detail='"><img src=x onerror=alert(1)>', recovered=False,
                   warning_codes=[], decision_reason='<b>', group='waiting')
        html = migratarr_ui.render_moves([row], 'waiting', 'Waiting', '')
        self.assertNotIn('<script>', html)
        self.assertNotIn('<img', html)
        self.assertIn('&lt;script&gt;', html)

    def test_pages_use_no_inline_styles_or_scripts(self):
        # The CSP allows only the served stylesheet; inline style attributes would be ignored.
        for path in ('/', '/run/' + RUN, '/execution/' + eid(9), '/nope'):
            body = self.get(path)[2]
            self.assertNotIn('style="', body, path)
            self.assertNotIn('<script', body, path)

    def test_reading_every_page_never_writes_or_locks(self):
        def snapshot():
            return {p: (p.stat().st_mtime_ns, p.read_bytes() if p.is_file() else None)
                    for p in sorted(self.base.rglob('*'))}
        before = snapshot()
        real_open = builtins.open

        def guarded_open(file, mode='r', *args, **kwargs):
            self.assertNotIn('executor.lock', str(file))
            self.assertFalse(set(mode) & set('wax+'), 'write-mode open of ' + str(file))
            return real_open(file, mode, *args, **kwargs)
        forbid = AssertionError('lock taken')
        with patch('builtins.open', guarded_open), patch('io.open', guarded_open), \
                patch.object(fcntl, 'flock', side_effect=forbid), patch.object(fcntl, 'lockf', side_effect=forbid):
            for path in ('/', '/run/' + RUN, '/api/runs', '/api/run/' + RUN, '/static/app.css',
                         *('/execution/' + eid(n) for n in range(1, 12)),
                         *('/api/execution/' + eid(n) for n in range(1, 12))):
                self.assertIn(self.get(path)[0], (200, 404), path)
        self.assertEqual(snapshot(), before)


class ServerTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        Fixture(Path(tmp.name))
        app = migratarr_ui.App(Path(tmp.name), STORAGE, {}, fake_usage)
        self.server = migratarr_ui.ThreadingHTTPServer(('127.0.0.1', 0), migratarr_ui.make_handler(app))
        self.server.RequestHandlerClass.log_message = lambda *a: None
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]

    def request(self, method, path, host=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        conn.request(method, path, headers={'Host': f'localhost:{self.port}' if host is None else host})
        response = conn.getresponse()
        body = response.read()
        conn.close()
        return response, body

    def test_get_works_with_security_headers(self):
        response, body = self.request('GET', '/run/' + RUN)
        self.assertEqual(response.status, 200)
        self.assertIn(b'Needs attention', body)
        csp = response.getheader('Content-Security-Policy')
        self.assertIn("default-src 'none'", csp)
        self.assertIn("style-src 'self'", csp)
        self.assertEqual(response.getheader('X-Content-Type-Options'), 'nosniff')
        self.assertEqual(response.getheader('Cache-Control'), 'no-store')

    def test_head_has_no_body(self):
        response, body = self.request('HEAD', '/')
        self.assertEqual((response.status, body), (200, b''))

    def test_any_other_method_is_refused(self):
        for method in ('POST', 'PUT', 'DELETE', 'PATCH', 'OPTIONS'):
            self.assertEqual(self.request(method, '/')[0].status, 405, method)

    def test_only_loopback_host_names_are_answered(self):
        for host in ('localhost', 'localhost:9000', '127.0.0.1:8765', '[::1]:8765'):
            self.assertEqual(self.request('GET', '/', host)[0].status, 200, host)
        for host in ('media-host.lan:8765', 'evil.example', '10.0.0.5:8765', ''):
            self.assertEqual(self.request('GET', '/', host)[0].status, 421, host)

    def test_main_binds_to_loopback_only(self):
        seen = {}

        class Fake:
            def __init__(self, address, handler):
                seen['address'] = address

            def serve_forever(self):
                raise KeyboardInterrupt

            def server_close(self):
                pass
        with patch.object(migratarr_ui, 'ThreadingHTTPServer', Fake), \
                patch.object(migratarr_ui, 'load_context', return_value=(Path('.'), {}, {})), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(migratarr_ui.main(['--port', '9123']), 0)
        self.assertEqual(seen['address'], ('127.0.0.1', 9123))


if __name__ == '__main__':
    unittest.main()
