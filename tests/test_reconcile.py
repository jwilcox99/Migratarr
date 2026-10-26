"""Phase 3 fold-ins: "mark reconciled" records, and exit status/signal on NAS failures."""
import fcntl
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import executor_command
import migratarr_jobs as jobs
import migratarr_reconcile
import migratarr_ui
import read_api
import reconcile_store
from test_read_api import RUN, Fixture, eid


class ReconcileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.f = Fixture(self.base)

    def state(self, n):
        return read_api.execution_state(self.base, eid(n), self.f.hash)

    def test_record_makes_move_reconciled(self):
        self.assertEqual(self.state(3)['state'], read_api.NEEDS_RECONCILIATION)
        entry = reconcile_store.record(self.base, eid(3), 'not_moved', 'Source intact; staging removed by hand',
                                       manifest_hash=self.f.hash)
        self.assertEqual(entry['state_before'], read_api.NEEDS_RECONCILIATION)
        state = self.state(3)
        self.assertEqual(state['state'], read_api.RECONCILED)
        self.assertEqual(state['detail'], 'Source intact; staging removed by hand')
        self.assertEqual(state['reconciled']['outcome'], 'not_moved')
        status = read_api.run_status(self.base, RUN)
        self.assertEqual(status['totals'][read_api.RECONCILED]['count'], 1)
        self.assertNotIn(read_api.RECONCILED, jobs.RUNNABLE_STATES)

    def test_later_attempt_undoes_the_record(self):
        reconcile_store.record(self.base, eid(3), 'other', 'checked', manifest_hash=self.f.hash)
        journal = read_api.journal_path(self.base, eid(3))
        with journal.open('a') as handle:
            handle.write(json.dumps(dict(utc='x', execution_id=eid(3), event='START', live=False)) + '\n')
        self.assertEqual(self.state(3)['state'], read_api.UNFINISHED)

    def test_unfinished_allowed_only_when_nothing_runs(self):
        lock = self.base / 'execution_logs' / 'executor.lock'
        with open(lock, 'a') as held:
            fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(reconcile_store.ReconcileError, 'an executor is running'):
                reconcile_store.record(self.base, eid(2), 'moved', 'done by hand', manifest_hash=self.f.hash)
        reconcile_store.record(self.base, eid(2), 'moved', 'done by hand', manifest_hash=self.f.hash)
        self.assertEqual(self.state(2)['state'], read_api.RECONCILED)

    def test_refusals(self):
        cases = [(4, 'moved', 'note'),          # STOPPED_SAFE: nothing to reconcile
                 (1, 'moved', 'note'),          # SUCCEEDED
                 (3, 'maybe', 'note'),          # unknown outcome
                 (3, 'moved', '   '),           # empty note
                 (3, 'moved', 'x' * 2001)]
        for n, outcome, note in cases:
            with self.subTest(n=n, outcome=outcome), self.assertRaises(reconcile_store.ReconcileError):
                reconcile_store.record(self.base, eid(n), outcome, note, manifest_hash=self.f.hash)
        self.assertFalse((self.base / 'execution_logs' / 'reconciled' / (eid(3) + '.json')).exists())

    def test_journal_untouched_and_history_kept(self):
        journal = read_api.journal_path(self.base, eid(3))
        before = journal.read_bytes()
        reconcile_store.record(self.base, eid(3), 'other', 'first look', manifest_hash=self.f.hash)
        with self.assertRaises(reconcile_store.ReconcileError):  # already RECONCILED
            reconcile_store.record(self.base, eid(3), 'other', 'second', manifest_hash=self.f.hash)
        self.assertEqual(journal.read_bytes(), before)
        self.assertEqual(len(reconcile_store.read_record(self.base, eid(3))['history']), 1)

    def test_unreadable_record_never_hides_a_flag(self):
        path = reconcile_store.record_path(self.base, eid(3))
        path.parent.mkdir(parents=True)
        path.write_text('{broken')
        self.assertEqual(self.state(3)['state'], read_api.NEEDS_RECONCILIATION)

    def test_cli(self):
        out = io.StringIO()
        with patch('sys.stderr', io.StringIO()):
            self.assertEqual(migratarr_reconcile.main([eid(3), '--base', str(self.base), '--outcome', 'not_moved',
                                                       '--note', 'checked'], out), 0)
        self.assertIn('RECONCILED: ' + eid(3), out.getvalue())
        out = io.StringIO()
        migratarr_reconcile.main([eid(3), '--base', str(self.base), '--show'], out)
        self.assertEqual(json.loads(out.getvalue())['state']['state'], read_api.RECONCILED)
        with patch('sys.stderr', io.StringIO()) as err:
            self.assertEqual(migratarr_reconcile.main([eid(4), '--base', str(self.base), '--outcome', 'moved',
                                                       '--note', 'x']), 1)
        self.assertIn('Not recorded', err.getvalue())

    def test_ui_form_and_action(self):
        app = migratarr_ui.App(self.base, actions=True, start_job=lambda *a: None)
        page = app.respond('/execution/' + eid(3), {})[2].decode()
        self.assertIn('Mark reconciled', page)
        self.assertNotIn('Mark reconciled', app.respond('/execution/' + eid(4), {})[2].decode())
        readonly = migratarr_ui.App(self.base, actions=False)
        self.assertNotIn('Mark reconciled', readonly.respond('/execution/' + eid(3), {})[2].decode())
        stale = app.respond_post(f'/execution/{eid(3)}/reconcile',
                                 {'token': [app.token], 'outcome': ['moved'], 'note': ['x'],
                                  'manifest_hash': ['old']})
        self.assertEqual(stale[0], 409)
        result = app.respond_post(f'/execution/{eid(3)}/reconcile',
                                  {'token': [app.token], 'outcome': ['not_moved'], 'note': ['source intact'],
                                   'manifest_hash': [self.f.hash]})
        self.assertEqual(result, ('redirect', '/execution/' + eid(3)))
        page = app.respond('/execution/' + eid(3), {})[2].decode()
        self.assertIn('Marked reconciled (not_moved)', page)
        self.assertIn('Reconciled by hand', page)


class ExitStatusTests(unittest.TestCase):
    def run_cmd(self, code, idle=True):
        def require(ok, message):
            if not ok:
                raise RuntimeError(message)
        command = [sys.executable, '-c', code]
        return executor_command.run_command(command, 'NAS copy', None, None if idle else 30, require=require,
                                            progress=lambda m: None, idle_timeout=60 if idle else None)

    def test_signal_and_status_are_recorded(self):
        kill = 'import os, signal, sys; print("copying", file=sys.stderr, flush=True); os.kill(os.getpid(), signal.SIGKILL)'
        with patch.object(executor_command.time, 'sleep', lambda s: None):
            with self.assertRaisesRegex(RuntimeError, r'NAS copy failed or is uncertain: copying \[killed by SIGKILL'):
                self.run_cmd(kill)
        with self.assertRaisesRegex(RuntimeError, r'failed or is uncertain: \[exit status 3\]'):
            self.run_cmd('import sys; sys.exit(3)', idle=False)

    def test_exit_text(self):
        self.assertEqual(executor_command.exit_text(-9), 'killed by SIGKILL on this host')
        self.assertIn('SSH error', executor_command.exit_text(255))
        self.assertEqual(executor_command.exit_text(137), 'exit status 137, which a shell reports for SIGKILL')
        self.assertEqual(executor_command.exit_text(1), 'exit status 1')


if __name__ == '__main__':
    unittest.main()
