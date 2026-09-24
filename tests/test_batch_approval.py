"""Batch approval is a separate, recorded step; batch runners never approve.

Covers approve_execution.py --approve-batch (run as a subprocess against a
temporary base), executor_manifest.approval_is_current, and the batch
runners' approved/awaiting split. No NAS, Docker or HTTP access.
"""
import contextlib
import csv
import hashlib
import importlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch, Mock

from executor_manifest import approval_is_current, load_approved_plan
from planner_settings import load_settings
from runtime_config import load_config
from storage_targets import load_targets

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / 'config/runtime.example.json'
RUN = '20260915T192959Z'
COLUMNS = ['execution_id', 'media_type', 'transfer_type', 'status', 'blockers', 'executed',
           'current', 'recommended', 'source_path', 'target_path', 'source_disk', 'target_disk',
           'title', 'size_gb']


class Refused(RuntimeError):
    pass


def require(ok, message):
    if not ok:
        raise Refused(message)


def row(number, media='TV', transfer='CROSS_DISK_TRANSFER', size='1', status='READY_FOR_REVIEW',
        blockers=''):
    folder = 'TV' if media == 'TV' else 'Movies'
    return dict(execution_id=f'{RUN}-{number:04d}', media_type=media, transfer_type=transfer,
                status=status, blockers=blockers, executed='NO', current='Library', recommended='Archive',
                source_path=f'/mnt/nas/media03/{folder}/Library/T{number}',
                target_path=f'/mnt/nas/media04/{folder}/Archive/T{number}',
                source_disk='media03', target_disk='media04', title=f'T{number}', size_gb=size)


def write_manifest(base, rows):
    folder = base / 'manifests' / RUN
    folder.mkdir(parents=True)
    stream = io.StringIO(newline='')
    writer = csv.DictWriter(stream, fieldnames=COLUMNS)
    writer.writeheader()
    writer.writerows(rows)
    (folder / 'execution_manifest.csv').write_bytes(stream.getvalue().encode())
    (folder / 'manifest_metadata.json').write_text(json.dumps(
        dict(manifest_version=1, run_id=RUN, snapshot_verified=True)))
    (folder / 'SHA256SUMS').write_text(''.join(
        hashlib.sha256((folder / name).read_bytes()).hexdigest() + '  ' + name + '\n'
        for name in ('execution_manifest.csv', 'manifest_metadata.json')))
    return hashlib.sha256((folder / 'execution_manifest.csv').read_bytes()).hexdigest()


def write_approvals(base, history, approved):
    folder = base / 'approvals'
    folder.mkdir(exist_ok=True)
    (folder / (RUN + '.json')).write_text(json.dumps(
        dict(run_id=RUN, approved_execution_ids=sorted(approved), history=history)))


class ApprovalIsCurrentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.id = RUN + '-0001'

    def current(self, manifest_hash='h'):
        return approval_is_current(self.base, RUN, self.id, manifest_hash, require)

    def test_missing_record_means_not_approved(self):
        self.assertFalse(self.current())

    def test_latest_approve_for_this_hash_is_current(self):
        write_approvals(self.base, [dict(action='APPROVE', execution_id=self.id, manifest_sha256='h')],
                        [self.id])
        self.assertTrue(self.current())
        self.assertFalse(self.current('other-hash'))

    def test_revoked_or_unlisted_is_not_current(self):
        write_approvals(self.base, [dict(action='APPROVE', execution_id=self.id, manifest_sha256='h'),
                                    dict(action='REVOKE', execution_id=self.id, manifest_sha256='h')], [])
        self.assertFalse(self.current())
        write_approvals(self.base, [dict(action='APPROVE', execution_id=self.id, manifest_sha256='h')], [])
        self.assertFalse(self.current())

    def test_malformed_or_wrong_run_record_refuses(self):
        (self.base / 'approvals').mkdir()
        (self.base / 'approvals' / (RUN + '.json')).write_text(json.dumps(dict(run_id=RUN)))
        with self.assertRaises(Refused):
            self.current()
        (self.base / 'approvals' / (RUN + '.json')).write_text(json.dumps(
            dict(run_id='20990101T000000Z', approved_execution_ids=[], history=[])))
        with self.assertRaises(Refused):
            self.current()


class ApproveBatchCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.rows = [row(1, size='5'), row(2, size='1'), row(3, size='3'),
                     row(4, blockers='CUMULATIVE_DESTINATION_SPACE'), row(5, media='Movie'),
                     row(6, transfer='SAME_DISK_RENAME'), row(7, size='2'), row(8, size='4')]
        self.hash = write_manifest(self.base, self.rows)
        # 0007 is already approved individually; 0008 already succeeded.
        write_approvals(self.base, [dict(action='APPROVE', execution_id=RUN + '-0007',
                                         manifest_sha256=self.hash)], [RUN + '-0007'])
        logs = self.base / 'execution_logs'
        logs.mkdir()
        (logs / (RUN + '-0008.jsonl')).write_text(json.dumps(dict(event='SUCCESS')) + '\n')

    def approve(self, *extra):
        env = dict(os.environ, MIGRATARR_CONFIG=str(EXAMPLE), MIGRATARR_BASE_PATH=self.base.as_posix())
        return subprocess.run([sys.executable, str(ROOT / 'approve_execution.py'), '--run', RUN, *extra],
                              cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)

    def record(self):
        return json.loads((self.base / 'approvals' / (RUN + '.json')).read_text())

    def test_preview_approves_nothing(self):
        before = self.record()
        result = self.approve('--approve-batch', '--media', 'TV', '--transfer', 'CROSS_DISK_TRANSFER')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('PREVIEW ONLY', result.stdout)
        self.assertEqual(self.record(), before)

    def test_yes_approves_only_eligible_rows_smallest_first_with_limit(self):
        result = self.approve('--approve-batch', '--media', 'TV', '--transfer', 'CROSS_DISK_TRANSFER',
                              '--limit', '2', '--yes')
        self.assertEqual(result.returncode, 0, result.stderr)
        record = self.record()
        new = [e for e in record['history'] if 'batch' in e]
        # Eligible: 0001 (5 GB), 0002 (1 GB), 0003 (3 GB). Excluded: blocked 0004, Movie 0005,
        # same-disk 0006, already approved 0007, already succeeded 0008. Limit keeps the 2 smallest.
        self.assertEqual([e['execution_id'] for e in new], [RUN + '-0002', RUN + '-0003'])
        self.assertEqual(sorted(record['approved_execution_ids']),
                         [RUN + '-0002', RUN + '-0003', RUN + '-0007'])
        self.assertTrue(all(e['action'] == 'APPROVE' and e['manifest_sha256'] == self.hash
                            and e['batch']['size'] == 2 for e in new))
        self.assertEqual(len({e['batch']['utc'] for e in new}), 1)
        # The executors' own loader accepts a batch approval exactly like a single one.
        for execution_id in (RUN + '-0002', RUN + '-0003'):
            loaded, manifest_hash = load_approved_plan(self.base, execution_id, 'TV',
                                                       'CROSS_DISK_TRANSFER', require)
            self.assertEqual((loaded['execution_id'], manifest_hash), (execution_id, self.hash))
        with self.assertRaises(Refused):
            load_approved_plan(self.base, RUN + '-0001', 'TV', 'CROSS_DISK_TRANSFER', require)

    def test_batch_options_are_validated(self):
        self.assertEqual(self.approve('--approve-batch', '--media', 'TV').returncode, 2)
        self.assertEqual(self.approve('--status', '--yes').returncode, 2)
        self.assertEqual(self.approve('--approve-batch', '--media', 'TV', '--transfer',
                                      'CROSS_DISK_TRANSFER', '--limit', '0').returncode, 2)


class BatchRunnerApprovalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        config = load_config(EXAMPLE, environ={})
        with patch('runtime_config.get_config', return_value=config), \
                patch('planner_settings.get_settings',
                      return_value=load_settings(ROOT / 'config/planner.example.json')), \
                patch('media_layout.get_targets',
                      return_value=load_targets(ROOT / 'config/storage-targets.example.json')), \
                patch('subprocess.run', side_effect=AssertionError('external command')), \
                patch('subprocess.check_output', side_effect=AssertionError('external command')):
            cls.modules = {name: importlib.import_module(name)
                           for name in ('batch_cross_movies', 'batch_cross_tv')}

    def run_batch(self, module_name, pending_name, approved_ids, revoke_after_first=False):
        batch = self.modules[module_name]
        rows = [dict(execution_id=f'{RUN}-000{i}', title=f'T{i}', size_gb=str(i)) for i in (1, 2, 3)]
        calls = []
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            (base / 'execution_logs').mkdir()
            history = [dict(action='APPROVE', execution_id=i, manifest_sha256='hash') for i in approved_ids]
            write_approvals(base, history, approved_ids)

            def runner(command, **kwargs):
                calls.append(command)
                if '--execute' in command:
                    (base / 'execution_logs' / (command[2] + '.jsonl')).write_text(
                        json.dumps(dict(event='SUCCESS', manifest_sha256='hash')) + '\n')
                    if revoke_after_first:
                        write_approvals(base, history + [dict(action='REVOKE', execution_id=i,
                                                              manifest_sha256='hash')
                                                         for i in approved_ids], [])
                return Mock(returncode=0)
            out = io.StringIO()
            with patch.object(batch, pending_name, return_value=(rows, 0, 'hash')), \
                    contextlib.redirect_stdout(out):
                self.assertEqual(batch.run_batch(base, RUN, True, runner), 0)
        self.output = out.getvalue()
        return {command[2] for command in calls}, calls

    def test_only_approved_rows_run_and_nothing_is_approved(self):
        for module_name, pending_name in (('batch_cross_movies', 'pending_movies'),
                                          ('batch_cross_tv', 'pending_series')):
            with self.subTest(batch=module_name):
                executed, calls = self.run_batch(module_name, pending_name, [RUN + '-0001', RUN + '-0003'])
                self.assertEqual(executed, {RUN + '-0001', RUN + '-0003'})
                self.assertIn(RUN + '-0002 | T2 | 2 GB | AWAITING APPROVAL', self.output)
                self.assertIn('approved and pending: 2; awaiting approval: 1', self.output)
                self.assertFalse(any('approve_execution.py' in part for c in calls for part in c))

    def test_no_approvals_runs_nothing(self):
        for module_name, pending_name in (('batch_cross_movies', 'pending_movies'),
                                          ('batch_cross_tv', 'pending_series')):
            with self.subTest(batch=module_name):
                executed, calls = self.run_batch(module_name, pending_name, [])
                self.assertEqual(calls, [])

    def test_approval_revoked_mid_batch_skips_remaining_rows(self):
        executed, _ = self.run_batch('batch_cross_tv', 'pending_series',
                                     [RUN + '-0001', RUN + '-0002'], revoke_after_first=True)
        self.assertEqual(executed, {RUN + '-0001'})


if __name__ == '__main__':
    unittest.main()
