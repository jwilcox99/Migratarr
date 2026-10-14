"""Cross-disk copies keep the source's permission bits on folders and files.

Before this, copy_tree created every folder 0755 (owned by the NAS SSH user), so
Radarr/Sonarr running as another user lost write access to moved media: new
episode imports, upgrades and renames failed with "Permission denied".
"""
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_executor_safety import OfflineTest

CROSS = ('execute_cross_movie', 'execute_cross_tv')

FOLDERS = {'': 0o777, 'Season 01': 0o2775, 'Season 02': 0o750, 'Season 02/Extras': 0o555}
FILES = {'Season 01/e01.mkv': 0o664, 'Season 02/e01.mkv': 0o640,
         'Season 02/Extras/x.mkv': 0o444, 'tvshow.nfo': 0o666}


def mode(path):
    return stat.S_IMODE(os.lstat(path).st_mode)


class CopyTreePermissionTests(OfflineTest):
    def setUp(self):
        super().setUp()
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory())).resolve()
        self.src = self.root / 'source'
        self.src.mkdir()
        for name in sorted(FOLDERS, key=len):
            if name:
                (self.src / name).mkdir()
        for name, bits in FILES.items():
            (self.src / name).write_bytes(name.encode() * 64)
            os.chmod(self.src / name, bits)
        # Deepest first, so a read-only folder doesn't block chmod of its parent's children.
        for name, bits in sorted(FOLDERS.items(), key=lambda x: -len(x[0])):
            os.chmod(self.src / name, bits)
        self.addCleanup(self.restore_writable)

    def restore_writable(self):
        for folder, _, _ in os.walk(self.root):
            os.chmod(folder, 0o700)

    def test_folders_and_files_keep_source_modes(self):
        old = os.umask(0o077)  # chmod must win over any umask
        self.addCleanup(os.umask, old)
        for name in CROSS:
            m = self.modules[name]
            stage = self.root / ('stage-' + name)
            with self.subTest(executor=name):
                expected = m.inventory(self.src)
                m.copy_tree(self.src, stage, expected)
                for folder, bits in FOLDERS.items():
                    self.assertEqual(oct(mode(stage / folder)), oct(bits), folder or '<root>')
                for file, bits in FILES.items():
                    self.assertEqual(oct(mode(stage / file)), oct(bits), file)
                    self.assertEqual((stage / file).read_bytes(), (self.src / file).read_bytes())
                self.assertEqual(m.content_only(m.inventory(stage)), m.content_only(expected))

    def test_folders_stay_private_until_filled(self):
        for name in CROSS:
            m = self.modules[name]
            stage = self.root / ('private-' + name)
            seen = []
            real_mkdir = Path.mkdir

            def record(path, mode=0o777, *args, **kwargs):
                seen.append(mode)
                return real_mkdir(path, mode, *args, **kwargs)

            with self.subTest(executor=name), patch.object(m.Path, 'mkdir', record):
                m.copy_tree(self.src, stage, m.inventory(self.src))
                self.assertEqual(set(seen), {0o700})

    def test_source_folder_replaced_by_file_refuses(self):
        for name in CROSS:
            m = self.modules[name]
            stage = self.root / ('swap-' + name)
            expected = m.inventory(self.src)
            real_lstat = os.lstat

            def swapped(path, *args, **kwargs):
                s = real_lstat(path, *args, **kwargs)
                if Path(path) == self.src / 'Season 01':
                    return os.stat_result((stat.S_IFREG | 0o644,) + tuple(s)[1:])
                return s

            with self.subTest(executor=name), patch.object(m.os, 'lstat', swapped), \
                    self.assertRaisesRegex(m.Refused, 'Source changed before copy'):
                m.copy_tree(self.src, stage, expected)


if __name__ == '__main__':
    unittest.main()
