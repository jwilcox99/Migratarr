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

    def test_demo_accepts_a_relative_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, MIGRATARR_HOME=str(Path(tmp, 'real-home')), PYTHONPATH=str(ROOT))
            result = subprocess.run([sys.executable, '-m', 'migratarr.cli', 'demo', '--dir', 'demo', '--no-ui'],
                                    cwd=tmp, env=env, capture_output=True, text=True, timeout=600)
            self.assertEqual(result.returncode, 0, result.stdout[-3000:] + result.stderr[-3000:])
            targets = json.loads(Path(tmp, 'demo/config/storage-targets.json').read_text())
            self.assertIn(str(Path(tmp).resolve()), json.dumps(targets))  # stored as absolute paths

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
