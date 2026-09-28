"""recover_cross_published.py: finish a cross-disk move whose published copy's NAS call timed out.

Builds the real diagnosed state with the real executors and the offline fakes from
test_executor_safety*.py: the copy is published, then the NAS response times out.
"""
import copy
import importlib
import inspect
import json
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

import test_executor_safety as safety
import test_executor_safety_phase2 as phase2
from planner_settings import load_settings
from runtime_config import load_config
from storage_targets import load_targets

EXECUTION = safety.EXECUTION
ROOT = safety.ROOT


def skip_inherited(cls):
    for name in dir(cls):
        if name.startswith('test_') and not name.startswith('test_recovery'):
            setattr(cls, name, None)
    return cls


class RecoveryMixin:
    FILENAME = None
    UPDATE_EVENT = None

    def setUp(self):
        super().setUp()
        with patch('runtime_config.get_config', return_value=self.config), \
                patch('planner_settings.get_settings', return_value=self.settings), \
                patch('media_layout.get_targets', return_value=self.targets):
            self.rp = importlib.import_module('recover_cross_published')
        self.journal = self.base / 'execution_logs' / (EXECUTION + '.jsonl')
        self.journal.parent.mkdir(exist_ok=True)
        # Reproduce the incident: the NAS publishes the copy, then its response times out.
        copy_transport = self.transport.call

        def timed_out_copy(operation, *args, **kwargs):
            result = copy_transport(operation, *args, **kwargs)
            if operation == 'copy':
                raise subprocess.TimeoutExpired('NAS copy', 1800)
            return result
        self.transport.call = timed_out_copy
        events = [dict(event='START', live=True)]

        def log(event, **details):
            events.append(dict(event=event, **details))
        with self.assertRaises(subprocess.TimeoutExpired) as caught:
            self.m.execute(self.base, EXECUTION, True, self.arr, log, self.transport)
        events.append(dict(event='STOPPED', error_type='TimeoutExpired', reason=str(caught.exception)))
        self.journal.write_text(''.join(json.dumps(dict(utc='2026-09-26T00:00:00+00:00', execution_id=EXECUTION,
                                                        **e)) + '\n' for e in events))
        self.assertTrue(self.src.is_dir() and self.dst.is_dir())
        self.trace.clear()
        self.recovery_transport = RecoveryTransport(self)

    def recover(self, live=True):
        def log(event, **details):
            with self.journal.open('a') as stream:
                stream.write(json.dumps(dict(utc='t', execution_id=EXECUTION, event=event, **details)) + '\n')
            self.trace.append('log:' + event)
            self.hook('log:' + event)
        return self.rp.recover(self.m, self.base, EXECUTION, live, self.arr, self.recovery_transport, log)

    def journal_events(self):
        return [json.loads(line)['event'] for line in self.journal.read_text().splitlines()]

    def assert_untouched(self, journal_before):
        self.assertEqual(self.journal.read_text(), journal_before)
        self.assertTrue(self.src.is_dir() and self.dst.is_dir())
        self.assertNotIn('arr:update', self.trace)
        self.assertNotIn('nas:delete', self.trace)

    def refuses(self, pattern):
        before = self.journal.read_text()
        with self.assertRaisesRegex(self.m.Refused, pattern):
            self.recover()
        self.assert_untouched(before)

    def status(self):
        import read_api
        manifest_hash = self.m.load_plan(self.base, EXECUTION)[1]
        return read_api.execution_state(self.base, EXECUTION, manifest_hash)

    def test_recovery_check_only_writes_nothing(self):
        before = self.journal.read_text()
        self.assertEqual(self.recover(live=False), 'CHECK_ONLY')
        self.assert_untouched(before)
        self.assertEqual(self.status()['state'], 'NEEDS_RECONCILIATION')
        self.assertIn('nas:receipt', self.trace)

    def test_recovery_success_finishes_the_move(self):
        self.assertEqual(self.recover(), 'SUCCESS')
        self.assertEqual(self.journal_events()[-5:], ['RECOVERY_STARTED', self.UPDATE_EVENT, 'DELETE_INTENT',
                                                      'SOURCE_REMOVED', 'SUCCESS'])
        order = ['log:RECOVERY_STARTED', 'arr:update', 'log:DELETE_INTENT', 'nas:delete', 'log:SUCCESS']
        self.assertEqual([self.trace.index(e) for e in order], sorted(self.trace.index(e) for e in order))
        self.assertFalse(self.src.exists())
        self.assertEqual((self.dst / self.FILENAME).read_bytes(), self.content)
        self.assertEqual(self.arr.record['path'], self.logical_dst)
        state = self.status()
        self.assertEqual((state['state'], state['recovered']), ('SUCCEEDED', True))
        with self.assertRaisesRegex(self.m.Refused, 'Previous live attempt'):
            self.m.check_journal(self.journal)

    def test_recovery_refuses_missing_destination(self):
        (self.dst / self.FILENAME).unlink()
        self.dst.rmdir()
        before = self.journal.read_text()
        with self.assertRaisesRegex(self.m.Refused, 'Expected both source and published destination directories'):
            self.recover()
        self.assertEqual(self.journal.read_text(), before)
        self.assertTrue(self.src.is_dir())
        self.assertNotIn('arr:update', self.trace)

    def test_recovery_refuses_corrupt_destination(self):
        (self.dst / self.FILENAME).write_bytes(b'corrupt')
        self.refuses('Destination content mismatch')

    def test_recovery_refuses_changed_source(self):
        (self.src / self.FILENAME).write_bytes(self.content + b' changed')
        self.refuses('Source changed since the copy started')

    def test_recovery_refuses_leftover_work_directory(self):
        (self.dst.parent / ('.migratarr-stage-' + EXECUTION)).mkdir()
        self.refuses('Unexpected work directory')

    def test_recovery_refuses_other_stop_reasons_and_later_events(self):
        lines = self.journal.read_text().splitlines()
        last = json.loads(lines[-1])
        for change in ('refused', 'later_event', 'recovery_started'):
            with self.subTest(change=change):
                if change == 'refused':
                    edited = lines[:-1] + [json.dumps(dict(last, error_type='Refused'))]
                elif change == 'later_event':
                    edited = lines + [json.dumps(dict(last, event='START', live=False)),
                                      json.dumps(dict(last, event='CHECK_ONLY'))]
                else:
                    edited = lines + [json.dumps(dict(last, event='RECOVERY_STARTED'))]
                self.journal.write_text('\n'.join(edited) + '\n')
                self.refuses('did not stop on a NAS timeout|does not end with|later mutation or recovery')
        self.journal.write_text('\n'.join(lines) + '\n')

    def test_recovery_refuses_revoked_approval(self):
        self.approval['history'].append(dict(execution_id=EXECUTION, action='REVOKE'))
        self.write_approval()
        self.refuses('exact manifest hash')

    def test_recovery_refuses_when_arr_no_longer_points_at_source(self):
        self.arr.record['path'] = '/media/elsewhere/Example'
        self.refuses('no longer points at the source|Radarr path verification failed')

    def test_recovery_refuses_lock_tag(self):
        self.arr.record['tags'] = [1]
        self.refuses('is now locked')

    def test_recovery_approval_revoked_after_arr_update_keeps_source(self):
        def revoke(event):
            if event == 'arr:update':
                self.approval['approved_execution_ids'] = []
                self.write_approval()
        self.hook = revoke
        with self.assertRaisesRegex(self.m.Refused, 'unapproved'):
            self.recover()
        self.assertTrue(self.src.is_dir())
        self.assertNotIn('nas:delete', self.trace)
        self.assertEqual(self.journal_events()[-2:], ['RECOVERY_STARTED', self.UPDATE_EVENT])


class RecoveryTransport:
    """NAS stand-in: rebuilds the receipt from observed state and performs the verified delete."""

    def __init__(self, case):
        self.case = case

    def call(self, operation, src, dst, before, execution_id, receipt=None):
        c = self.case
        assert src == c.src and dst == c.dst and execution_id == EXECUTION
        c.trace.append('nas:' + operation)
        if operation == 'receipt':
            return dict(result='OK', operation='copy', reconstructed=True, source_id=[1, 2],
                        source_inventory=c.m.inventory(src))
        assert operation == 'delete' and receipt and receipt['reconstructed'] is True
        for p in sorted(src.rglob('*'), reverse=True):
            p.unlink() if p.is_file() else p.rmdir()
        src.rmdir()
        c.hook('nas:delete')
        return dict(result='OK', operation='delete')


@skip_inherited
class MovieRecoveryTests(RecoveryMixin, safety.CrossDiskSequenceTests):
    FILENAME, UPDATE_EVENT = 'movie.mkv', 'RADARR_UPDATE_INTENT'


@skip_inherited
class TvRecoveryTests(RecoveryMixin, safety.TvCrossDiskSequenceTests):
    FILENAME, UPDATE_EVENT = 'episode.mkv', 'SONARR_UPDATE_INTENT'

    def test_recovery_refuses_changed_episode_associations(self):
        api = self.arr.api

        def changed(endpoint, body=None):
            result = api(endpoint, body)
            if endpoint == 'episode?seriesId=11' and body is None:
                result[0]['monitored'] = False
            return result
        self.arr.api = changed
        self.refuses('Episode files or associations changed since the copy started')


class RecoveryProgramTests(unittest.TestCase):
    """The NAS receipt program compiles, and nas_receipt checks what it must (run with fakes)."""

    @classmethod
    def setUpClass(cls):
        config = load_config(ROOT / 'config/runtime.example.json', environ={})
        with patch('runtime_config.get_config', return_value=config), \
                patch('planner_settings.get_settings',
                      return_value=load_settings(ROOT / 'config/planner.example.json')), \
                patch('media_layout.get_targets',
                      return_value=load_targets(ROOT / 'config/storage-targets.example.json')):
            cls.rp = importlib.import_module('recover_cross_published')

    def test_receipt_program_compiles_and_dispatches_receipt(self):
        for m in (self.rp.execute_cross_movie, self.rp.execute_cross_tv):
            program = self.rp.receipt_program(m)
            compile(program, 'receipt', 'exec')
            self.assertIn('print(json.dumps(nas_receipt(data)))', program)
            self.assertNotIn('print(json.dumps(nas_operation(data)))', program)

    def receipt_namespace(self, source, destination):
        m = self.rp.execute_cross_tv
        ns = dict(vars(m))
        def fake_pair(source_path, destination_path, layout):
            m.nas_pair(source_path, destination_path, layout)  # real path/layout validation
            return phase2.FakeRemotePath(source_path), phase2.FakeRemotePath(destination_path)
        ns['nas_pair'] = fake_pair
        ns['canonical_existing'] = lambda p: None
        ns['inventory'] = lambda root: copy.deepcopy(source if 'media04' in str(root) else destination)
        ns['file_metadata'] = lambda root: m.inventory_metadata(source)
        ns['sync_parents'] = lambda *a: None
        exec(inspect.getsource(self.rp.nas_receipt), ns)
        return ns['nas_receipt']

    def data(self, **changes):
        return dict(dict(operation='receipt', execution_id=EXECUTION,
                         source='/volume3/media04/TV/Library/Example',
                         destination='/volume4/media01/TV/Current/Example',
                         inventory={'e.mkv': [4, 'h', 1, 2]}), **changes)

    def test_nas_receipt_rebuilds_identity_only_when_both_trees_match(self):
        source = {'e.mkv': [4, 'h', 123, 456]}
        with patch.object(os.path, 'lexists', return_value=False):
            receipt = self.receipt_namespace(source, {'e.mkv': [4, 'h', 9, 9]})(self.data())
            self.assertEqual((receipt['operation'], receipt['reconstructed'], receipt['source_id']),
                             ('copy', True, [4, 104]))
            with self.assertRaisesRegex(self.rp.execute_cross_tv.Refused, 'NAS content mismatch'):
                self.receipt_namespace(source, {'e.mkv': [4, 'other', 9, 9]})(self.data())
            for field, value in (('operation', 'delete'), ('execution_id', '../x'),
                                 ('destination', '/volume3/media04/TV/Current/Example')):
                with self.subTest(field=field), self.assertRaises(self.rp.execute_cross_tv.Refused):
                    self.receipt_namespace(source, source)(self.data(**{field: value}))
        with patch.object(os.path, 'lexists', return_value=True), \
                self.assertRaisesRegex(self.rp.execute_cross_tv.Refused, 'Unexpected work directory'):
            self.receipt_namespace(source, source)(self.data())


if __name__ == '__main__':
    unittest.main()
