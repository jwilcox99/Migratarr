"""read_api.py / migratarr_status.py: derived states, integrity, and the read-only guarantee."""
import builtins
import contextlib
import csv
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

import migratarr_status
import read_api
from executor_manifest import approval_is_current

RUN = '20260915T192959Z'
PLAN_FIELDS = ['media_type', 'title', 'current', 'recommended', 'source_path', 'target_path', 'size_gb',
               'source_disk', 'target_disk', 'transfer_type', 'decision_reason', 'status', 'blockers',
               'warnings']


def eid(n):
    return f'{RUN}-{n:04d}'


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_csv(path, fields, rows):
    stream = io.StringIO(newline='')
    writer = csv.DictWriter(stream, fieldnames=fields)
    writer.writeheader()
    writer.writerows(rows)
    Path(path).write_bytes(stream.getvalue().encode())


def write_sums(folder, names):
    (folder / 'SHA256SUMS').write_text(''.join(f'{sha(folder / n)}  {n}\n' for n in names))


def plan_row(n, status='READY_FOR_REVIEW', blockers=''):
    return dict(media_type='TV', title=f'Show {n}', current='Library', recommended='Archive',
                source_path=f'/mnt/nas/media03/TV/Library/S{n}', target_path=f'/mnt/nas/media04/TV/Archive/S{n}',
                size_gb=str(n), source_disk='media03', target_disk='media04',
                transfer_type='CROSS_DISK_TRANSFER', decision_reason='Old + cold', status=status,
                blockers=blockers, warnings='')


class Fixture:
    """A small base path: one run, its manifest, approvals and one journal per derived state."""

    def __init__(self, base):
        self.base = base
        run = base / 'runs' / RUN
        run.mkdir(parents=True)
        plan = [plan_row(n) for n in range(1, 12)] + [plan_row(99, 'BLOCKED', 'SOURCE_MISSING;DESTINATION_COLLISION')]
        write_csv(run / 'move_plan.csv', PLAN_FIELDS, plan)
        (run / 'metadata.json').write_text(json.dumps(dict(
            run_id=RUN, created_utc='2026-09-15T19:29:59+00:00', git=dict(commit='abc'),
            counts=dict(planned_moves=len(plan)))))
        write_sums(run, ['move_plan.csv', 'metadata.json'])

        manifest = base / 'manifests' / RUN
        manifest.mkdir(parents=True)
        rows = [dict(execution_id=eid(n), **plan_row(n), approved='NO', executed='NO', execution_result='')
                for n in range(1, 12)]
        write_csv(manifest / 'execution_manifest.csv', ['execution_id', *PLAN_FIELDS, 'approved', 'executed',
                                                        'execution_result'], rows)
        (manifest / 'manifest_metadata.json').write_text(json.dumps(dict(
            manifest_version=1, run_id=RUN, snapshot_verified=True, counts=dict(eligible_rows=11))))
        write_sums(manifest, ['execution_manifest.csv', 'manifest_metadata.json'])
        self.hash = sha(manifest / 'execution_manifest.csv')

        batch = dict(utc='2026-09-25T10:00:00+00:00', media_type='TV', transfer_type='CROSS_DISK_TRANSFER', size=2)
        history = [dict(action='APPROVE', execution_id=eid(1), manifest_sha256=self.hash, utc='t1')]
        history += [dict(action='APPROVE', execution_id=eid(n), manifest_sha256=self.hash, utc=batch['utc'],
                         batch=batch) for n in (2, 3)]
        history += [dict(action='APPROVE', execution_id=eid(5), manifest_sha256=self.hash, utc='t2'),
                    dict(action='REVOKE', execution_id=eid(5), manifest_sha256=self.hash, utc='t3'),
                    dict(action='APPROVE', execution_id=eid(9), manifest_sha256='other-hash', utc='t4')]
        (base / 'approvals').mkdir()
        (base / 'approvals' / (RUN + '.json')).write_text(json.dumps(dict(
            run_id=RUN, approved_execution_ids=[eid(1), eid(2), eid(3), eid(9)], history=history)))

        logs = base / 'execution_logs'
        logs.mkdir()
        (logs / 'executor.lock').write_text('')
        h = self.hash
        journals = {
            1: [('START', dict(live=True)), ('PREFLIGHT_OK', {}), ('COPY_INTENT', {}), ('COPIED', {}),
                ('SONARR_UPDATE_INTENT', {}), ('DELETE_INTENT', {}), ('SOURCE_REMOVED', {}),
                ('SUCCESS', dict(manifest_sha256=h))],
            2: [('START', dict(live=True)), ('PREFLIGHT_OK', {}), ('COPY_INTENT', {})],
            3: [('START', dict(live=True)), ('COPY_INTENT', {}),
                ('STOPPED', dict(error_type='TimeoutError', reason='uncertain copy'))],
            4: [('START', dict(live=True)), ('STOPPED', dict(error_type='Refused', reason='Series is locked'))],
            5: [('START', dict(live=False)), ('PREFLIGHT_OK', {}), ('CHECK_ONLY', {})],
            8: [('START', dict(live=True)), ('SUCCESS', dict(manifest_sha256='other-hash'))],
            9: [('START', dict(live=True)), ('COPY_INTENT', {}), ('STOPPED', dict(reason='timeout')),
                ('RECOVERY_STARTED', {}), ('SUCCESS', dict(manifest_sha256=h))],
            10: [('START', dict(live=True)), ('RENAME_INTENT', {}), ('STOPPED', dict(reason='EINVAL')),
                 ('START', dict(live=False)), ('PREFLIGHT_OK', {}), ('CHECK_ONLY', {})],
            # A recovery that began and then stopped: media may be half-reconciled.
            11: [('RECOVERY_STARTED', {}), ('STOPPED', dict(reason='Radarr permission denied'))],
        }
        for n, entries in journals.items():
            (logs / (eid(n) + '.jsonl')).write_text(''.join(
                json.dumps(dict(utc=f'2026-09-25T0{i}:00:00+00:00', execution_id=eid(n), event=e, **d)) + '\n'
                for i, (e, d) in enumerate(entries)))
        (logs / (eid(7) + '.jsonl')).write_text('{"event": "START", "live": true}\n{"event": "COPY')


EXPECTED_EXECUTION = {
    1: read_api.SUCCEEDED, 2: read_api.UNFINISHED, 3: read_api.NEEDS_RECONCILIATION,
    4: read_api.STOPPED_SAFE, 5: read_api.CHECKED, 6: read_api.NOT_STARTED, 7: read_api.JOURNAL_UNREADABLE,
    8: read_api.NEEDS_RECONCILIATION, 9: read_api.SUCCEEDED, 10: read_api.NEEDS_RECONCILIATION,
    11: read_api.NEEDS_RECONCILIATION,
}
EXPECTED_APPROVAL = {1: 'APPROVED', 2: 'APPROVED', 3: 'APPROVED', 4: 'UNAPPROVED', 5: 'REVOKED',
                     6: 'UNAPPROVED', 7: 'UNAPPROVED', 8: 'UNAPPROVED', 9: 'UNAPPROVED', 10: 'UNAPPROVED',
                     11: 'UNAPPROVED'}


class ReadApiTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.fx = Fixture(self.base)

    def test_every_execution_and_approval_state(self):
        status = read_api.run_status(self.base, RUN)
        self.assertTrue(status['integrity_ok'])
        rows = {row['execution_id']: row for row in status['rows']}
        for n, expected in EXPECTED_EXECUTION.items():
            with self.subTest(execution=n):
                self.assertEqual(rows[eid(n)]['execution'], expected)
                self.assertEqual(rows[eid(n)]['approval'], EXPECTED_APPROVAL[n])
        self.assertTrue(rows[eid(9)]['recovered'])
        self.assertFalse(rows[eid(1)]['recovered'])
        self.assertEqual(rows[eid(2)]['approval_batch_utc'], '2026-09-25T10:00:00+00:00')
        self.assertEqual(rows[eid(4)]['detail'], 'Series is locked')
        self.assertEqual(status['totals'][read_api.NEEDS_RECONCILIATION], dict(count=4, size_gb=32.0))

    def test_approval_state_matches_the_executors_rule(self):
        approvals = read_api.get_approvals(self.base, RUN, self.fx.hash)['states']
        def require(ok, message):
            if not ok:
                raise AssertionError(message)
        for n in range(1, 12):
            with self.subTest(execution=n):
                current = approval_is_current(self.base, RUN, eid(n), self.fx.hash, require)
                self.assertEqual(approvals.get(eid(n), {}).get('state') == 'APPROVED', current)

    def test_batches_grouped(self):
        batches = read_api.get_approvals(self.base, RUN, self.fx.hash)['batches']
        self.assertEqual([b['execution_ids'] for b in batches], [[eid(2), eid(3)]])

    def test_plan_includes_blocked_rows_with_codes(self):
        plan = read_api.get_plan(self.base, RUN)
        blocked = [r for r in plan['rows'] if r['status'] == 'BLOCKED']
        self.assertEqual([r['blocker_codes'] for r in blocked], [['SOURCE_MISSING', 'DESTINATION_COLLISION']])

    def test_tampered_manifest_or_run_is_reported_not_shown(self):
        path = self.base / 'manifests' / RUN / 'execution_manifest.csv'
        path.write_bytes(path.read_bytes() + b'x')
        status = read_api.run_status(self.base, RUN)
        self.assertFalse(status['integrity_ok'])
        self.assertEqual(status['rows'], [])
        self.assertIn('Checksum mismatch: execution_manifest.csv', status['integrity_problems'])
        plan = self.base / 'runs' / RUN / 'move_plan.csv'
        plan.write_bytes(plan.read_bytes() + b'x')
        self.assertFalse(read_api.list_runs(self.base)[0]['integrity_ok'])
        self.assertEqual(read_api.get_plan(self.base, RUN)['rows'], [])

    def test_invalid_ids_and_missing_runs_raise(self):
        for call in (lambda: read_api.run_status(self.base, '../etc'),
                     lambda: read_api.events(self.base, '../../x'),
                     lambda: read_api.get_plan(self.base, '20990101T000000Z')):
            with self.assertRaises(read_api.ReadError):
                call()

    def test_approval_record_retried_once_then_reported(self):
        path = self.base / 'approvals' / (RUN + '.json')
        good = path.read_text()
        reads = []
        original = Path.read_text
        def flaky(self_path, *args, **kwargs):
            if self_path == path:
                reads.append(1)
                return good[:10] if len(reads) == 1 else good
            return original(self_path, *args, **kwargs)
        with patch.object(Path, 'read_text', flaky), patch.object(read_api.time, 'sleep'):
            self.assertTrue(read_api.get_approvals(self.base, RUN, self.fx.hash)['record_present'])
        path.write_text('{"run_id":')
        with patch.object(read_api.time, 'sleep'), self.assertRaises(read_api.ReadError):
            read_api.get_approvals(self.base, RUN, self.fx.hash)

    def test_reads_never_write_or_lock(self):
        def snapshot():
            return {p: (p.stat().st_mtime_ns, p.read_bytes() if p.is_file() else None)
                    for p in sorted(self.base.rglob('*'))}
        before = snapshot()
        opened = []
        real_open = builtins.open
        def guarded_open(file, mode='r', *args, **kwargs):
            opened.append((str(file), mode))
            self.assertNotIn('executor.lock', str(file))
            self.assertFalse(set(mode) & set('wax+'), 'write-mode open of ' + str(file))
            return real_open(file, mode, *args, **kwargs)
        forbid = AssertionError('lock taken')
        with patch('builtins.open', guarded_open), patch('io.open', guarded_open), \
                patch.object(fcntl, 'flock', side_effect=forbid), patch.object(fcntl, 'lockf', side_effect=forbid), \
                contextlib.redirect_stdout(io.StringIO()):
            read_api.list_runs(self.base)
            read_api.get_plan(self.base, RUN)
            read_api.run_status(self.base, RUN)
            for n in range(1, 12):
                read_api.execution_state(self.base, eid(n), self.fx.hash)
            for argv in ([], ['--run', RUN], ['--execution', eid(1)], ['--run', RUN, '--json']):
                self.assertEqual(migratarr_status.main(['--base', str(self.base), *argv]), 0)
        self.assertTrue(opened)
        self.assertEqual(snapshot(), before)

    @unittest.skipIf(hasattr(os, 'geteuid') and os.geteuid() == 0, 'root ignores file permissions')
    def test_works_on_a_read_only_tree(self):
        paths = sorted(self.base.rglob('*'), key=lambda p: len(p.parts), reverse=True)

        def restore():  # so TemporaryDirectory can clean up
            self.base.chmod(0o700)
            for p in reversed(paths):
                p.chmod(0o700)
        self.addCleanup(restore)
        for p in paths:
            p.chmod(stat.S_IRUSR | (stat.S_IXUSR if p.is_dir() else 0))
        self.base.chmod(0o500)
        status = read_api.run_status(self.base, RUN)
        self.assertEqual(len(status['rows']), 11)


class StatusCliTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        Fixture(self.base)

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = migratarr_status.main(['--base', str(self.base), *argv])
        return rc, out.getvalue(), err.getvalue()

    def test_run_list_run_table_and_timeline(self):
        rc, out, _ = self.run_cli()
        self.assertEqual((rc, out.strip()), (0, f'{RUN} | planned 12 | manifest yes | OK'))
        rc, out, _ = self.run_cli('--run', RUN)
        self.assertEqual(rc, 0)
        self.assertIn(f'{eid(4)} | TV | Show 4 | Library -> Archive | 4 GB | UNAPPROVED | STOPPED_SAFE '
                      '(Series is locked)', out)
        self.assertIn('NEEDS_RECONCILIATION: 4 (32.00 GB)', out)
        rc, out, _ = self.run_cli('--execution', eid(3))
        self.assertIn('STOPPED {"error_type": "TimeoutError", "reason": "uncertain copy"}', out)

    def test_json_and_errors(self):
        rc, out, _ = self.run_cli('--run', RUN, '--json')
        self.assertEqual(json.loads(out)['totals'][read_api.SUCCEEDED]['count'], 2)
        rc, _, err = self.run_cli('--run', 'nope')
        self.assertEqual(rc, 2)
        self.assertIn('Invalid run ID', err)


if __name__ == '__main__':
    unittest.main()
