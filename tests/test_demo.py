"""`migratarr demo`: the real planning pipeline on the made-up library, in a throwaway folder."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class DemoTests(unittest.TestCase):
    def test_demo_plans_the_made_up_library_without_touching_real_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, decoy = Path(tmp, 'demo'), Path(tmp, 'real-home')
            env = dict(os.environ, MIGRATARR_HOME=str(decoy), MIGRATARR_CONFIG=str(decoy / 'runtime.json'))
            # Its own process: the demo sets its environment and swaps urlopen for the made-up library.
            result = subprocess.run([sys.executable, '-m', 'migratarr.cli', 'demo', '--dir', str(home), '--no-ui'],
                                    cwd=ROOT, env=env, capture_output=True, text=True, timeout=600)
            self.assertEqual(result.returncode, 0, result.stdout[-3000:] + result.stderr[-3000:])
            self.assertFalse(decoy.exists())  # the user's own data folder is never read or written
            runs = list((home / 'runs').iterdir())
            self.assertEqual(len(runs), 1)
            manifest = list((home / 'manifests').glob('*.csv')) + list((home / 'manifests').glob('**/*.csv'))
            self.assertTrue(manifest, list(home.iterdir()))
            self.assertIn('nothing has moved', result.stdout)
            self.assertNotIn('retrying', result.stdout)  # the demo's stand-in TMDB answers, never fails
            self.assertIn('CROSS_DISK_TRANSFER', result.stdout)
            self.assertIn('SAME_DISK_RENAME', result.stdout)
            ending = result.stdout.split('SAME_DISK_RENAME')[-1]
            self.assertNotIn('python3 ', ending)  # commands, not script paths
            # No approve or plain `migratarr ui`: those would act on the user's own data folder.
            self.assertNotIn('approve --', ending)
            # Settings come from the data folder, so browsing the demo again needs MIGRATARR_HOME.
            self.assertIn(f'MIGRATARR_HOME={home.resolve()} migratarr ui --read-only', ending)
            runtime = json.loads((home / 'config/runtime.json').read_text())
            self.assertEqual(runtime['topology'], 'local')
            self.assertFalse(list(home.glob('secrets/*')))  # no keys stored
            # Believable sizes and disk space, from sparse files and the demo disks' pretend capacity.
            gb = [float(x) for x in __import__('re').findall(r'TRANSFER: \d+ \(([\d.]+) GB\)', result.stdout)]
            self.assertTrue(gb and gb[0] > 10, result.stdout[-1500:])
            used = sum(f.stat().st_blocks * 512 for f in (home / 'disks').rglob('*') if f.is_file())
            self.assertLess(used, 64 * 1024 * 1024)  # hundreds of GB reported, almost nothing written
            self.assertTrue((home / 'demo-disks.json').exists())

    def test_demo_accepts_a_relative_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, MIGRATARR_HOME=str(Path(tmp, 'real-home')), PYTHONPATH=str(ROOT))
            result = subprocess.run([sys.executable, '-m', 'migratarr.cli', 'demo', '--dir', 'demo', '--no-ui'],
                                    cwd=tmp, env=env, capture_output=True, text=True, timeout=600)
            self.assertEqual(result.returncode, 0, result.stdout[-3000:] + result.stderr[-3000:])
            targets = json.loads(Path(tmp, 'demo/config/storage-targets.json').read_text())
            self.assertIn(str(Path(tmp).resolve()), json.dumps(targets))  # stored as absolute paths

    def test_sizes_are_stable_and_believable(self):
        from migratarr import demo
        for media, low, high in (('Movie', 1.5, 28), ('TV', 6, 90)):
            size = demo.title_size(media, 'Some Title (2001)') / 1024 ** 3
            self.assertTrue(low <= size <= high, size)
            self.assertEqual(demo.title_size(media, 'Some Title (2001)'), demo.title_size(media, 'Some Title (2001)'))

    def test_no_sparse_files_means_tiny_files_not_a_full_disk(self):
        from unittest.mock import patch
        from migratarr import demo
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp, 'title.mkv')
            real_stat = Path.stat

            def dense(self, *a, **k):  # pretend every byte was written
                st = real_stat(self, *a, **k)
                return type('S', (), dict(st_size=st.st_size, st_blocks=st.st_size // 512 + 1))()
            with patch.object(Path, 'stat', dense):
                self.assertFalse(demo.sized_file(target, 20 * 1024 ** 3))
            self.assertEqual(target.stat().st_size, 4096)

    def test_pretend_capacity_only_for_demo_disks(self):
        from migratarr import demo
        with tempfile.TemporaryDirectory() as tmp:
            disks = {Path(tmp, 'disks', 'demo01'): (4 * demo.TB, demo.TB)}
            usage = demo.disk_usage(disks)
            self.assertEqual(usage(str(Path(tmp, 'disks', 'demo01', 'Movies', 'Common'))).free, demo.TB)
            self.assertNotEqual(usage(tmp).total, 4 * demo.TB)  # anything else: the real disk
            stat = demo.statvfs(disks)
            self.assertEqual(stat(str(Path(tmp, 'disks', 'demo01'))).f_bavail * 4096, demo.TB)
            self.assertIsNone(demo.demo_disks(tmp))

    def test_demo_refuses_a_folder_that_is_not_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, 'keep.txt').write_text('mine')
            result = subprocess.run([sys.executable, '-m', 'migratarr.cli', 'demo', '--dir', tmp, '--no-ui'],
                                    cwd=ROOT, capture_output=True, text=True, timeout=120)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('not empty', result.stderr)
            self.assertEqual(Path(tmp, 'keep.txt').read_text(), 'mine')


if __name__ == '__main__':
    unittest.main()
