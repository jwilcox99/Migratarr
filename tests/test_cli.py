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
        for name in ('ui', 'plan', 'status', 'approve', 'doctor'):
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

    def test_every_listed_module_exists_except_phase_2_doctor(self):
        import importlib.util
        missing = {name for name, (module, _) in migratarr_cli.COMMANDS.items()
                   if name not in ('fake', 'gone') and importlib.util.find_spec(module) is None}
        self.assertLessEqual(missing, {'doctor'})


class PackagingTests(unittest.TestCase):
    def test_pyproject_lists_every_top_level_module(self):
        import re
        root = Path(__file__).resolve().parents[1]
        text = (root / 'pyproject.toml').read_text(encoding='utf-8')
        listed = set(re.findall(r'^\s+"([A-Za-z0-9_]+)",$', text.split('py-modules = [', 1)[1].split(']', 1)[0], re.M))
        self.assertEqual(listed, {p.stem for p in root.glob('*.py')})


if __name__ == '__main__':
    unittest.main()
