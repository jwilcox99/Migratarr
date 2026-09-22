"""Offline characterization of shared same-disk NFS visibility handling."""
import errno
import unittest
from unittest.mock import Mock, patch

from test_executor_safety import OfflineTest


class NfsVisibilityTests(OfflineTest):
    def setUp(self):
        super().setUp()
        self.src = self.base / 'source'
        self.dst = self.base / 'destination'
        self.dst.mkdir()
        (self.dst / 'media.mkv').write_bytes(b'test media')
        self.logs = []

    def log(self, event, **details):
        self.logs.append((event, details))

    def test_both_executor_wrappers_accept_verified_destination(self):
        for name in ('execute_movie_nas', 'execute_tv_nas'):
            module = self.modules[name]
            expected = module.inventory(self.dst)
            with self.subTest(executor=name):
                module.verify_nfs_after_rename(self.src, self.dst, expected, self.log)
        self.assertEqual(self.logs, [])

    def test_source_visibility_retries_once_and_logs_ready(self):
        module = self.modules['execute_movie_nas']
        expected = module.inventory(self.dst)
        src = Mock()
        src.lstat.side_effect = [Mock(), FileNotFoundError()]
        with patch('executor_nfs.time.monotonic', side_effect=(0, 1)), \
                patch('executor_nfs.time.sleep') as sleep:
            module.verify_nfs_after_rename(src, self.dst, expected, self.log,
                                           timeout=60, interval=2, metadata_only=True)
        self.assertEqual([event for event, _ in self.logs],
                         ['NFS_VISIBILITY_WAIT', 'NFS_VISIBILITY_READY'])
        sleep.assert_called_once_with(2)

    def test_timeout_preserves_executor_refusal_type_and_message(self):
        for name in ('execute_movie_nas', 'execute_tv_nas'):
            module = self.modules[name]
            src = Mock()
            src.lstat.return_value = Mock()
            with self.subTest(executor=name), \
                    patch('executor_nfs.time.monotonic', side_effect=(0, 2)):
                with self.assertRaisesRegex(module.Refused,
                                            'NFS visibility verification timed out'):
                    module.verify_nfs_after_rename(src, self.dst, {}, self.log,
                                                   timeout=1, interval=0)

    def test_readable_inventory_mismatch_refuses_without_retry(self):
        module = self.modules['execute_tv_nas']
        expected = module.inventory(self.dst)
        (self.dst / 'media.mkv').write_bytes(b'changed')
        with patch('executor_nfs.time.sleep') as sleep:
            with self.assertRaisesRegex(module.Refused, 'NFS destination inventory mismatch'):
                module.verify_nfs_after_rename(self.src, self.dst, expected, self.log)
        sleep.assert_not_called()
        self.assertEqual(self.logs, [])

    def test_metadata_only_avoids_content_inventory(self):
        module = self.modules['execute_movie_nas']
        expected = module.inventory(self.dst)
        with patch.object(module, 'inventory', side_effect=AssertionError('content hash called')):
            module.verify_nfs_after_rename(self.src, self.dst, expected, self.log,
                                           metadata_only=True)

    def test_only_missing_or_stale_errors_retry(self):
        module = self.modules['execute_tv_nas']
        for error in (OSError(errno.ENOENT, 'missing'), OSError(errno.ESTALE, 'stale')):
            src = Mock()
            src.lstat.side_effect = [error, FileNotFoundError()]
            expected = module.inventory(self.dst)
            with self.subTest(error=error.errno), \
                    patch('executor_nfs.time.monotonic', side_effect=(0, 1)), \
                    patch('executor_nfs.time.sleep'):
                module.verify_nfs_after_rename(src, self.dst, expected, self.log,
                                               metadata_only=True)
            self.logs.clear()
        src = Mock()
        src.lstat.side_effect = OSError(errno.EACCES, 'denied')
        with self.assertRaises(OSError):
            module.verify_nfs_after_rename(src, self.dst, {}, self.log)


if __name__ == '__main__':
    unittest.main()
