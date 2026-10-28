"""Gate 3: migratarr.engine.run() end to end, for all four pairings, on this host.

Real manifest, approval, host lock, journal and the real shipped NAS programs
(topology local, test_local_transport's disks); only Radarr/Sonarr are the
offline fakes from the safety tests. Checks the result, the journal on disk
and the media, for check-only, a live move and a refusal.
"""
import contextlib
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import test_executor_safety as safety
import test_executor_safety_phase2 as phase2
import test_local_transport as local
import executor_command

REAL_SLEEP = executor_command.time.sleep

# The borrowed manifest helpers write safety.RUN into the metadata, so use its IDs.
RUN, EXECUTION = safety.RUN, safety.EXECUTION
CASES = {
    # name: (media, folder, target category, cross, file, fake Arr)
    'execute_movie_nas': ('Movie', 'Movies', 'Common', False, 'movie.mkv', phase2.MovieArr),
    'execute_tv_nas': ('TV', 'TV', 'Current', False, 'episode.mkv', phase2.Sonarr),
    'execute_cross_movie': ('Movie', 'Movies', 'Common', True, 'movie.mkv', safety.FakeRadarr),
    'execute_cross_tv': ('TV', 'TV', 'Current', True, 'episode.mkv', safety.FakeSonarr),
}
EVENTS = {
    False: ['START', 'PREFLIGHT_OK', 'RENAME_INTENT', 'RENAMED', '<UPDATE>', 'SUCCESS'],
    True: ['START', 'PREFLIGHT_OK', 'COPY_INTENT', 'COPIED', '<UPDATE>', 'DELETE_INTENT', 'SOURCE_REMOVED',
           'SUCCESS'],
}


class EngineRunTests(unittest.TestCase):
    # The manifest and approval as the approval step writes them.
    write_manifest = safety.OfflineTest.write_manifest
    write_checksums = safety.OfflineTest.write_checksums
    write_approval = safety.OfflineTest.write_approval

    def setUp(self):
        local.LocalTopologyTests.setUp(self)  # disks, local runtime, executors patched to them
        self.base = Path(self.temp.name).resolve() / 'base'
        self.base.mkdir()
        self.trace = []
        self.hook = lambda event: None
        # The NAS program runs for real; only its 1 s idle poll is shortened.
        fast = patch.object(executor_command.time, 'sleep', side_effect=lambda seconds: REAL_SLEEP(0.02))
        fast.start()
        self.addCleanup(fast.stop)

    def prepare(self, name):
        media, folder, target, cross, filename, fake = CASES[name]
        if cross and not self.cross_possible:
            self.skipTest('needs two filesystems (/tmp and /dev/shm)')
        self.m = self.modules[name]
        source_disk = 'disk2' if cross else 'disk1'
        self.src = self.disks[source_disk] / folder / 'Library' / 'Example'
        self.dst = self.disks['disk1'] / folder / target / 'Example'
        self.src.mkdir()
        self.content = b'engine run test media ' + name.encode()
        (self.src / filename).write_bytes(self.content)
        self.logical_src = f'/media/{folder}/Library/Example'
        self.logical_dst = f'/media/{folder}/{target}/Example'
        self.row = dict(execution_id=EXECUTION, media_type=media,
                        transfer_type='CROSS_DISK_TRANSFER' if cross else 'SAME_DISK_RENAME',
                        status='READY_FOR_REVIEW', blockers='', executed='NO', current='Library',
                        recommended=target, source_disk=source_disk, target_disk='disk1',
                        source_path=str(self.src), target_path=str(self.dst))
        self.folder = self.base / 'manifests' / RUN
        self.folder.mkdir(parents=True)
        self.approval_path = self.base / 'approvals' / (RUN + '.json')
        self.approval_path.parent.mkdir()
        self.write_manifest([self.row])
        self.approval = dict(run_id=RUN, approved_execution_ids=[EXECUTION], history=[
            dict(execution_id=EXECUTION, action='APPROVE', manifest_sha256=self.manifest_hash)])
        self.write_approval()
        self.arr = fake(self)
        service = 'Sonarr' if media == 'TV' else 'Radarr'
        p = patch.object(self.m, service, return_value=self.arr)
        p.start()
        self.addCleanup(p.stop)
        return cross, filename

    def main(self, *extra):
        out, err = io.StringIO(), io.StringIO()
        argv = ['x.py', EXECUTION, '--base', str(self.base), *extra]
        with patch('sys.argv', argv), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = self.m.main()
        return rc, out.getvalue(), err.getvalue()

    def journal(self):
        path = self.base / 'execution_logs' / (EXECUTION + '.jsonl')
        return [json.loads(line) for line in path.read_text().splitlines()]

    def test_check_only_then_live_move(self):
        for name in CASES:
            with self.subTest(executor=name):
                self.setUp_clean(name)
                cross, filename = self.prepare(name)
                update = 'SONARR_UPDATE_INTENT' if 'tv' in name else 'RADARR_UPDATE_INTENT'
                self.assertEqual(self.main()[:2], (0, 'CHECK_ONLY: ' + EXECUTION + '\n'))
                self.assertEqual([e['event'] for e in self.journal()], ['START', 'PREFLIGHT_OK', 'CHECK_ONLY'])
                self.assertTrue(self.src.exists())
                self.assertFalse(self.dst.exists())
                rc, out, err = self.main('--execute')
                self.assertEqual((rc, out), (0, 'SUCCESS: ' + EXECUTION + '\n'), err)
                events = self.journal()
                live = [e['event'] for e in events[3:] if not e['event'].startswith('NFS_VISIBILITY')]
                self.assertEqual(live, [update if e == '<UPDATE>' else e for e in EVENTS[cross]])
                self.assertEqual(events[-1]['manifest_sha256'], self.manifest_hash)
                self.assertTrue(all(e['execution_id'] == EXECUTION and 'utc' in e for e in events))
                self.assertFalse(self.src.exists())
                self.assertEqual((self.dst / filename).read_bytes(), self.content)
                self.assertEqual(self.arr.record['path'], self.logical_dst)
                self.assertEqual(list(self.dst.parent.glob('.migratarr-*')), [])
                self.assertEqual(list(self.src.parent.glob('.migratarr-*')), [])

    def test_refusal_is_journaled_as_stopped(self):
        for name in CASES:
            with self.subTest(executor=name):
                self.setUp_clean(name)
                self.prepare(name)
                self.arr.record['tags'] = [1]  # migratarr-lock
                rc, out, err = self.main('--execute')
                self.assertEqual((rc, out), (1, ''))
                journal = self.base / 'execution_logs' / (EXECUTION + '.jsonl')
                self.assertEqual(err.splitlines()[-2:], ['STOPPED: ' + self.journal()[-1]['reason'],
                                 'Inspect journal: ' + str(journal) + '. No automatic rollback or retry.'])
                last = self.journal()[-1]
                self.assertEqual((last['event'], last['error_type']), ('STOPPED', 'Refused'))
                self.assertTrue(self.src.exists())
                self.assertFalse(self.dst.exists())

    def test_run_returns_a_result(self):
        from migratarr.engine.run import run
        name = 'execute_movie_nas'
        self.prepare(name)
        with contextlib.redirect_stderr(io.StringIO()):
            result = run(self.m.NS, EXECUTION, base=self.base, live=False, media='Movie',
                         transfer='SAME_DISK_RENAME')
        self.assertEqual((result.status, result.last_event, result.error_type), ('CHECK_ONLY', 'CHECK_ONLY', None))
        self.assertEqual(result.journal_path, self.base / 'execution_logs' / (EXECUTION + '.jsonl'))

    def setUp_clean(self, name):
        # One pairing per subtest: fresh folders and journal, nothing left from the last one.
        folders = [self.base / p for p in ('execution_logs', 'manifests', 'approvals')]
        folders += [item for disk in self.disks.values() for item in disk.glob('*/*/*')]  # media from the last one
        for folder in folders:
            if folder.exists():
                for p in sorted(folder.rglob('*'), key=lambda p: len(p.parts), reverse=True):
                    p.rmdir() if p.is_dir() else p.unlink()
                folder.rmdir()
        self.trace, self.hook = [], (lambda event: None)


if __name__ == '__main__':
    unittest.main()
