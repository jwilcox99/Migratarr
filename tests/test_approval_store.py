"""approval_store.py: atomic, locked approvals shared by approve_execution.py and the UI."""
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import approval_store
import read_api
from executor_manifest import approval_is_current
from test_read_api import RUN, Fixture, eid


def refuse(ok, message):
    if not ok:
        raise AssertionError(message)


class ApprovalStoreTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.fx = Fixture(self.base)

    def record(self):
        return json.loads(approval_store.record_path(self.base, RUN).read_text())

    def test_approve_writes_entries_executors_accept(self):
        new = approval_store.approve(self.base, RUN, [eid(6), eid(4)], self.fx.hash)
        self.assertEqual(new, [eid(6), eid(4)])
        for n in (6, 4):
            self.assertTrue(approval_is_current(self.base, RUN, eid(n), self.fx.hash, refuse))
        entries = [e for e in self.record()['history'] if e.get('batch', {}).get('source') == 'ui']
        self.assertEqual([e['execution_id'] for e in entries], [eid(6), eid(4)])
        self.assertEqual(entries[0]['batch']['size'], 2)
        states = read_api.run_status(self.base, RUN)['rows']
        self.assertEqual({r['execution_id']: r['approval'] for r in states}[eid(6)], read_api.APPROVED)

    def test_stale_hash_or_ineligible_row_writes_nothing(self):
        before = approval_store.record_path(self.base, RUN).read_bytes()
        with self.assertRaisesRegex(approval_store.ApprovalError, 'manifest changed'):
            approval_store.approve(self.base, RUN, [eid(6)], 'old-hash')
        with self.assertRaisesRegex(approval_store.ApprovalError, 'Not eligible'):
            approval_store.approve(self.base, RUN, [eid(6), eid(1)], self.fx.hash)  # 0001 already moved
        with self.assertRaisesRegex(approval_store.ApprovalError, 'Not eligible'):
            approval_store.approve(self.base, RUN, [RUN + '-9999'], self.fx.hash)
        with self.assertRaisesRegex(approval_store.ApprovalError, 'Nothing selected'):
            approval_store.approve(self.base, RUN, [], self.fx.hash)
        self.assertEqual(approval_store.record_path(self.base, RUN).read_bytes(), before)

    def test_already_current_approvals_are_not_duplicated(self):
        self.assertEqual(approval_store.approve(self.base, RUN, [eid(2), eid(3)], self.fx.hash), [])
        self.assertEqual(approval_store.approve(self.base, RUN, [eid(2), eid(6)], self.fx.hash), [eid(6)])

    def test_revoke_then_reapprove(self):
        self.assertTrue(approval_store.revoke(self.base, RUN, eid(2)))
        self.assertFalse(approval_is_current(self.base, RUN, eid(2), self.fx.hash, refuse))
        self.assertFalse(approval_store.revoke(self.base, RUN, eid(2)))
        self.assertEqual(approval_store.approve(self.base, RUN, [eid(2)], self.fx.hash), [eid(2)])
        self.assertTrue(approval_is_current(self.base, RUN, eid(2), self.fx.hash, refuse))

    def test_failed_write_leaves_the_old_record_and_no_temp_files(self):
        before = approval_store.record_path(self.base, RUN).read_bytes()
        with patch('os.replace', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                approval_store.approve(self.base, RUN, [eid(6)], self.fx.hash)
        self.assertEqual(approval_store.record_path(self.base, RUN).read_bytes(), before)
        self.assertEqual([p.name for p in (self.base / 'approvals').iterdir() if p.suffix == '.tmp'], [])

    def test_concurrent_writers_never_lose_an_entry(self):
        ids = [eid(n) for n in (4, 6, 10)]
        # 0010 needs reconciliation but is still eligible by the CLI's rule; revoke/approve all race.
        threads = [threading.Thread(target=approval_store.approve, args=(self.base, RUN, [i], self.fx.hash))
                   for i in ids]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        approved = set(self.record()['approved_execution_ids'])
        self.assertTrue(set(ids) <= approved)

    def test_invalid_record_refuses(self):
        approval_store.record_path(self.base, RUN).write_text(json.dumps({'run_id': 'other'}))
        with self.assertRaises(approval_store.ApprovalError):
            approval_store.approve(self.base, RUN, [eid(6)], self.fx.hash)


if __name__ == '__main__':
    unittest.main()
