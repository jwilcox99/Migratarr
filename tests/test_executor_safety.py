"""Offline characterization of Phase One executor refusal and ordering contracts.

No CLI entrypoints, NAS helpers, Docker commands or HTTP clients are invoked.
Only disposable test media is copied/deleted, by a fake transport under TemporaryDirectory.
"""
import copy
import csv
import hashlib
import importlib
import io
import json
from contextlib import ExitStack
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from planner_settings import load_settings, parse_settings
from storage_targets import load_targets
from runtime_config import load_config

ROOT = Path(__file__).resolve().parents[1]
RUN = '20260915T192959Z'
EXECUTION = RUN + '-0001'
EXECUTORS = ('execute_movie', 'execute_movie_nas', 'execute_tv_nas', 'execute_cross_movie',
             'execute_cross_tv')


class OfflineTest(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for target in ('subprocess.Popen', 'subprocess.run', 'subprocess.check_output',
                       'socket.create_connection'):
            self.stack.enter_context(patch(target, side_effect=AssertionError('External I/O forbidden')))
        self.config = load_config(ROOT / 'config/runtime.example.json', environ={})
        self.settings = load_settings(ROOT / 'config/planner.example.json')
        self.targets = load_targets(ROOT / 'config/storage-targets.example.json')
        with patch('runtime_config.get_config', return_value=self.config), \
                patch('planner_settings.get_settings', return_value=self.settings), \
                patch('media_layout.get_targets', return_value=self.targets):
            self.modules = {name: importlib.import_module(name) for name in EXECUTORS}
        self.base = Path(self.stack.enter_context(tempfile.TemporaryDirectory())).resolve()
        for module in self.modules.values():
            self.stack.enter_context(patch.object(module, 'RUNTIME', self.config))
            if hasattr(module, 'progress'):
                self.stack.enter_context(patch.object(module, 'progress'))

    def fixture(self, name):
        media = 'TV' if name in ('execute_tv_nas', 'execute_cross_tv') else 'Movie'
        cross = name in ('execute_cross_movie', 'execute_cross_tv')
        row = dict(execution_id=EXECUTION, media_type=media,
                   transfer_type='CROSS_DISK_TRANSFER' if cross else 'SAME_DISK_RENAME',
                   status='READY_FOR_REVIEW', blockers='', executed='NO', current='Library',
                   recommended='Common' if media == 'Movie' else 'Current',
                   source_disk='media04', target_disk='media01' if cross else 'media04')
        folder = 'Movies' if media == 'Movie' else 'TV'
        row['source_path'] = f'/mnt/nas/{row["source_disk"]}/{folder}/Library/Example'
        row['target_path'] = f'/mnt/nas/{row["target_disk"]}/{folder}/{row["recommended"]}/Example'
        self.row = row
        self.folder = self.base / 'manifests' / RUN
        self.folder.mkdir(parents=True, exist_ok=True)
        self.approval_path = self.base / 'approvals' / (RUN + '.json')
        self.approval_path.parent.mkdir(exist_ok=True)
        self.write_manifest([row])
        self.approval = dict(run_id=RUN, approved_execution_ids=[EXECUTION], history=[
            dict(execution_id=EXECUTION, action='APPROVE', manifest_sha256=self.manifest_hash)])
        self.write_approval()
        return row

    def write_manifest(self, rows):
        stream = io.StringIO(newline='')
        writer = csv.DictWriter(stream, fieldnames=list(self.row))
        writer.writeheader()
        writer.writerows(rows)
        raw = stream.getvalue().encode()
        self.manifest_hash = hashlib.sha256(raw).hexdigest()
        (self.folder / 'execution_manifest.csv').write_bytes(raw)
        (self.folder / 'manifest_metadata.json').write_text(json.dumps(
            dict(manifest_version=1, run_id=RUN, snapshot_verified=True)))
        self.write_checksums()

    def write_checksums(self):
        (self.folder / 'SHA256SUMS').write_text(''.join(
            hashlib.sha256((self.folder / name).read_bytes()).hexdigest() + '  ' + name + '\n'
            for name in ('execution_manifest.csv', 'manifest_metadata.json')))

    def write_approval(self):
        self.approval_path.write_text(json.dumps(self.approval))


class ManifestSafetyTests(OfflineTest):
    def test_valid_approved_manifest_for_each_executor(self):
        for name, module in self.modules.items():
            with self.subTest(executor=name):
                row = self.fixture(name)
                self.assertEqual(module.load_plan(self.base, EXECUTION), (row, self.manifest_hash))

    def test_tampered_manifest_or_metadata_refused(self):
        for name, module in self.modules.items():
            for filename in ('execution_manifest.csv', 'manifest_metadata.json'):
                with self.subTest(executor=name, filename=filename):
                    self.fixture(name)
                    p = self.folder / filename
                    p.write_bytes(p.read_bytes() + b' ')
                    with self.assertRaisesRegex(module.Refused, 'Checksum mismatch'):
                        module.load_plan(self.base, EXECUTION)

    def test_rehashed_manifest_still_requires_matching_approval(self):
        for name, module in self.modules.items():
            with self.subTest(executor=name):
                self.fixture(name)
                self.row['source_path'] += ' changed'
                self.write_manifest([self.row])
                with self.assertRaisesRegex(module.Refused, 'exact manifest hash'):
                    module.load_plan(self.base, EXECUTION)

    def test_revoked_missing_or_wrong_run_approval_refused(self):
        for name, module in self.modules.items():
            for change in ('revoked', 'missing', 'wrong_run', 'empty_history'):
                with self.subTest(executor=name, change=change):
                    self.fixture(name)
                    if change == 'revoked':
                        self.approval['history'].append(dict(execution_id=EXECUTION, action='REVOKE'))
                    elif change == 'missing':
                        self.approval['approved_execution_ids'] = []
                    elif change == 'wrong_run':
                        self.approval['run_id'] = '20200101T000000Z'
                    else:
                        self.approval['history'] = []
                    self.write_approval()
                    with self.assertRaises(module.Refused):
                        module.load_plan(self.base, EXECUTION)

    def test_duplicate_execution_or_checksum_entries_refused(self):
        for name, module in self.modules.items():
            for duplicate in ('row', 'checksum'):
                with self.subTest(executor=name, duplicate=duplicate):
                    self.fixture(name)
                    if duplicate == 'row':
                        self.write_manifest([self.row, self.row])
                    else:
                        p = self.folder / 'SHA256SUMS'
                        p.write_text(p.read_text() * 2)
                    with self.assertRaisesRegex(module.Refused, 'Duplicate'):
                        module.load_plan(self.base, EXECUTION)

    def test_blocked_or_already_executed_rows_refused(self):
        for name, module in self.modules.items():
            for field, value in (('blockers', 'COLLISION'), ('status', 'BLOCKED'), ('executed', 'YES')):
                with self.subTest(executor=name, field=field):
                    self.fixture(name)
                    self.row[field] = value
                    self.write_manifest([self.row])
                    self.approval['history'][0]['manifest_sha256'] = self.manifest_hash
                    self.write_approval()
                    with self.assertRaises(module.Refused):
                        module.load_plan(self.base, EXECUTION)

    def test_invalid_metadata_columns_and_approval_shape_refused(self):
        for name, module in self.modules.items():
            for invalid in ('metadata', 'columns', 'approval'):
                with self.subTest(executor=name, invalid=invalid):
                    self.fixture(name)
                    if invalid == 'metadata':
                        (self.folder / 'manifest_metadata.json').write_text(json.dumps(
                            dict(manifest_version=1, run_id=RUN, snapshot_verified=False)))
                        self.write_checksums()
                    elif invalid == 'columns':
                        del self.row['target_disk']
                        self.write_manifest([self.row])
                        self.approval['history'][0]['manifest_sha256'] = self.manifest_hash
                        self.write_approval()
                    else:
                        self.approval['history'] = 'not-a-list'
                        self.write_approval()
                    with self.assertRaises(module.Refused):
                        module.load_plan(self.base, EXECUTION)

    def test_executor_media_and_transfer_scope_remain_local(self):
        for name, module in self.modules.items():
            row = self.fixture(name)
            wrong_media = 'TV' if row['media_type'] == 'Movie' else 'Movie'
            wrong_transfer = ('CROSS_DISK_TRANSFER' if row['transfer_type'] == 'SAME_DISK_RENAME'
                              else 'SAME_DISK_RENAME')
            for field, value, message in (
                    ('media_type', wrong_media, 'Only .* is supported'),
                    ('transfer_type', wrong_transfer, 'Only .* is supported')):
                with self.subTest(executor=name, field=field):
                    self.fixture(name)
                    self.row[field] = value
                    self.write_manifest([self.row])
                    self.approval['history'][0]['manifest_sha256'] = self.manifest_hash
                    self.write_approval()
                    with self.assertRaisesRegex(module.Refused, message):
                        module.load_plan(self.base, EXECUTION)


class JournalAndPathSafetyTests(OfflineTest):
    def test_uncertain_live_journals_refuse_even_after_check_only_tail(self):
        blocked = {
            'execute_movie': ('RENAME_INTENT', 'SUCCESS'),
            'execute_movie_nas': ('RENAME_INTENT', 'SUCCESS'),
            'execute_tv_nas': ('RENAME_INTENT', 'RENAMED', 'SONARR_UPDATE_INTENT', 'SUCCESS'),
            'execute_cross_movie': ('COPY_INTENT', 'COPIED', 'RADARR_UPDATE_INTENT',
                                    'DELETE_INTENT', 'SOURCE_REMOVED', 'SUCCESS', 'RENAME_INTENT'),
            'execute_cross_tv': ('COPY_INTENT', 'COPIED', 'SONARR_UPDATE_INTENT',
                                 'DELETE_INTENT', 'SOURCE_REMOVED', 'SUCCESS', 'RENAME_INTENT'),
        }
        journal = self.base / 'journal.jsonl'
        for name, kinds in blocked.items():
            for kind in kinds:
                with self.subTest(executor=name, event=kind):
                    journal.write_text('\n'.join(json.dumps(dict(event=e)) for e in
                                                 ('START', kind, 'STOPPED', 'CHECK_ONLY')))
                    with self.assertRaisesRegex(self.modules[name].Refused, 'Previous live attempt'):
                        self.modules[name].check_journal(journal)

    def test_clean_check_only_journal_allowed_but_truncated_json_refused(self):
        journal = self.base / 'journal.jsonl'
        for module in self.modules.values():
            journal.write_text('{"event":"CHECK_ONLY"}\n')
            module.check_journal(journal)
            journal.write_text('{"event":"CHECK_ONLY"}\n{"event":')
            with self.assertRaises(json.JSONDecodeError):
                module.check_journal(journal)

    def test_path_traversal_disk_mismatch_and_folder_rename_refused(self):
        for name, module in self.modules.items():
            row = self.fixture(name)
            for field, value in (('source_path', row['source_path'] + '/../Other'),
                                 ('source_disk', 'media99'),
                                 ('target_path', row['target_path'] + ' different')):
                with self.subTest(executor=name, field=field):
                    with self.assertRaises(module.Refused):
                        module.paths(dict(row, **{field: value}))

    def test_same_disk_recovery_rejects_unrelated_failure(self):
        module = self.modules['execute_movie_nas']
        journal = self.base / 'journal.jsonl'
        journal.write_text('\n'.join(json.dumps(e) for e in [
            dict(event='PREFLIGHT_OK'), dict(event='RENAME_INTENT'),
            dict(event='STOPPED', error_type='OSError', reason='Permission denied')]))
        with self.assertRaisesRegex(module.Refused, 'diagnosed NFS EINVAL'):
            module.check_journal(journal, resume=True)


class FakeRadarr:
    """Returns detached API records and verifies real temporary-file content."""
    def __init__(self, case):
        self.case = case
        self.record = dict(id=7, path=case.logical_src, rootFolderPath='/media/Movies/Library',
                           hasFile=True, monitored=True, qualityProfileId=1, tags=[],
                           movieFile=dict(id=9, relativePath='movie.mkv', size=len(case.content),
                                          path=case.logical_src + '/movie.mkv'))

    def api(self, endpoint, body=None):
        c = self.case
        if body is not None:
            c.trace.append('arr:update')
            self.record = copy.deepcopy(body)
            self.record['movieFile']['path'] = self.record['path'] + '/movie.mkv'
            c.hook('arr:update')
            return copy.deepcopy(self.record)
        if endpoint == 'system/status':
            return {'version': 'offline-test'}
        if endpoint == 'movie':
            return [copy.deepcopy(self.record)]
        if endpoint == 'movie/7':
            return copy.deepcopy(self.record)
        if endpoint == 'rootfolder':
            return [dict(path='/media/Movies/Common', accessible=True)]
        if endpoint == 'tag':
            return [dict(id=1, label='migratarr-lock'), dict(id=2, label='migratarr-rare')]
        raise AssertionError('Unexpected endpoint: ' + endpoint)

    def visible(self, path, kind='-f'):
        self.case.trace.append('arr:visible')

    def verify_file(self, path, expected_hash):
        c = self.case
        root = c.src if path.startswith(c.logical_src + '/') else c.dst
        c.trace.append('arr:verify:' + ('source' if root == c.src else 'destination'))
        c.m.require(hashlib.sha256((root / 'movie.mkv').read_bytes()).hexdigest() == expected_hash,
                    'Fake Arr-visible content mismatch')


class FakeTransport:
    def __init__(self, case):
        self.case = case

    def call(self, operation, src, dst, before, execution_id, receipt=None):
        c = self.case
        assert src == c.src and dst == c.dst and execution_id == EXECUTION
        c.trace.append('nas:' + operation)
        if operation == 'copy':
            dst.mkdir()
            (dst / 'movie.mkv').write_bytes((src / 'movie.mkv').read_bytes())
        elif operation == 'delete':
            assert receipt == {'result': 'OK', 'operation': 'copy'}
            (src / 'movie.mkv').unlink()
            src.rmdir()
        elif operation != 'check':
            raise AssertionError(operation)
        c.hook('nas:' + operation)
        return dict(result='OK', operation=operation)


class ConfiguredOverrideTagsMixin:
    """Executors honor planner.json override tags, not the migratarr-* literals."""
    def use_tags(self, recommended):
        data = json.loads((ROOT / 'config/planner.example.json').read_text(encoding='utf-8'))
        data['overrides'] = {'lock_tag': 'keep', 'category_tags': {'vault': 'Rare', 'shelf': recommended}}
        self.stack.enter_context(patch.object(self.m, 'OVERRIDES', parse_settings(data).overrides))
        original = self.arr.api
        labels = [dict(id=1, label='keep'), dict(id=2, label='vault'),
                  dict(id=3, label='migratarr-lock'), dict(id=4, label='shelf')]
        self.arr.api = lambda endpoint, body=None: (copy.deepcopy(labels) if endpoint == 'tag'
                                                    else original(endpoint, body))

    def test_configured_lock_and_category_tags_gate_the_move(self):
        self.use_tags(self.RECOMMENDED)
        for tag in (1, 2):
            with self.subTest(tag=tag):
                self.arr.record['tags'] = [tag]
                with self.assertRaises(self.m.Refused):
                    self.run_move()
                self.assertNotIn('nas:copy', self.trace)
                self.assert_retained()
        # The former literal lock tag is an ordinary label once configured away,
        # and a configured tag agreeing with the recommendation is accepted.
        self.arr.record['tags'] = [3, 4]
        self.assertEqual(self.run_move(), 'SUCCESS')


class CrossDiskSequenceTests(ConfiguredOverrideTagsMixin, OfflineTest):
    RECOMMENDED = 'Common'

    def setUp(self):
        super().setUp()
        self.m = self.modules['execute_cross_movie']
        self.fixture('execute_cross_movie')
        self.src = self.base / 'source' / 'Example'
        self.dst = self.base / 'destination' / 'Example'
        self.src.mkdir(parents=True)
        self.dst.parent.mkdir()
        self.content = b'offline disposable test media'
        (self.src / 'movie.mkv').write_bytes(self.content)
        self.logical_src = '/media/Movies/Library/Example'
        self.logical_dst = '/media/Movies/Common/Example'
        self.trace = []
        self.hook = lambda event: None
        self.arr = FakeRadarr(self)
        self.transport = FakeTransport(self)
        # Only translate deployment paths to the sandbox; keep manifest verification,
        # inventory hashing, destination verification and execution logic real.
        self.stack.enter_context(patch.object(self.m, 'paths', return_value=(
            self.src, self.dst, self.logical_src, self.logical_dst)))

    def run_move(self, live=True):
        def log(event, **details):
            self.trace.append('log:' + event)
            self.hook('log:' + event)
        return self.m.execute(self.base, EXECUTION, live, self.arr, log, self.transport)

    def assert_retained(self, copied=False):
        self.assertEqual((self.src / 'movie.mkv').read_bytes(), self.content)
        self.assertNotIn('nas:delete', self.trace)
        self.assertNotIn('log:SUCCESS', self.trace)
        if copied:
            self.assertTrue(self.dst.is_dir())

    def test_check_only_never_copies_updates_or_deletes(self):
        self.assertEqual(self.run_move(live=False), 'CHECK_ONLY')
        self.assertEqual([e for e in self.trace if e.startswith('nas:')], ['nas:check'])
        self.assertNotIn('arr:update', self.trace)
        self.assertFalse(self.dst.exists())
        self.assert_retained()

    def test_success_journals_copy_verify_update_then_delete(self):
        self.assertEqual(self.run_move(), 'SUCCESS')
        ordered = ['log:COPY_INTENT', 'nas:copy', 'log:COPIED', 'arr:verify:destination',
                   'log:RADARR_UPDATE_INTENT', 'arr:update', 'log:DELETE_INTENT',
                   'nas:delete', 'log:SOURCE_REMOVED', 'log:SUCCESS']
        positions = [self.trace.index(event) for event in ordered]
        self.assertEqual(positions, sorted(positions))
        self.assertFalse(self.src.exists())
        self.assertEqual((self.dst / 'movie.mkv').read_bytes(), self.content)

    def test_existing_destination_blocks_before_transport(self):
        self.dst.mkdir()
        with self.assertRaisesRegex(self.m.Refused, 'Source/destination not ready'):
            self.run_move()
        self.assertNotIn('nas:check', self.trace)
        self.assert_retained()

    def test_lock_and_conflicting_override_block_copy(self):
        for tag in (1, 2):
            with self.subTest(tag=tag):
                self.arr.record['tags'] = [tag]
                with self.assertRaises(self.m.Refused):
                    self.run_move()
                self.assertNotIn('nas:copy', self.trace)
                self.assert_retained()

    def test_copy_timeout_keeps_source_and_never_updates_arr(self):
        def fail(event):
            if event == 'nas:copy':
                raise TimeoutError('simulated uncertain copy response')
        self.hook = fail
        with self.assertRaises(TimeoutError):
            self.run_move()
        self.assertNotIn('arr:update', self.trace)
        self.assert_retained(copied=True)

    def test_source_change_after_copy_blocks_update_and_deletion(self):
        changed = self.content + b' changed during copy'
        def change(event):
            if event == 'nas:copy':
                (self.src / 'movie.mkv').write_bytes(changed)
        self.hook = change
        with self.assertRaisesRegex(self.m.Refused, 'Source changed while copying'):
            self.run_move()
        self.assertEqual((self.src / 'movie.mkv').read_bytes(), changed)
        self.assertEqual((self.dst / 'movie.mkv').read_bytes(), self.content)
        self.assertNotIn('arr:update', self.trace)
        self.assertNotIn('nas:delete', self.trace)
        self.assertNotIn('log:SUCCESS', self.trace)

    def test_source_change_after_preflight_blocks_copy(self):
        changed = self.content + b' changed after preflight'
        def change(event):
            if event == 'log:PREFLIGHT_OK':
                (self.src / 'movie.mkv').write_bytes(changed)
        self.hook = change
        with self.assertRaisesRegex(self.m.Refused, 'Source changed during preflight'):
            self.run_move()
        self.assertEqual((self.src / 'movie.mkv').read_bytes(), changed)
        self.assertNotIn('nas:copy', self.trace)
        self.assertNotIn('arr:update', self.trace)
        self.assertFalse(self.dst.exists())

    def test_corrupt_destination_keeps_source(self):
        def corrupt(event):
            if event == 'nas:copy':
                (self.dst / 'movie.mkv').write_bytes(b'corrupt')
        self.hook = corrupt
        with self.assertRaisesRegex(self.m.Refused, 'Destination content mismatch'):
            self.run_move()
        self.assertNotIn('arr:update', self.trace)
        self.assert_retained(copied=True)

    def test_approval_revoked_after_copy_blocks_arr_update(self):
        def revoke(event):
            if event == 'nas:copy':
                self.approval['history'].append(dict(execution_id=EXECUTION, action='REVOKE'))
                self.write_approval()
        self.hook = revoke
        with self.assertRaisesRegex(self.m.Refused, 'exact manifest hash'):
            self.run_move()
        self.assertNotIn('arr:update', self.trace)
        self.assert_retained(copied=True)

    def test_approval_revoked_after_arr_update_blocks_deletion(self):
        def revoke(event):
            if event == 'arr:update':
                self.approval['approved_execution_ids'] = []
                self.write_approval()
        self.hook = revoke
        with self.assertRaisesRegex(self.m.Refused, 'unapproved'):
            self.run_move()
        self.assertIn('arr:update', self.trace)
        self.assert_retained(copied=True)

    def test_wrong_arr_path_after_update_keeps_both_copies(self):
        def change(event):
            if event == 'arr:update':
                self.arr.record['path'] = '/unexpected'
        self.hook = change
        with self.assertRaisesRegex(self.m.Refused, 'Radarr path verification failed'):
            self.run_move()
        self.assert_retained(copied=True)


class FakeSonarr:
    """Returns detached API records and verifies real temporary-file content."""
    def __init__(self, case):
        self.case = case
        self.record = dict(id=11, path=case.logical_src, rootFolderPath='/media/TV/Library',
                           seriesType='standard', seasonFolder=True, monitored=True,
                           qualityProfileId=2, tags=[])

    def api(self, endpoint, body=None):
        c = self.case
        if body is not None:
            c.trace.append('arr:update')
            self.record = copy.deepcopy(body)
            c.hook('arr:update')
            return copy.deepcopy(self.record)
        if endpoint == 'system/status':
            return {'version': '4.0.0'}
        if endpoint == 'series':
            return [copy.deepcopy(self.record)]
        if endpoint == 'series/11':
            return copy.deepcopy(self.record)
        if endpoint == 'rootfolder':
            return [dict(path='/media/TV/Current', accessible=True)]
        if endpoint == 'tag':
            return [dict(id=1, label='migratarr-lock'), dict(id=2, label='migratarr-rare')]
        if endpoint == 'episodefile?seriesId=11':
            return [dict(id=21, seriesId=11, relativePath='episode.mkv',
                         path=self.record['path'] + '/episode.mkv', size=len(c.content))]
        if endpoint == 'episode?seriesId=11':
            return [dict(id=31, seriesId=11, episodeFileId=21, hasFile=True,
                         seasonNumber=1, episodeNumber=1, monitored=True)]
        raise AssertionError('Unexpected endpoint: ' + endpoint)

    def visible(self, path, kind='-f'):
        self.case.trace.append('arr:visible')

    def verify_file(self, path, expected_hash):
        c = self.case
        root = c.src if path.startswith(c.logical_src + '/') else c.dst
        c.trace.append('arr:verify:' + ('source' if root == c.src else 'destination'))
        c.m.require(hashlib.sha256((root / 'episode.mkv').read_bytes()).hexdigest() == expected_hash,
                    'Fake Sonarr-visible content mismatch')


class TvFakeTransport:
    def __init__(self, case):
        self.case = case

    def call(self, operation, src, dst, before, execution_id, receipt=None):
        c = self.case
        assert src == c.src and dst == c.dst and execution_id == EXECUTION
        c.trace.append('nas:' + operation)
        if operation == 'copy':
            dst.mkdir()
            (dst / 'episode.mkv').write_bytes((src / 'episode.mkv').read_bytes())
        elif operation == 'delete':
            assert receipt == {'result': 'OK', 'operation': 'copy'}
            (src / 'episode.mkv').unlink()
            src.rmdir()
        elif operation != 'check':
            raise AssertionError(operation)
        c.hook('nas:' + operation)
        return dict(result='OK', operation=operation)


class TvCrossDiskSequenceTests(ConfiguredOverrideTagsMixin, OfflineTest):
    RECOMMENDED = 'Current'

    def setUp(self):
        super().setUp()
        self.m = self.modules['execute_cross_tv']
        self.fixture('execute_cross_tv')
        self.src = self.base / 'source' / 'Example'
        self.dst = self.base / 'destination' / 'Example'
        self.src.mkdir(parents=True)
        self.dst.parent.mkdir()
        self.content = b'offline disposable test episode'
        (self.src / 'episode.mkv').write_bytes(self.content)
        self.logical_src = '/media/TV/Library/Example'
        self.logical_dst = '/media/TV/Current/Example'
        self.trace = []
        self.hook = lambda event: None
        self.arr = FakeSonarr(self)
        self.transport = TvFakeTransport(self)
        # Only translate deployment paths to the sandbox; keep manifest verification,
        # inventory hashing, destination verification and execution logic real.
        self.stack.enter_context(patch.object(self.m, 'paths', return_value=(
            self.src, self.dst, self.logical_src, self.logical_dst)))

    def run_move(self, live=True):
        def log(event, **details):
            self.trace.append('log:' + event)
            self.hook('log:' + event)
        return self.m.execute(self.base, EXECUTION, live, self.arr, log, self.transport)

    def assert_retained(self, copied=False):
        self.assertEqual((self.src / 'episode.mkv').read_bytes(), self.content)
        self.assertNotIn('nas:delete', self.trace)
        self.assertNotIn('log:SUCCESS', self.trace)
        if copied:
            self.assertTrue(self.dst.is_dir())

    def test_check_only_never_copies_updates_or_deletes(self):
        self.assertEqual(self.run_move(live=False), 'CHECK_ONLY')
        self.assertEqual([e for e in self.trace if e.startswith('nas:')], ['nas:check'])
        self.assertNotIn('arr:update', self.trace)
        self.assertFalse(self.dst.exists())
        self.assert_retained()

    def test_success_journals_copy_verify_update_then_delete(self):
        self.assertEqual(self.run_move(), 'SUCCESS')
        ordered = ['log:COPY_INTENT', 'nas:copy', 'log:COPIED', 'arr:verify:destination',
                   'log:SONARR_UPDATE_INTENT', 'arr:update', 'log:DELETE_INTENT',
                   'nas:delete', 'log:SOURCE_REMOVED', 'log:SUCCESS']
        positions = [self.trace.index(event) for event in ordered]
        self.assertEqual(positions, sorted(positions))
        self.assertFalse(self.src.exists())
        self.assertEqual((self.dst / 'episode.mkv').read_bytes(), self.content)

    def test_existing_destination_blocks_before_transport(self):
        self.dst.mkdir()
        with self.assertRaisesRegex(self.m.Refused, 'Source/destination not ready'):
            self.run_move()
        self.assertNotIn('nas:check', self.trace)
        self.assert_retained()

    def test_lock_and_conflicting_override_block_copy(self):
        for tag in (1, 2):
            with self.subTest(tag=tag):
                self.arr.record['tags'] = [tag]
                with self.assertRaises(self.m.Refused):
                    self.run_move()
                self.assertNotIn('nas:copy', self.trace)
                self.assert_retained()

    def test_copy_timeout_keeps_source_and_never_updates_arr(self):
        def fail(event):
            if event == 'nas:copy':
                raise TimeoutError('simulated uncertain copy response')
        self.hook = fail
        with self.assertRaises(TimeoutError):
            self.run_move()
        self.assertNotIn('arr:update', self.trace)
        self.assert_retained(copied=True)

    def test_source_change_after_copy_blocks_update_and_deletion(self):
        changed = self.content + b' changed during copy'
        def change(event):
            if event == 'nas:copy':
                (self.src / 'episode.mkv').write_bytes(changed)
        self.hook = change
        with self.assertRaisesRegex(self.m.Refused, 'Source changed while copying'):
            self.run_move()
        self.assertEqual((self.src / 'episode.mkv').read_bytes(), changed)
        self.assertEqual((self.dst / 'episode.mkv').read_bytes(), self.content)
        self.assertNotIn('arr:update', self.trace)
        self.assertNotIn('nas:delete', self.trace)
        self.assertNotIn('log:SUCCESS', self.trace)

    def test_source_change_after_preflight_blocks_copy(self):
        changed = self.content + b' changed after preflight'
        def change(event):
            if event == 'log:PREFLIGHT_OK':
                (self.src / 'episode.mkv').write_bytes(changed)
        self.hook = change
        with self.assertRaisesRegex(self.m.Refused, 'Source changed during preflight'):
            self.run_move()
        self.assertEqual((self.src / 'episode.mkv').read_bytes(), changed)
        self.assertNotIn('nas:copy', self.trace)
        self.assertNotIn('arr:update', self.trace)
        self.assertFalse(self.dst.exists())

    def test_corrupt_destination_keeps_source(self):
        def corrupt(event):
            if event == 'nas:copy':
                (self.dst / 'episode.mkv').write_bytes(b'corrupt')
        self.hook = corrupt
        with self.assertRaisesRegex(self.m.Refused, 'Destination content mismatch'):
            self.run_move()
        self.assertNotIn('arr:update', self.trace)
        self.assert_retained(copied=True)

    def test_approval_revoked_after_copy_blocks_arr_update(self):
        def revoke(event):
            if event == 'nas:copy':
                self.approval['history'].append(dict(execution_id=EXECUTION, action='REVOKE'))
                self.write_approval()
        self.hook = revoke
        with self.assertRaisesRegex(self.m.Refused, 'exact manifest hash'):
            self.run_move()
        self.assertNotIn('arr:update', self.trace)
        self.assert_retained(copied=True)

    def test_approval_revoked_after_arr_update_blocks_deletion(self):
        def revoke(event):
            if event == 'arr:update':
                self.approval['approved_execution_ids'] = []
                self.write_approval()
        self.hook = revoke
        with self.assertRaisesRegex(self.m.Refused, 'unapproved'):
            self.run_move()
        self.assertIn('arr:update', self.trace)
        self.assert_retained(copied=True)

    def test_wrong_arr_path_after_update_keeps_both_copies(self):
        def change(event):
            if event == 'arr:update':
                self.arr.record['path'] = '/unexpected'
        self.hook = change
        with self.assertRaisesRegex(self.m.Refused, 'Sonarr path verification failed'):
            self.run_move()
        self.assert_retained(copied=True)



if __name__ == '__main__':
    unittest.main()
