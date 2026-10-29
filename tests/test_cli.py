"""migratarr_cli.py: the `migratarr` command dispatches to the existing scripts unchanged."""
import contextlib
import io
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

import migratarr_cli


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = migratarr_cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class CliTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp_path = tmp.name
        Path(tmp.name, 'fake_cmd.py').write_text(textwrap.dedent('''
            import sys
            print('argv=' + repr(sys.argv))
            if '--fail' in sys.argv:
                sys.exit('it failed')
            sys.exit(3 if '--three' in sys.argv else None)
        '''))
        sys.path.insert(0, tmp.name)
        self.addCleanup(sys.path.remove, tmp.name)
        self.addCleanup(sys.modules.pop, 'fake_cmd', None)
        patcher = patch.dict(migratarr_cli.COMMANDS, {'fake': ('fake_cmd', 'a test command'),
                                                      'gone': ('no_such_module_xyz', 'missing')})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_help_lists_commands(self):
        code, out, _ = run()
        self.assertEqual(code, 0)
        for name in ('ui', 'plan', 'status', 'approve', 'doctor', 'reconcile'):
            self.assertIn(name, out)

    def test_version(self):
        self.assertEqual(run('--version')[:2], (0, 'migratarr ' + migratarr_cli.__version__ + '\n'))

    def test_unknown_command(self):
        code, _, err = run('nope')
        self.assertEqual(code, 2)
        self.assertIn('unknown command', err)

    def test_missing_module_is_reported_not_raised(self):
        code, _, err = run('gone')
        self.assertEqual(code, 2)
        self.assertIn('not available', err)

    def test_dispatch_passes_arguments_and_exit_codes(self):
        saved = list(sys.argv)
        code, out, _ = run('fake', '--x', 'y')
        self.assertEqual(code, 0)
        self.assertIn("argv=['migratarr fake', '--x', 'y']", out)
        self.assertEqual(sys.argv, saved)
        self.assertEqual(run('fake', '--three')[0], 3)
        code, _, err = run('fake', '--fail')
        self.assertEqual((code, err), (1, 'it failed\n'))

    def test_missing_settings_are_a_message_not_a_traceback(self):
        Path(self.tmp_path, 'fake_cfg.py').write_text(
            'from migratarr.runtime_config import ConfigError\n'
            'raise ConfigError("Runtime configuration: cannot load /x/config/runtime.json")\n')
        self.addCleanup(sys.modules.pop, 'fake_cfg', None)
        with patch.dict(migratarr_cli.COMMANDS, {'cfg': ('fake_cfg', 'needs settings')}):
            code, _, err = run('cfg')
        self.assertEqual(code, 2)
        self.assertIn('cannot load /x/config/runtime.json', err)
        self.assertIn('migratarr doctor', err)
        self.assertNotIn('Traceback', err)

    def test_every_command_help_works_before_setup(self):
        # A fresh install has no settings; "<command> --help" must still print usage.
        import os
        import subprocess
        names = sorted(n for n in migratarr_cli.COMMANDS if n not in ('fake', 'gone'))
        env = dict(os.environ, MIGRATARR_HOME=str(Path(self.tmp_path, 'fresh-home')))
        root = Path(__file__).resolve().parents[1]
        for name in names:
            with self.subTest(command=name):
                r = subprocess.run([sys.executable, '-m', 'migratarr.cli', name, '--help'], cwd=str(root),
                                   env=env, capture_output=True, text=True, timeout=60)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertIn('usage:', r.stdout)
        self.assertFalse(Path(self.tmp_path, 'fresh-home').exists(), '--help must not create the data folder')

    def test_every_listed_command_exists(self):
        import importlib.util
        missing = {name for name, (module, _) in migratarr_cli.COMMANDS.items()
                   if name not in ('fake', 'gone') and importlib.util.find_spec(module) is None}
        self.assertEqual(missing, set())


class PackagingTests(unittest.TestCase):
    ROOT = Path(__file__).resolve().parents[1]

    def test_only_the_package_is_installed(self):
        import re
        text = (self.ROOT / 'pyproject.toml').read_text(encoding='utf-8')
        self.assertNotIn('py-modules', text)  # no top-level modules in site-packages
        listed = set(re.findall(r'"([A-Za-z0-9_.]+)"', re.search(r'^packages = \[(.*?)\]', text, re.M).group(1)))
        found = {'.'.join(p.parent.relative_to(self.ROOT).parts) for p in (self.ROOT / 'migratarr').glob('**/__init__.py')}
        self.assertEqual(listed, found)
        self.assertIn('migratarr = "migratarr.cli:main"', text)

    def test_top_level_files_are_only_wrappers_for_the_package(self):
        """A clone keeps `python3 <script>.py` working; each file only forwards to migratarr/<module>."""
        import importlib.util
        from migratarr.paths import module_name
        for path in sorted(self.ROOT.glob('*.py')):
            with self.subTest(path.name):
                text = path.read_text(encoding='utf-8')
                target = module_name(path.name)
                self.assertIn(f"run_module('{target}', run_name='__main__')", text)
                self.assertIn(f"import_module('{target}')", text)
                self.assertLess(len(text.splitlines()), 20)
                self.assertIsNotNone(importlib.util.find_spec(target), target)

    def test_frozen_placement_modules_match_their_pins(self):
        """movie_placement_v1 / tv_placement_v1 are frozen; `sha256sum -c` in migratarr/ checks them."""
        import hashlib
        for pin in sorted((self.ROOT / 'migratarr').glob('*_placement_v1.sha256')):
            digest, name = pin.read_text().split()
            source = (pin.parent / name).read_bytes().replace(b'\r\n', b'\n')
            self.assertEqual(hashlib.sha256(source).hexdigest(), digest, name)


if __name__ == '__main__':
    unittest.main()
