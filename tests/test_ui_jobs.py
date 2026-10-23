"""migratarr_jobs.py: one background job at a time; move jobs re-check every row and never approve."""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import approval_store
import migratarr_jobs as jobs
import read_api
from test_read_api import RUN, Fixture, eid

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / 'config' / 'runtime.example.json'


class FakeExecutors:
    """Stands in for the four executors: appends journal events the way they do."""

    def __init__(self, base, manifest_hash, refuse_check=(), fail_live=(), on_call=None):
        self.base, self.hash = base, manifest_hash
        self.refuse_check, self.fail_live, self.on_call = set(refuse_check), set(fail_live), on_call
        self.calls = []

    def journal(self, execution_id, *events):
        path = self.base / 'execution_logs' / (execution_id + '.jsonl')
        with path.open('a') as f:
            for name, extra in events:
                f.write(json.dumps(dict(utc='2026-10-23T00:00:00+00:00', execution_id=execution_id,
                                        event=name, **extra)) + '\n')

    def __call__(self, command, cwd=None, env=None):
        script, execution_id = Path(command[1]).name, command[2]
        live = '--execute' in command
        self.calls.append((script, execution_id, live))
        if self.on_call:
            self.on_call(execution_id, live)
        if not live:
            if execution_id in self.refuse_check:
                self.journal(execution_id, ('START', dict(live=False)),
                             ('STOPPED', dict(error_type='Refused', reason='Series is locked')))
                return SimpleNamespace(returncode=1)
            self.journal(execution_id, ('START', dict(live=False)), ('CHECK_ONLY', {}))
            return SimpleNamespace(returncode=0)
        if execution_id in self.fail_live:
            self.journal(execution_id, ('START', dict(live=True)), ('COPY_INTENT', {}),
                         ('STOPPED', dict(reason='NAS delete failed')))
            return SimpleNamespace(returncode=1)
        self.journal(execution_id, ('START', dict(live=True)), ('SUCCESS', dict(manifest_sha256=self.hash)))
        return SimpleNamespace(returncode=0)


class MoveJobTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.fx = Fixture(self.base)
        # Approve 0004 (stopped safely, 4 GB) and 0006 (not started, 6 GB). 0001-0003 are approved
        # already: 0001 moved, 0002 unfinished and 0003 needs reconciliation, so those are held.
        approval_store.approve(self.base, RUN, [eid(6), eid(4)], self.fx.hash)

    def job(self):
        return jobs.Job(self.base, '20261023T000000Z-run', 'run', RUN)

    def run_job(self, executors=None, ids=None, permissions=lambda path: [], snapshot=lambda: {}):
        executors = executors or FakeExecutors(self.base, self.fx.hash)
        job = self.job()
        with patch('builtins.print'):
            jobs.run_moves(job, RUN, ids, runner=executors, config_snapshot=snapshot, permissions=permissions)
        return job.record, executors

    def test_queue_is_approved_runnable_rows_smallest_first(self):
        queue, held, manifest_hash = jobs.move_queue(self.base, RUN)
        self.assertEqual([r['execution_id'] for r in queue], [eid(4), eid(6)])
        self.assertEqual({r['execution_id'] for r in held}, {eid(2), eid(3)})
        self.assertEqual(manifest_hash, self.fx.hash)

    def test_each_row_runs_check_only_then_live_with_its_executor(self):
        record, executors = self.run_job()
        self.assertEqual(executors.calls, [('execute_cross_tv.py', eid(4), False), ('execute_cross_tv.py', eid(4), True),
                                           ('execute_cross_tv.py', eid(6), False), ('execute_cross_tv.py', eid(6), True)])
        self.assertEqual(record['state'], jobs.FINISHED)
        self.assertEqual([d['execution_id'] for d in record['done']], [eid(4), eid(6)])
        self.assertTrue((self.base / 'jobs' / '20261023T000000Z-run.json').exists())
        self.assertFalse((self.base / 'jobs' / 'active.json').exists())
        states = {r['execution_id']: r['execution'] for r in read_api.run_status(self.base, RUN)['rows']}
        self.assertEqual((states[eid(4)], states[eid(6)]), (read_api.SUCCEEDED, read_api.SUCCEEDED))

    def test_only_the_confirmed_rows_run(self):
        record, executors = self.run_job(ids=[eid(6)])
        self.assertEqual({c[1] for c in executors.calls}, {eid(6)})

    def test_stop_request_is_honoured_between_moves_never_during_one(self):
        def stop_after_first(execution_id, live):
            if execution_id == eid(4) and live:
                (self.base / 'jobs' / '20261023T000000Z-run.stop').write_text('now')
        record, executors = self.run_job(FakeExecutors(self.base, self.fx.hash, on_call=stop_after_first))
        self.assertEqual(record['state'], jobs.STOPPED)
        self.assertEqual([d['execution_id'] for d in record['done']], [eid(4)])
        self.assertNotIn(eid(6), {c[1] for c in executors.calls})
        self.assertFalse((self.base / 'jobs' / '20261023T000000Z-run.stop').exists())

    def test_revoked_approval_is_skipped(self):
        def revoke_next(execution_id, live):
            if execution_id == eid(4) and live:
                approval_store.revoke(self.base, RUN, eid(6))
        record, executors = self.run_job(FakeExecutors(self.base, self.fx.hash, on_call=revoke_next))
        self.assertEqual(record['state'], jobs.FINISHED)
        self.assertNotIn(eid(6), {c[1] for c in executors.calls})
        self.assertIn('revoked', record['skipped'][-1]['reason'])

    def test_permission_problem_skips_the_row_before_any_executor_runs(self):
        def problems(path):
            return ['S6 is drwxr-sr-x (no group write)'] if path.endswith('/S6') else []
        record, executors = self.run_job(permissions=problems)
        self.assertNotIn(eid(6), {c[1] for c in executors.calls})
        self.assertIn('no group write', record['skipped'][-1]['reason'])
        self.assertEqual(read_api.execution_state(self.base, eid(6), self.fx.hash)['state'], read_api.NOT_STARTED)

    def test_check_only_refusal_skips_and_continues(self):
        record, executors = self.run_job(FakeExecutors(self.base, self.fx.hash, refuse_check=[eid(4)]))
        self.assertEqual(record['state'], jobs.FINISHED)
        self.assertNotIn(('execute_cross_tv.py', eid(4), True), executors.calls)
        self.assertIn(('execute_cross_tv.py', eid(6), True), executors.calls)
        self.assertIn('Series is locked', record['skipped'][-1]['reason'])

    def test_live_failure_stops_the_job(self):
        record, executors = self.run_job(FakeExecutors(self.base, self.fx.hash, fail_live=[eid(4)]))
        self.assertEqual(record['state'], jobs.FAILED)
        self.assertNotIn(eid(6), {c[1] for c in executors.calls})
        self.assertIn('Check its journal', record['message'])

    def test_manifest_or_config_change_stops_the_job(self):
        def edit_manifest(execution_id, live):
            if live:
                path = self.base / 'manifests' / RUN / 'execution_manifest.csv'
                path.write_bytes(path.read_bytes() + b'\n')
        record, _ = self.run_job(FakeExecutors(self.base, self.fx.hash, on_call=edit_manifest))
        self.assertEqual(record['state'], jobs.FAILED)
        self.assertIn('manifest changed', record['message'].lower())

        self.setUp()
        values = iter([{'a': 1}, {'a': 2}])
        record, executors = self.run_job(snapshot=lambda: next(values, {'a': 2}))
        self.assertEqual(record['state'], jobs.FAILED)
        self.assertEqual(executors.calls, [])


class PermissionTests(unittest.TestCase):
    def test_folders_without_group_write_are_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            show = Path(tmp) / 'Show'
            (show / 'Season 1').mkdir(parents=True)
            os.chmod(show, 0o2775)
            os.chmod(show / 'Season 1', 0o2775)
            self.assertEqual(jobs.permission_problems(str(show)), [])
            os.chmod(show / 'Season 1', 0o2755)
            problems = jobs.permission_problems(str(show))
            self.assertEqual(len(problems), 1)
            self.assertIn('Season 1 is drwxr-sr-x', problems[0])
            self.assertEqual(jobs.permission_problems(str(Path(tmp) / 'missing')), [])


class StartAndStatusTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        Fixture(self.base)

    def fake_popen(self, command, **kwargs):
        self.command, self.kwargs = command, kwargs
        return SimpleNamespace(pid=999999)

    def test_one_job_at_a_time_and_interrupted_jobs_must_be_dismissed(self):
        job_id = jobs.start_job(self.base, 'run', RUN, [eid(6)], popen=self.fake_popen)
        self.assertIn('--job', self.command)
        self.assertEqual(self.command[-4:], ['--run', RUN, '--id', eid(6)])
        self.assertTrue(self.kwargs['start_new_session'])
        self.assertEqual(jobs.active_job(self.base, alive=lambda pid, jid: True)['state'], jobs.STARTING)
        with patch.object(jobs, 'process_alive', lambda pid, jid: True):
            with self.assertRaisesRegex(jobs.JobError, 'already running'):
                jobs.start_job(self.base, 'plan', popen=self.fake_popen)
        # Its process is gone after the start grace period: reported, never silently cleared.
        record = json.loads((self.base / 'jobs' / 'active.json').read_text())
        record['started_epoch'] = time.time() - 60
        (self.base / 'jobs' / 'active.json').write_text(json.dumps(record))
        self.assertEqual(jobs.active_job(self.base)['state'], jobs.INTERRUPTED)
        with self.assertRaisesRegex(jobs.JobError, 'interrupted'):
            jobs.start_job(self.base, 'plan', popen=self.fake_popen)
        jobs.dismiss_interrupted(self.base, job_id)
        self.assertIsNone(jobs.active_job(self.base))
        self.assertEqual(jobs.get_job(self.base, job_id)['state'], jobs.INTERRUPTED)

    def test_a_live_runner_lock_refuses_a_new_job(self):
        import fcntl
        (self.base / 'jobs').mkdir()
        with open(self.base / 'jobs' / 'runner.lock', 'a') as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            with self.assertRaisesRegex(jobs.JobError, 'still running'):
                jobs.start_job(self.base, 'plan', popen=self.fake_popen)
        self.assertFalse((self.base / 'jobs' / 'active.json').exists())
        jobs.start_job(self.base, 'plan', popen=self.fake_popen)  # free again once released

    def test_job_ids_are_validated(self):
        for bad in ('../x', '', 'a' * 50, '20261023T000000Z-run/..'):
            self.assertIsNone(jobs.get_job(self.base, bad))
            self.assertEqual(jobs.log_tail(self.base, bad), '')

    def test_log_tail_keeps_the_latest_progress_line(self):
        (self.base / 'jobs').mkdir()
        (self.base / 'jobs' / 'x-run.log').write_text('one\nHashing 10%\rHashing 50%\rHashing 90%\ntwo\n')
        self.assertEqual(jobs.log_tail(self.base, 'x-run').split('\n')[:3], ['one', 'Hashing 90%', 'two'])

    def test_real_job_process_end_to_end(self):
        """A real detached runner with nothing runnable finishes cleanly and records itself."""
        env = dict(os.environ, MIGRATARR_CONFIG=str(EXAMPLE), MIGRATARR_BASE_PATH=self.base.as_posix())
        with patch.dict(os.environ, env):
            job_id = jobs.start_job(self.base, 'run', RUN, [eid(6)])
        deadline = time.time() + 30
        while time.time() < deadline and (jobs.active_job(self.base) or {}).get('state') in (jobs.STARTING, jobs.RUNNING):
            time.sleep(0.1)
        record = jobs.get_job(self.base, job_id)
        self.assertEqual(record['state'], jobs.FINISHED, jobs.log_tail(self.base, job_id))
        self.assertEqual(record['done'], [])  # 0006 isn't approved in the fixture, so nothing ran
        self.assertIn('0 moved', jobs.log_tail(self.base, job_id))


if __name__ == '__main__':
    unittest.main()
