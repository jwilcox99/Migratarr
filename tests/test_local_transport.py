"""Phase 3: the executors' NAS program channel (migratarr/engine/transport.py).

nas_ssh must build exactly the commands the executors built before the
channel existed; local must run the very same shipped program on this host,
for real, against real folders.
"""
import importlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from planner_settings import load_settings
from runtime_config import RuntimeConfig, load_config
from storage_targets import parse_targets, load_targets

ROOT = Path(__file__).resolve().parents[1]
EXECUTORS = ('execute_movie_nas', 'execute_tv_nas', 'execute_cross_movie', 'execute_cross_tv')
EXECUTION = '20261026T120000Z-0001'


def import_executors(config, targets):
    with patch('runtime_config.get_config', return_value=config), \
            patch('planner_settings.get_settings', return_value=load_settings(ROOT / 'config/planner.example.json')), \
            patch('media_layout.get_targets', return_value=targets):
        return {name: importlib.import_module(name) for name in EXECUTORS}


class SshCommandsUnchangedTests(unittest.TestCase):
    """The exact argv each executor ran before the channel (main @ 696d7d4 + gate 2)."""

    def setUp(self):
        self.config = load_config(ROOT / 'config/examples/runtime.v1.example.json', environ={})
        self.targets = load_targets(ROOT / 'config/storage-targets.example.json')
        self.modules = import_executors(self.config, self.targets)

    def test_master_call_and_exit(self):
        for name, module in self.modules.items():
            with self.subTest(executor=name), patch.object(module, 'RUNTIME', self.config):
                runs = []
                with patch('subprocess.run', side_effect=lambda argv, **kw: runs.append((argv, kw))):
                    with module.NasTransport() as transport:
                        socket = transport.temp.name + '/socket'
                        options = ['-i', self.config.ssh_key, '-o', 'IdentitiesOnly=yes', '-o', 'BatchMode=yes',
                                   '-o', 'StrictHostKeyChecking=yes', '-o', 'ConnectTimeout=15',
                                   '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=3',
                                   '-o', 'ControlPath=' + socket]
                        self.assertEqual(transport.options, options)
                        self.assertEqual(transport.command('PROGRAM'), [
                            'ssh', *options, '-o', 'BatchMode=yes', 'migratarr@nas.example',
                            "/usr/bin/python3 -c PROGRAM"])
                self.assertEqual(runs[0], (['ssh', *options, '-M', '-N', '-f', '-o', 'ControlPersist=yes',
                                            'migratarr@nas.example'], dict(check=True, timeout=120)))
                self.assertEqual(runs[1][0], ['ssh', *options, '-O', 'exit', 'migratarr@nas.example'])
                self.assertFalse(os.path.exists(socket.rsplit('/', 1)[0]))

    def test_call_sends_program_through_channel(self):
        for name, module in self.modules.items():
            with self.subTest(executor=name), patch.object(module, 'RUNTIME', self.config):
                transport = object.__new__(module.NasTransport)
                transport.options = ['-x']
                seen = []

                def fake(command, label, input=None, **kwargs):
                    seen.append(command)
                    return '{"result": "OK", "operation": "check"}'
                folder = 'Movies' if 'movie' in name else 'TV'
                src = Path(f'/mnt/nas/media04/{folder}/Library/X')
                dst = Path(f'/mnt/nas/media0{1 if "cross" in name else 4}/{folder}/'
                           f'{"Common" if folder == "Movies" else "Current"}/X')
                with patch.object(module, 'run_progress', side_effect=fake), \
                        patch.object(module, 'remote_program', return_value='PROGRAM'):
                    args = (EXECUTION,) if 'cross' in name else ()
                    transport.call('check', src, dst, {}, *args)
                self.assertEqual(seen, [['ssh', '-x', '-o', 'BatchMode=yes', 'migratarr@nas.example',
                                         '/usr/bin/python3 -c PROGRAM']])


class LocalTopologyTests(unittest.TestCase):
    """Real renames, copies and deletes through the shipped programs, on this host."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name).resolve()
        second = None
        if os.path.isdir('/dev/shm') and os.stat('/dev/shm').st_dev != base.stat().st_dev:
            shm = tempfile.TemporaryDirectory(dir='/dev/shm')
            self.addCleanup(shm.cleanup)
            second = Path(shm.name).resolve()
        self.disks = {'disk1': base / 'disk1', 'disk2': (second or base) / 'disk2'}
        self.cross_possible = second is not None
        data = json.loads((ROOT / 'config/examples/storage-targets.local.example.json').read_text())
        data['targets'] = [t for t in data['targets'] if t['id'] in self.disks]
        for t in data['targets']:
            t['path'] = str(self.disks[t['id']])
        for media in data['placement'].values():
            for category in media:
                media[category] = ['disk1'] if category in ('Common', 'Current') else ['disk2']
        self.targets = parse_targets(data)
        runtime = json.loads((ROOT / 'config/examples/runtime.local.example.json').read_text())
        runtime.pop('storage_targets')
        self.config = RuntimeConfig(runtime, self.targets)
        for disk in self.disks.values():
            for folder in ('Movies/Common', 'Movies/Library', 'TV/Current', 'TV/Library'):
                (disk / folder).mkdir(parents=True)
        self.modules = import_executors(self.config, self.targets)
        # progress() is shipped inside the program, so it stays real; keep its output quiet.
        quiet = patch('sys.stderr', io.StringIO())
        quiet.start()
        self.addCleanup(quiet.stop)
        for module in self.modules.values():
            for attr, value in (('RUNTIME', self.config), ('TARGETS', self.targets)):
                p = patch.object(module, attr, value)
                p.start()
                self.addCleanup(p.stop)

    def item(self, disk, folder, name='Example (2001)'):
        path = self.disks[disk] / folder / name
        path.mkdir()
        (path / 'movie.mkv').write_bytes(os.urandom(4096))
        (path / 'extras').mkdir()
        (path / 'extras/trailer.mkv').write_bytes(os.urandom(1024))
        return path

    def test_no_ssh_and_this_python(self):
        module = self.modules['execute_movie_nas']
        with patch('subprocess.run', side_effect=AssertionError('no ssh in topology local')):
            with module.NasTransport() as transport:
                self.assertEqual(transport.command('P'), [sys.executable, '-I', '-c', 'P'])
                self.assertEqual(transport.where(), 'this host')

    def test_same_disk_rename_movie_and_tv(self):
        for name, folder, target in (('execute_movie_nas', 'Movies', 'Common'), ('execute_tv_nas', 'TV', 'Current')):
            with self.subTest(executor=name):
                module = self.modules[name]
                src = self.item('disk1', folder + '/Library')
                dst = self.disks['disk1'] / folder / target / src.name
                before = module.inventory(src)
                with module.NasTransport() as transport:
                    transport.call('check', src, dst, before)
                    self.assertTrue(src.exists())
                    transport.call('rename', src, dst, before)
                self.assertFalse(src.exists())
                self.assertEqual(module.file_metadata(dst), module.inventory_metadata(before))

    def test_same_disk_refuses_changed_source(self):
        module = self.modules['execute_movie_nas']
        src = self.item('disk1', 'Movies/Library')
        dst = self.disks['disk1'] / 'Movies/Common' / src.name
        before = module.inventory(src)
        (src / 'movie.mkv').write_bytes(b'changed')
        with module.NasTransport() as transport, self.assertRaises(module.Refused):
            transport.call('rename', src, dst, before)
        self.assertTrue(src.exists())
        self.assertFalse(dst.exists())

    def test_cross_disk_copy_then_delete(self):
        if not self.cross_possible:
            self.skipTest('needs two filesystems (/tmp and /dev/shm)')
        for name, folder, target in (('execute_cross_movie', 'Movies', 'Common'),
                                     ('execute_cross_tv', 'TV', 'Current')):
            with self.subTest(executor=name):
                module = self.modules[name]
                src = self.item('disk2', folder + '/Library')
                dst = self.disks['disk1'] / folder / target / src.name
                before = module.inventory(src)
                with module.NasTransport() as transport:
                    transport.call('check', src, dst, before, EXECUTION)
                    self.assertFalse(dst.exists())
                    receipt = transport.call('copy', src, dst, before, EXECUTION)
                    self.assertTrue(src.exists() and dst.exists())
                    transport.call('delete', src, dst, before, EXECUTION, receipt)
                self.assertFalse(src.exists())
                self.assertEqual(sorted(p.name for p in dst.rglob('*')), ['extras', 'movie.mkv', 'trailer.mkv'])
                self.assertEqual(list(src.parent.glob('.migratarr-*')), [])
                self.assertEqual(list(dst.parent.glob('.migratarr-*')), [])


class SameFilesystemRefusalTests(LocalTopologyTests):
    """Two disks on one filesystem: the shipped program refuses a cross-disk move, in one plain line."""

    def setUp(self):
        real = os.path.isdir
        with patch('os.path.isdir', lambda p: p != '/dev/shm' and real(p)):  # both disks in one temp folder
            super().setUp()
        self.assertFalse(self.cross_possible)

    def test_cross_disk_refusal_is_one_plain_line(self):
        for name, folder, target in (('execute_cross_movie', 'Movies', 'Common'),
                                     ('execute_cross_tv', 'TV', 'Current')):
            with self.subTest(executor=name):
                module = self.modules[name]
                src = self.item('disk2', folder + '/Library')
                dst = self.disks['disk1'] / folder / target / src.name
                before = module.inventory(src)
                shown = io.StringIO()
                with patch('sys.stderr', shown), module.NasTransport() as transport, \
                        self.assertRaises(module.Refused) as caught:
                    transport.call('check', src, dst, before, EXECUTION)
                self.assertEqual(str(caught.exception),
                                 "NAS check refused: the source and target disks are on the same filesystem, so "
                                 "a cross-disk move can't run between them. Use two different disks, or put both "
                                 "categories on one disk.")
                self.assertNotIn('Traceback', shown.getvalue())
                self.assertIn('NAS check: Refused: NAS filesystems are not distinct', shown.getvalue())
                self.assertTrue(src.exists())
                self.assertFalse(dst.exists())

    # Only the test above runs here (the others are LocalTopologyTests').
    test_no_ssh_and_this_python = test_same_disk_rename_movie_and_tv = None
    test_same_disk_refuses_changed_source = test_cross_disk_copy_then_delete = None


if __name__ == '__main__':
    unittest.main()
