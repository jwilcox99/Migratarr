"""migratarr_ui.py actions: plan -> confirm -> run, with tokens, stale-page refusals and no writes on GET."""
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode

import approval_store
import migratarr_jobs as jobs
import migratarr_ui
import read_api
from executor_manifest import approval_is_current
from test_read_api import RUN, Fixture, eid
from test_ui import STORAGE, fake_usage

TOKEN = 'test-token'


def refuse(ok, message):
    if not ok:
        raise AssertionError(message)


class ActionTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.fx = Fixture(self.base)
        self.started = []
        self.flagged = set()
        self.app = migratarr_ui.App(self.base, STORAGE, {}, fake_usage, actions=True, token=TOKEN,
                                    start_job=self.fake_start, permissions=self.fake_permissions)

    def fake_start(self, base, kind, run_id=None, execution_ids=()):
        self.started.append((kind, run_id, list(execution_ids)))
        return '20261023T120000Z-' + kind

    def fake_permissions(self, path):
        return [path + ' is drwxr-sr-x (no group write)'] if path in self.flagged else []

    def get(self, path, query=None):
        status, _, body = self.app.respond(path, query)
        return status, body.decode()

    def post(self, path, **fields):
        fields.setdefault('token', TOKEN)
        result = self.app.respond_post(path, {k: v if isinstance(v, list) else [v] for k, v in fields.items()})
        if result[0] == 'redirect':
            return 303, result[1]
        return result[0], result[2].decode()

    def approval_record(self):
        return approval_store.record_path(self.base, RUN).read_bytes()

    def test_run_page_shows_the_three_steps_and_selectable_rows(self):
        status, body = self.get('/run/' + RUN)
        self.assertEqual(status, 200)
        self.assertIn('class="steps"', body)
        self.assertNotIn('Read-only view', body)
        for n in (4, 5, 6):  # stopped safely, checked/revoked, not started: approvable
            self.assertIn(f'name="id" value="{eid(n)}"', body)
        for n in (1, 2, 3, 10):  # moved, running, approved, needs attention: not selectable
            self.assertNotIn(f'name="id" value="{eid(n)}"', body)
        self.assertIn(f'action="/run/{RUN}/review"', body)
        self.assertIn(f'name="token" value="{TOKEN}"', body)
        self.assertIn('Revoke', body)  # approved rows waiting to run

    def test_read_only_app_has_no_forms(self):
        app = migratarr_ui.App(self.base, STORAGE, {}, fake_usage)
        body = app.respond('/run/' + RUN)[2].decode()
        self.assertNotIn('<form', body)
        self.assertIn('Read-only view', body)
        self.assertEqual(app.respond_post('/plan', {'token': [app.token]})[0], 405)
        self.assertEqual(app.respond('/run/' + RUN + '/confirm-run')[0], 404)

    def test_missing_or_wrong_token_changes_nothing(self):
        before = self.approval_record()
        for token in ('', 'wrong'):
            status, body = self.post(f'/run/{RUN}/approve', token=token, id=[eid(6)], manifest_hash=self.fx.hash)
            self.assertEqual(status, 403)
            self.assertIn('Nothing was changed', body)
        self.assertEqual(self.post('/plan', token='wrong')[0], 403)
        self.assertEqual((self.approval_record(), self.started), (before, []))

    def test_review_then_approve(self):
        before = self.approval_record()
        self.flagged = {'/mnt/nas/media03/TV/Library/S6'}
        status, body = self.post(f'/run/{RUN}/review', id=[eid(6), eid(4)], pick='selected')
        self.assertEqual(status, 200)
        self.assertIn('Approve and start moving (2 moves, 10.0 GB)', body)
        self.assertIn('>Approve only<', body)
        self.assertIn('no group write', body)
        self.assertIn(f'name="manifest_hash" value="{self.fx.hash}"', body)
        self.assertEqual(self.approval_record(), before)  # reviewing writes nothing

        status, location = self.post(f'/run/{RUN}/approve', id=[eid(4), eid(6)], manifest_hash=self.fx.hash)
        self.assertEqual(status, 303)
        self.assertIn('done=approved', location)
        for n in (4, 6):
            self.assertTrue(approval_is_current(self.base, RUN, eid(n), self.fx.hash, refuse))
        _, body = self.get('/run/' + RUN, {'done': ['approved'], 'n': ['2']})
        self.assertIn('2 approved. Nothing has moved yet.', body)
        # Approving lands on #waiting: the next step is right there, not only in the steps at the top.
        waiting = body.split('id="waiting"', 1)[1].split('</section>', 1)[0]
        self.assertIn(f'href="/run/{RUN}/confirm-run"', waiting)
        self.assertIn('approved move', waiting)

    def test_approve_and_start_moving(self):
        before = self.approval_record()
        status, location = self.post(f'/run/{RUN}/approve', id=[eid(6), eid(4)], manifest_hash=self.fx.hash,
                                     then='start')
        self.assertEqual((status, location), (303, '/job/20261023T120000Z-run'))
        self.assertEqual(self.started, [('run', RUN, [eid(4), eid(6)])])  # only these, smallest first
        for n in (4, 6):
            self.assertTrue(approval_is_current(self.base, RUN, eid(n), self.fx.hash, refuse))
        self.assertNotEqual(self.approval_record(), before)

    def test_approve_and_start_while_a_job_runs_only_approves(self):
        self.write_active()
        patcher = patch.object(jobs, 'process_alive', lambda pid, job_id: True)
        patcher.start()
        self.addCleanup(patcher.stop)
        _, body = self.post(f'/run/{RUN}/review', id=[eid(4)], pick='selected')
        self.assertNotIn('Approve and start moving', body)
        self.assertIn('A job is running', body)
        status, location = self.post(f'/run/{RUN}/approve', id=[eid(4)], manifest_hash=self.fx.hash, then='start')
        self.assertIn('done=approved_busy', location)
        self.assertEqual(self.started, [])
        self.assertTrue(approval_is_current(self.base, RUN, eid(4), self.fx.hash, refuse))

    def test_approve_and_start_refuses_a_changed_manifest(self):
        status, body = self.post(f'/run/{RUN}/approve', id=[eid(4)], manifest_hash='0' * 64, then='start')
        self.assertNotEqual(status, 303)
        self.assertEqual(self.started, [])

    def test_review_all_selects_every_eligible_row(self):
        status, body = self.post(f'/run/{RUN}/review', pick='all')
        self.assertIn('Approve and start moving (3 moves', body)
        status, body = self.post(f'/run/{RUN}/review', pick='selected')
        self.assertEqual(status, 400)

    def test_stale_review_is_refused(self):
        before = self.approval_record()
        status, body = self.post(f'/run/{RUN}/approve', id=[eid(6)], manifest_hash='old')
        self.assertEqual(status, 409)
        self.assertIn('manifest changed', body)
        status, body = self.post(f'/run/{RUN}/approve', id=[eid(1)], manifest_hash=self.fx.hash)
        self.assertEqual(status, 409)  # already moved
        self.assertEqual(self.approval_record(), before)

    def test_revoke(self):
        self.assertEqual(self.post(f'/run/{RUN}/revoke', id=[eid(2)])[0], 303)
        self.assertFalse(approval_is_current(self.base, RUN, eid(2), self.fx.hash, refuse))

    def test_confirm_run_lists_the_queue_and_starts_only_what_was_shown(self):
        approval_store.approve(self.base, RUN, [eid(6), eid(4)], self.fx.hash)
        self.flagged = {'/mnt/nas/media03/TV/Library/S4'}
        status, body = self.get(f'/run/{RUN}/confirm-run')
        self.assertEqual(status, 200)
        self.assertIn('Start moving 1 (6.0 GB)', body)  # 0004 has a permission problem
        self.assertIn('will be skipped', body)
        self.assertIn('Approved but held', body)        # 0002 unfinished, 0003 needs reconciliation
        self.assertIn(f'name="id" value="{eid(6)}"', body)
        self.assertNotIn(f'name="id" value="{eid(4)}"', body)

        status, location = self.post(f'/run/{RUN}/start', id=[eid(6)], manifest_hash=self.fx.hash)
        self.assertEqual((status, location), (303, '/job/20261023T120000Z-run'))
        self.assertEqual(self.started, [('run', RUN, [eid(6)])])

        for fields in (dict(id=[eid(6)], manifest_hash='old'), dict(id=[eid(5)], manifest_hash=self.fx.hash),
                       dict(manifest_hash=self.fx.hash)):
            self.assertEqual(self.post(f'/run/{RUN}/start', **fields)[0], 409, fields)
        self.assertEqual(len(self.started), 1)

    def test_plan_starts_a_planning_job(self):
        self.assertEqual(self.post('/plan'), (303, '/job/20261023T120000Z-plan'))
        self.assertEqual(self.started, [('plan', None, [])])
        self.assertIn('Plan a new run', self.get('/')[1])

    def write_active(self, **fields):
        record = dict(job_id='20261023T120000Z-run', kind='run', run_id=RUN, state=jobs.RUNNING, pid=1,
                      started_utc='2026-10-23T12:00:00+00:00', started_epoch=0, queue=[eid(6)],
                      current=dict(execution_id=eid(6), title='Show 6', size_gb='6', index=1, total=1, step='moving'),
                      done=[], skipped=[], message='1/1: Show 6')
        record.update(fields)
        jobs.write_json(self.base / 'jobs' / 'active.json', record)
        (self.base / 'jobs' / '20261023T120000Z-run.log').write_text('Hashing 10%\rHashing 40%\n')

    def test_job_page_shows_progress_and_stop_request(self):
        self.write_active()
        patcher = patch.object(jobs, 'process_alive', lambda pid, job_id: True)
        patcher.start()
        self.addCleanup(patcher.stop)
        status, body = self.get('/job/20261023T120000Z-run')
        self.assertEqual(status, 200)
        self.assertIn('Stop after this move', body)
        self.assertIn('Hashing 40%', body)
        self.assertNotIn('Hashing 10%', body)
        self.assertIn('content="5"', body)
        _, run_page = self.get('/run/' + RUN)
        self.assertIn('Moves are running', run_page)
        self.assertNotIn('Run 1 approved', run_page)  # no second job while one runs
        self.assertEqual(self.post('/plan')[0], 303)  # start_job (faked here) is what refuses a second job
        self.assertEqual(self.post('/job/20261023T120000Z-run/stop'), (303, '/job/20261023T120000Z-run?done=stop'))
        self.assertTrue((self.base / 'jobs' / '20261023T120000Z-run.stop').exists())

    def test_job_output_is_newest_first(self):
        self.write_active()
        (self.base / 'jobs' / '20261023T120000Z-run.log').write_text('first line\nsecond line\nlatest line\n')
        _, body = self.get('/job/20261023T120000Z-run')
        log = body.split('<pre class="log">', 1)[1].split('</pre>', 1)[0]
        self.assertEqual(log.splitlines(), ['latest line', 'second line', 'first line'])
        self.assertIn('newest first', body)

    def test_interrupted_job_is_shown_and_can_be_dismissed(self):
        self.write_active(pid=999999)
        status, body = self.get('/job/20261023T120000Z-run')
        self.assertIn('Interrupted', body)
        self.assertIn('Dismiss', body)
        self.assertIn('stopped unexpectedly', self.get('/')[1])
        self.assertEqual(self.post('/job/20261023T120000Z-run/dismiss'), (303, '/'))
        self.assertIsNone(jobs.active_job(self.base))
        self.assertEqual(self.get('/job/20261023T120000Z-run')[0], 200)  # kept as a finished record

    def test_get_pages_with_actions_on_never_write(self):
        approval_store.approve(self.base, RUN, [eid(6)], self.fx.hash)
        self.write_active(pid=999999)

        def snapshot():
            return {p: p.read_bytes() if p.is_file() else None for p in sorted(self.base.rglob('*'))}
        before = snapshot()
        for path in ('/', '/run/' + RUN, f'/run/{RUN}/confirm-run', '/job/20261023T120000Z-run',
                     '/api/job/20261023T120000Z-run', '/execution/' + eid(6)):
            self.assertEqual(self.get(path)[0], 200, path)
        self.assertEqual(snapshot(), before)


class ActionServerTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.fx = Fixture(self.base)
        app = migratarr_ui.App(self.base, STORAGE, {}, fake_usage, actions=True, token=TOKEN)
        self.server = migratarr_ui.ThreadingHTTPServer(('127.0.0.1', 0), migratarr_ui.make_handler(app))
        self.server.RequestHandlerClass.log_message = lambda *a: None
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]

    def post(self, path, fields, **headers):
        body = urlencode(fields, doseq=True).encode()
        headers = {'Host': f'localhost:{self.port}', 'Content-Type': 'application/x-www-form-urlencoded', **headers}
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        conn.request('POST', path, body=body, headers=headers)
        response = conn.getresponse()
        data = response.read()
        conn.close()
        return response, data

    def test_approve_over_http_redirects_and_records(self):
        fields = dict(token=TOKEN, id=[eid(6)], manifest_hash=self.fx.hash)
        response, _ = self.post(f'/run/{RUN}/approve', fields, Origin=f'http://localhost:{self.port}',
                                **{'Sec-Fetch-Site': 'same-origin'})
        self.assertEqual(response.status, 303)
        self.assertTrue(response.getheader('Location').startswith(f'/run/{RUN}?done=approved'))
        self.assertTrue(approval_is_current(self.base, RUN, eid(6), self.fx.hash, refuse))

    def test_cross_site_and_malformed_posts_are_refused(self):
        before = approval_store.record_path(self.base, RUN).read_bytes()
        fields = dict(token=TOKEN, id=[eid(6)], manifest_hash=self.fx.hash)
        self.assertEqual(self.post(f'/run/{RUN}/approve', fields, Origin='http://evil.example')[0].status, 403)
        self.assertEqual(self.post(f'/run/{RUN}/approve', fields, **{'Sec-Fetch-Site': 'cross-site'})[0].status, 403)
        self.assertEqual(self.post(f'/run/{RUN}/approve', fields, Host='evil.example')[0].status, 421)
        self.assertEqual(self.post(f'/run/{RUN}/approve', fields, **{'Content-Type': 'text/plain'})[0].status, 400)
        self.assertEqual(self.post('/nope', fields)[0].status, 405)
        self.assertEqual(approval_store.record_path(self.base, RUN).read_bytes(), before)

    def test_forms_are_allowed_by_the_csp_only_when_actions_are_on(self):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        conn.request('GET', '/', headers={'Host': 'localhost'})
        response = conn.getresponse()
        response.read()
        conn.close()
        self.assertIn("form-action 'self'", response.getheader('Content-Security-Policy'))


if __name__ == '__main__':
    unittest.main()
