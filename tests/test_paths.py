"""migratarr.paths: the data folder and module launches that make an installed copy work (phase 7)."""
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import migratarr_jobs
import migratarr_plan
from migratarr import paths

ROOT = Path(__file__).resolve().parents[1]


class HomeTests(unittest.TestCase):
    def test_migratarr_home_wins(self):
        self.assertEqual(paths.home({'MIGRATARR_HOME': '~/m'}), Path('~/m').expanduser())
        self.assertEqual(paths.home_source({'MIGRATARR_HOME': '/x'}), 'MIGRATARR_HOME')

    def test_a_clone_keeps_using_itself(self):
        with patch.object(paths, 'is_clone', return_value=True):
            self.assertEqual(paths.home({}), paths.CODE)
            self.assertEqual(paths.config_dir({}), paths.CODE / 'config')

    def test_an_install_uses_the_user_data_folder(self):
        with patch.object(paths, 'is_clone', return_value=False):
            self.assertEqual(paths.home({'XDG_DATA_HOME': '/xdg'}), Path('/xdg/migratarr'))
            self.assertEqual(paths.home({}), Path('~/.local/share/migratarr').expanduser())
            self.assertEqual(paths.home_source({}), 'default for an installed copy')

    def test_config_files_follow_the_data_folder(self):
        from planner_settings import _default_path
        from runtime_config import runtime_path
        from migratarr.settings import locate
        env = {'MIGRATARR_HOME': '/data/m'}
        self.assertEqual(runtime_path(env), Path('/data/m/config/runtime.json'))
        self.assertEqual(_default_path(env), Path('/data/m/config/planner.json'))
        files = locate(env)
        self.assertEqual((files.runtime, files.storage_targets, files.planner),
                         (Path('/data/m/config/runtime.json'), Path('/data/m/config/storage-targets.json'),
                          Path('/data/m/config/planner.json')))
        self.assertEqual(runtime_path({'MIGRATARR_HOME': '/data/m', 'MIGRATARR_CONFIG': '/c/r.json'}),
                         Path('/c/r.json'))

    def test_new_installs_put_runs_in_the_data_folder(self):
        from migratarr.settings.ui_backend import default_base
        self.assertEqual(default_base({'MIGRATARR_HOME': '/data/m'}), '/data/m')


class ModuleLaunchTests(unittest.TestCase):
    def test_module_command(self):
        self.assertEqual(paths.module_command('execute_cross_tv.py', 'id', '--base', Path('/b'), python='py'),
                         ['py', '-m', 'migratarr.execute_cross_tv', 'id', '--base', '/b'])
        self.assertEqual(paths.module_name('migratarr_jobs.py'), 'migratarr.jobs')

    def test_every_launched_script_is_a_module_in_the_package(self):
        import importlib.util
        names = set(migratarr_jobs.EXECUTORS.values()) | {'migratarr_jobs.py', 'migratarr_plan.py'} | \
            {args[0] for _, args in migratarr_plan.STEPS}
        for name in names:
            spec = importlib.util.find_spec(paths.module_name(name))
            self.assertIsNotNone(spec, name)
            self.assertEqual(Path(spec.origin).parent, paths.PACKAGE, name)

    def test_a_job_started_as_a_module_is_recognised_as_alive(self):
        job_id = '20261027T100000Z-plan'
        # A stand-in with the same command line shape as start_job's: python -m migratarr_jobs run ... <job>
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, 'migratarr').mkdir()
            Path(tmp, 'migratarr', '__init__.py').write_text('')
            Path(tmp, 'migratarr', 'jobs.py').write_text('import time\ntime.sleep(30)\n')
            proc = subprocess.Popen([sys.executable, '-m', 'migratarr.jobs', 'run', '--job', job_id, 'plan'],
                                    cwd=tmp)
            try:
                time.sleep(0.3)
                self.assertTrue(migratarr_jobs.process_alive(proc.pid, job_id))
                self.assertFalse(migratarr_jobs.process_alive(proc.pid, '20261027T100001Z-plan'))
            finally:
                proc.kill()
                proc.wait()


class InstalledPlanTests(unittest.TestCase):
    def test_an_installed_copy_skips_the_git_check(self):
        from runtime_config import load_config
        config = load_config(ROOT / 'config/examples/runtime.v1.example.json', environ={})
        with tempfile.TemporaryDirectory() as tmp, \
                patch('runtime_config.get_config', return_value=config), \
                patch('planner_settings.get_settings', return_value=None), \
                patch('service_keys.read_key', return_value='value'), \
                patch.object(migratarr_plan, 'health_failures', return_value=[]):
            self.assertEqual(migratarr_plan.preflight(Path(tmp), False), ([], config.base_path))

    def test_code_digest_changes_with_the_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, 'a.py').write_text('x = 1\n')
            first = paths.code_digest(['a.py', 'b.py'], Path(tmp))
            Path(tmp, 'a.py').write_text('x = 2\n')
            self.assertNotEqual(first, paths.code_digest(['a.py', 'b.py'], Path(tmp)))


if __name__ == '__main__':
    unittest.main()
