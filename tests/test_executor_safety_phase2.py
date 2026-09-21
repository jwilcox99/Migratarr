"""Second offline checkpoint: same-disk sequencing, NAS receipts, recoveries."""
import copy
import hashlib
import importlib
import json
import os
from pathlib import Path, PurePosixPath
import unittest
from unittest.mock import patch

from test_executor_safety import EXECUTION, RUN, OfflineTest


class SameDiskTransport:
    def __init__(self, case):
        self.case = case

    def call(self, operation, src, dst, before):
        case = self.case
        case.trace.append('nas:' + operation)
        case.hook('nas:' + operation)
        if operation == 'rename':
            os.rename(src, dst)
        elif operation != 'check':
            raise AssertionError(operation)
        return dict(result='OK', operation=operation)


class MovieArr:
    def __init__(self, case):
        self.case = case
        self.record = dict(
            id=7, path=case.logical_src, rootFolderPath='/media/Movies/Library',
            hasFile=True, monitored=True, qualityProfileId=1, tags=[],
            movieFile=dict(id=9, relativePath='movie.mkv', size=len(case.content),
                           path=case.logical_src + '/movie.mkv'))

    def api(self, endpoint, body=None):
        case = self.case
        if body is not None:
            case.trace.append('arr:update')
            self.record = copy.deepcopy(body)
            self.record['movieFile']['path'] = self.record['path'] + '/movie.mkv'
            case.hook('arr:update')
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
            return [dict(id=1, label='migratarr-lock')]
        raise AssertionError(endpoint)

    def visible(self, path, kind='-f'):
        self.case.trace.append('arr:visible')

    def verify_file(self, path, expected_hash):
        case = self.case
        root = case.src if path.startswith(case.logical_src + '/') else case.dst
        case.trace.append('arr:verify:' + ('source' if root == case.src else 'destination'))
        case.m.require(hashlib.sha256((root / 'movie.mkv').read_bytes()).hexdigest() == expected_hash,
                       'Fake Arr content mismatch')


class Sonarr:
    def __init__(self, case):
        self.case = case
        self.record = dict(
            id=11, path=case.logical_src, rootFolderPath='/media/TV/Library', tags=[],
            seriesType='standard', seasonFolder=True, monitored=True, qualityProfileId=2)

    def api(self, endpoint, body=None):
        case = self.case
        if body is not None:
            case.trace.append('arr:update')
            self.record = copy.deepcopy(body)
            case.hook('arr:update')
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
            return [dict(id=1, label='migratarr-lock')]
        if endpoint == 'episodefile?seriesId=11':
            return [dict(id=21, seriesId=11, relativePath='episode.mkv',
                         path=self.record['path'] + '/episode.mkv', size=len(case.content))]
        if endpoint == 'episode?seriesId=11':
            return [dict(id=31, seriesId=11, episodeFileId=21, hasFile=True,
                         seasonNumber=1, episodeNumber=1, monitored=True)]
        raise AssertionError(endpoint)

    def visible(self, path, kind='-f'):
        self.case.trace.append('arr:visible')

    def verify_file(self, path, expected_hash):
        case = self.case
        root = case.src if path.startswith(case.logical_src + '/') else case.dst
        case.trace.append('arr:verify:' + ('source' if root == case.src else 'destination'))
        case.m.require(hashlib.sha256((root / 'episode.mkv').read_bytes()).hexdigest() == expected_hash,
                       'Fake Sonarr content mismatch')


class SameDiskSequenceMixin:
    module_name = None

    def setUp(self):
        super().setUp()
        self.m = self.modules[self.module_name]
        self.fixture(self.module_name)
        self.src = self.base / 'disk' / 'Library' / 'Example'
        self.dst = self.base / 'disk' / self.row['recommended'] / 'Example'
        self.src.mkdir(parents=True)
        self.dst.parent.mkdir(parents=True, exist_ok=True)
        self.content = b'disposable same-disk media'
        self.filename = 'episode.mkv' if self.module_name == 'execute_tv_nas' else 'movie.mkv'
        (self.src / self.filename).write_bytes(self.content)
        media = 'TV' if self.module_name == 'execute_tv_nas' else 'Movies'
        self.logical_src = f'/media/{media}/Library/Example'
        self.logical_dst = f'/media/{media}/{self.row["recommended"]}/Example'
        self.trace = []
        self.hook = lambda event: None
        self.transport = SameDiskTransport(self)
        self.arr = Sonarr(self) if self.module_name == 'execute_tv_nas' else MovieArr(self)
        self.stack.enter_context(patch.object(self.m, 'paths', return_value=(
            self.src, self.dst, self.logical_src, self.logical_dst)))
        if self.module_name == 'execute_tv_nas':
            self.stack.enter_context(patch.object(self.m, 'remote_path', return_value='/remote'))

        def verify_rename(src, dst, expected, log, **kwargs):
            self.trace.append('nfs:verify')
            self.m.require(not src.exists() and dst.is_dir(), 'Fake rename visibility mismatch')
            actual = self.m.file_metadata(dst) if kwargs.get('metadata_only') else self.m.inventory(dst)
            wanted = self.m.inventory_metadata(expected) if kwargs.get('metadata_only') else expected
            self.m.require(actual == wanted, 'Fake destination inventory mismatch')

        self.stack.enter_context(patch.object(self.m, 'verify_nfs_after_rename', side_effect=verify_rename))

    def run_move(self, live=True):
        def log(event, **details):
            self.trace.append('log:' + event)
            self.hook('log:' + event)
        return self.m.execute(self.base, EXECUTION, live, self.arr, log, self.transport)

    def test_check_only_has_no_rename_or_arr_update(self):
        self.assertEqual(self.run_move(False), 'CHECK_ONLY')
        self.assertTrue(self.src.exists())
        self.assertFalse(self.dst.exists())
        self.assertNotIn('nas:rename', self.trace)
        self.assertNotIn('arr:update', self.trace)

    def test_success_orders_intent_rename_verify_update_success(self):
        self.assertEqual(self.run_move(), 'SUCCESS')
        ordered = ['log:RENAME_INTENT', 'nas:rename', 'log:RENAMED', 'nfs:verify',
                   self.update_intent, 'arr:update', 'log:SUCCESS']
        positions = [self.trace.index(event) for event in ordered]
        self.assertEqual(positions, sorted(positions))
        self.assertFalse(self.src.exists())
        self.assertEqual((self.dst / self.filename).read_bytes(), self.content)

    def test_source_change_after_preflight_blocks_rename(self):
        changed = self.content + b' changed'
        def mutate(event):
            if event == 'log:PREFLIGHT_OK':
                (self.src / self.filename).write_bytes(changed)
        self.hook = mutate
        with self.assertRaisesRegex(self.m.Refused, 'Source changed during preflight'):
            self.run_move()
        self.assertNotIn('nas:rename', self.trace)
        self.assertNotIn('arr:update', self.trace)
        self.assertEqual((self.src / self.filename).read_bytes(), changed)

    def test_rename_failure_never_updates_arr(self):
        def fail(event):
            if event == 'nas:rename':
                raise OSError(22, 'simulated rename refusal')
        self.hook = fail
        with self.assertRaises(OSError):
            self.run_move()
        self.assertTrue(self.src.exists())
        self.assertFalse(self.dst.exists())
        self.assertNotIn('arr:update', self.trace)


class MovieSameDiskTests(SameDiskSequenceMixin, OfflineTest):
    module_name = 'execute_movie_nas'
    update_intent = 'log:RADARR_UPDATE_INTENT'

    def test_arr_update_failure_leaves_renamed_media_for_reconciliation(self):
        def fail(event):
            if event == 'arr:update':
                raise RuntimeError('simulated Arr failure')
        self.hook = fail
        with self.assertRaises(RuntimeError):
            self.run_move()
        self.assertFalse(self.src.exists())
        self.assertTrue(self.dst.exists())
        self.assertNotIn('log:SUCCESS', self.trace)


class TVSameDiskTests(SameDiskSequenceMixin, OfflineTest):
    module_name = 'execute_tv_nas'
    update_intent = 'log:SONARR_UPDATE_INTENT'

    def test_episode_association_change_after_rename_blocks_arr_update(self):
        original = self.arr.api
        changed = False
        def api(endpoint, body=None):
            nonlocal changed
            result = original(endpoint, body)
            if endpoint == 'episode?seriesId=11' and not self.src.exists() and body is None:
                changed = True
                result[0]['monitored'] = False
            return result
        self.arr.api = api
        with self.assertRaisesRegex(self.m.Refused, 'Episode associations changed before path update'):
            self.run_move()
        self.assertTrue(changed)
        self.assertNotIn('arr:update', self.trace)
        self.assertTrue(self.dst.exists())


class FakeRemotePath:
    devices = {'/volume4/media01': 1, '/volume1/media02': 2,
               '/volume2/media03': 3, '/volume3/media04': 4}

    def __init__(self, *parts):
        self.p = PurePosixPath(*[str(x) for x in parts])

    @property
    def parts(self): return self.p.parts

    @property
    def name(self): return self.p.name

    @property
    def parent(self): return type(self)(self.p.parent)

    def __truediv__(self, other): return type(self)(self.p / other)

    def __str__(self): return str(self.p)

    def __fspath__(self): return str(self.p)

    def is_dir(self): return True

    def stat(self):
        root = '/' + '/'.join(self.parts[1:3])
        return type('Stat', (), {'st_dev': self.devices[root], 'st_ino': 100 + self.devices[root]})()


class NasReceiptTests(OfflineTest):
    def setUp(self):
        super().setUp()
        self.m = self.modules['execute_cross_movie']
        self.source = {'movie.mkv': [4, 'hash', 123, 456]}
        self.data = dict(operation='delete', execution_id=EXECUTION,
                         source='/volume3/media04/Movies/Library/Example',
                         destination='/volume4/media01/Movies/Common/Example',
                         inventory=copy.deepcopy(self.source),
                         receipt=dict(result='OK', operation='copy', source_id=[4, 104],
                                      source_inventory=copy.deepcopy(self.source)))
        self.stack.enter_context(patch.object(self.m, 'Path', FakeRemotePath))
        self.stack.enter_context(patch.object(self.m, 'canonical_existing'))
        self.stack.enter_context(patch.object(self.m, 'inventory', return_value=copy.deepcopy(self.source)))
        self.stack.enter_context(patch.object(self.m.os.path, 'lexists', return_value=False))

    def test_delete_refuses_receipt_identity_operation_and_inventory_mismatch(self):
        mutations = (
            lambda r: r.update(result='ERROR'),
            lambda r: r.update(operation='check'),
            lambda r: r.update(source_id=[4, 999]),
            lambda r: r.update(source_inventory={'movie.mkv': [4, 'other', 123, 456]}),
        )
        for mutate in mutations:
            data = copy.deepcopy(self.data)
            mutate(data['receipt'])
            with self.subTest(receipt=data['receipt']), self.assertRaisesRegex(
                    self.m.Refused, 'Source identity differs from copied source'):
                self.m.nas_operation(data)

    def test_check_refuses_existing_destination_and_low_space(self):
        data = copy.deepcopy(self.data)
        data['operation'] = 'check'
        data.pop('receipt')
        destination = data['destination']
        with patch.object(self.m.os.path, 'lexists', side_effect=lambda p: str(p) == destination):
            with self.assertRaisesRegex(self.m.Refused, 'Destination exists'):
                self.m.nas_operation(data)
        with patch.object(self.m.shutil, 'disk_usage', return_value=type('Usage', (), {'free': 1})()):
            with self.assertRaisesRegex(self.m.Refused, 'Insufficient destination space'):
                self.m.nas_operation(data)

    def test_invalid_operation_path_and_execution_id_refuse_before_mutation(self):
        changes = (
            ('operation', 'remove'),
            ('execution_id', '../bad'),
            ('source', '/volume3/media04/TV/Library/Example'),
            ('destination', '/volume3/media04/Movies/Common/Other'),
        )
        for field, value in changes:
            data = copy.deepcopy(self.data)
            data[field] = value
            with self.subTest(field=field), self.assertRaises(self.m.Refused):
                self.m.nas_operation(data)


class IncidentRecoveryTests(OfflineTest):
    def recovery_fixture(self, name):
        with patch('runtime_config.get_config', return_value=self.config):
            module = importlib.import_module(name)
        execution_id = module.EXECUTION_ID
        row = dict(recommended='Common')
        manifest_hash = 'manifest-hash'
        src = Path('/fake/source')
        dst = Path('/fake/destination')
        logical_src = '/media/Movies/Library/Example'
        logical_dst = '/media/Movies/Common/Example'
        plan = dict(event='PREFLIGHT_OK', execution_id=execution_id,
                    manifest_sha256=manifest_hash, source=str(src), destination=str(dst),
                    logical_source=logical_src, logical_destination=logical_dst,
                    radarr_id=7, movie_file_id=9)
        inventory = {'movie.mkv': [4, 'hash', 123, 456]}
        events = [plan, dict(event='COPY_INTENT', execution_id=execution_id,
                            inventory=copy.deepcopy(inventory))]
        if name == 'recover_cross_0102':
            events.append(dict(event='STOPPED', execution_id=execution_id,
                               error_type='TimeoutExpired', reason='command timed out after 1800 seconds'))
        else:
            receipt = dict(result='OK', operation='copy', source_id=[1, 2],
                           source_inventory=copy.deepcopy(inventory))
            events.extend([dict(event='COPIED', execution_id=execution_id, receipt=receipt),
                           dict(event='STOPPED', execution_id=execution_id,
                                reason='Radarr changed before update')])
        logs = self.base / 'execution_logs'
        logs.mkdir(exist_ok=True)
        journal = logs / (execution_id + '.jsonl')
        journal.write_text('\n'.join(json.dumps(e) for e in events) + '\n')
        patches = [patch.object(module.m, 'load_plan', return_value=(row, manifest_hash)),
                   patch.object(module.m, 'paths', return_value=(
                       src, dst, logical_src, logical_dst))]
        return module, events, journal, patches

    def test_exact_incident_journals_are_accepted(self):
        for name in ('recover_cross_0102', 'recover_cross_0116'):
            module, events, journal, patches = self.recovery_fixture(name)
            with self.subTest(recovery=name), patches[0], patches[1]:
                result = module.load_recovery(self.base)
                self.assertEqual(result[1], 'manifest-hash')
                self.assertEqual(result[5], Path('/fake/source'))

    def test_wrong_execution_id_final_failure_or_later_mutation_refused(self):
        for name in ('recover_cross_0102', 'recover_cross_0116'):
            for mutation in ('id', 'failure', 'later'):
                module, events, journal, patches = self.recovery_fixture(name)
                if mutation == 'id':
                    events[0]['execution_id'] = 'other'
                elif mutation == 'failure':
                    events[-1]['reason'] = 'different failure'
                else:
                    events.insert(-1, dict(event='RECOVERY_STARTED', execution_id=module.EXECUTION_ID))
                journal.write_text('\n'.join(json.dumps(e) for e in events) + '\n')
                with self.subTest(recovery=name, mutation=mutation), patches[0], patches[1]:
                    with self.assertRaises(module.m.Refused):
                        module.load_recovery(self.base)

    def test_0116_receipt_and_both_recovery_paths_remain_bound(self):
        module, events, journal, patches = self.recovery_fixture('recover_cross_0116')
        events[-2]['receipt']['source_id'] = []
        journal.write_text('\n'.join(json.dumps(e) for e in events) + '\n')
        with patches[0], patches[1], self.assertRaisesRegex(module.m.Refused, 'Invalid original copy receipt'):
            module.load_recovery(self.base)

        for name in ('recover_cross_0102', 'recover_cross_0116'):
            module, events, journal, patches = self.recovery_fixture(name)
            patches[1] = patch.object(module.m, 'paths', return_value=(
                Path('/other/source'), Path('/fake/destination'),
                '/media/Movies/Library/Other', '/media/Movies/Common/Example'))
            with self.subTest(recovery=name), patches[0], patches[1]:
                with self.assertRaisesRegex(module.m.Refused, 'paths disagree'):
                    module.load_recovery(self.base)


if __name__ == '__main__':
    unittest.main()
