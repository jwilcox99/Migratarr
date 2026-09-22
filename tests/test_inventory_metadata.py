"""Metadata scan parity and standalone NAS-helper dependency coverage."""
import ast
import hashlib
import stat
from types import SimpleNamespace
from unittest.mock import patch

from test_executor_safety import OfflineTest


NAS_EXECUTORS = ('execute_movie_nas', 'execute_tv_nas', 'execute_cross_movie')


def progress(message):
    pass


class InventoryMetadataTests(OfflineTest):
    def setUp(self):
        super().setUp()
        for name in NAS_EXECUTORS:
            self.stack.enter_context(patch.object(self.modules[name], 'progress', progress))
        self.tree = self.base / 'tree'
        self.tree.mkdir()
        (self.tree / 'Season 01').mkdir()
        self.media = self.tree / 'Season 01' / 'episode.mkv'
        self.content = b'content to hash and verify'
        self.media.write_bytes(self.content)
        (self.tree / 'empty.txt').touch()

    def expected_metadata(self):
        result = {'Season 01': ['directory']}
        for path in (self.media, self.tree / 'empty.txt'):
            info = path.stat()
            result[path.relative_to(self.tree).as_posix()] = [
                info.st_size, info.st_ino, info.st_mtime_ns]
        return result

    def test_scan_matches_actual_metadata_without_opening_files(self):
        expected = self.expected_metadata()
        for name in NAS_EXECUTORS:
            with self.subTest(executor=name), patch('pathlib.Path.open',
                    side_effect=AssertionError('metadata scan opened content')):
                self.assertEqual(self.modules[name].file_metadata(self.tree), expected)

    def test_hash_inventory_and_metadata_conversion_match_scan(self):
        expected = self.expected_metadata()
        for name in NAS_EXECUTORS:
            with self.subTest(executor=name):
                m = self.modules[name]
                inventory = m.inventory(self.tree)
                self.assertEqual(inventory['Season 01/episode.mkv'][1],
                                 hashlib.sha256(self.content).hexdigest())
                self.assertEqual(inventory['empty.txt'][1], hashlib.sha256(b'').hexdigest())
                self.assertEqual(m.inventory_metadata(inventory), expected)
                self.assertEqual(m.file_metadata(self.tree), expected)

    def test_links_nested_devices_and_special_files_refuse_with_local_exception(self):
        original = type(self.media).lstat
        info = self.media.stat()
        for name in NAS_EXECUTORS:
            m = self.modules[name]
            for kind, mode, device, message in (
                    ('link', stat.S_IFLNK, info.st_dev, 'Link or nested device found'),
                    ('device', stat.S_IFREG, info.st_dev + 1, 'Link or nested device found'),
                    ('fifo', stat.S_IFIFO, info.st_dev, 'Nonregular file found')):
                def lstat(path):
                    if path == self.media:
                        return SimpleNamespace(st_mode=mode, st_dev=device)
                    return original(path)
                with self.subTest(executor=name, kind=kind), \
                        patch.object(type(self.media), 'lstat', lstat):
                    with self.assertRaisesRegex(m.Refused, message):
                        m.file_metadata(self.tree)

    def test_filesystem_errors_propagate(self):
        for name in NAS_EXECUTORS:
            with self.subTest(executor=name):
                with self.assertRaises(FileNotFoundError):
                    self.modules[name].file_metadata(self.base / 'absent')

    def test_empty_metadata_is_allowed_but_empty_hash_inventory_refuses(self):
        empty = self.base / 'empty'
        empty.mkdir()
        for name in NAS_EXECUTORS:
            m = self.modules[name]
            with self.subTest(executor=name):
                self.assertEqual(m.file_metadata(empty), {})
                with self.assertRaisesRegex(m.Refused, 'Source contains no media data'):
                    m.inventory(empty)

    def test_directory_change_during_hashing_still_refuses(self):
        for name in NAS_EXECUTORS:
            m = self.modules[name]
            before = self.expected_metadata()
            after = dict(before, added=['directory'])
            with self.subTest(executor=name), \
                    patch.object(m, 'file_metadata', side_effect=(before, after)):
                with self.assertRaisesRegex(m.Refused, 'changed while hashing'):
                    m.inventory(self.tree)

    def helper_namespace(self, name):
        # Compile the complete helper first, then execute only its definitions and
        # inert configuration. Dispatch/locking/mutation code is never executed.
        code = (self.modules[name].remote_program('media04') if name != 'execute_cross_movie'
                else self.modules[name].remote_program())
        compile(code, '<NAS helper>', 'exec')
        tree = ast.parse(code)
        nodes = []
        for node in tree.body:
            if isinstance(node, ast.Import):
                self.assertFalse(any(alias.name.startswith('executor_') for alias in node.names))
                node.names = [alias for alias in node.names if alias.name != 'fcntl']
                if node.names:
                    nodes.append(node)
            elif isinstance(node, ast.ImportFrom):
                self.assertIn(node.module, {'pathlib', 'datetime'})
                nodes.append(node)
            elif isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                nodes.append(node)
            elif isinstance(node, ast.Assign) and all(
                    isinstance(t, ast.Name) and t.id in {'DISKS', 'REMOTE_ROOT'} for t in node.targets):
                nodes.append(node)
        namespace = {'__name__': 'isolated_nas_helper'}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), '<NAS definitions>', 'exec'), namespace)
        namespace['progress'] = lambda message: None
        return namespace

    def test_embedded_helpers_scan_and_hash_without_local_module_dependencies(self):
        expected = self.expected_metadata()
        for name in NAS_EXECUTORS:
            with self.subTest(executor=name):
                ns = self.helper_namespace(name)
                self.assertEqual(ns['file_metadata'](self.tree), expected)
                inventory = ns['inventory'](self.tree)
                self.assertEqual(ns['inventory_metadata'](inventory), expected)
                self.assertEqual(inventory['Season 01/episode.mkv'][1],
                                 hashlib.sha256(self.content).hexdigest())

    def test_embedded_helper_uses_its_own_refusal_type(self):
        original = type(self.media).lstat
        info = self.media.stat()
        def lstat(path):
            if path == self.media:
                return SimpleNamespace(st_dev=info.st_dev, st_mode=stat.S_IFLNK)
            return original(path)
        for name in NAS_EXECUTORS:
            ns = self.helper_namespace(name)
            with self.subTest(executor=name), patch.object(type(self.media), 'lstat', lstat):
                with self.assertRaisesRegex(ns['Refused'], 'Link or nested device found'):
                    ns['file_metadata'](self.tree)
