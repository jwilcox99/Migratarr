"""migratarr_plan.py: one command for the planning pipeline, stopping at the first failure."""
import contextlib
import io
import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

import migratarr_plan
from runtime_config import load_config
from service_keys import SecretError
from test_read_api import RUN, Fixture

ROOT = Path(__file__).resolve().parents[1]

# Each stub logs its name and arguments; the snapshot and manifest stubs publish a
# prepared run so the summary reads real read_api output.
STUB = textwrap.dedent('''
    import os, shutil, sys
    from pathlib import Path
    name = Path(sys.argv[0]).name
    with open(os.environ['STUB_LOG'], 'a') as log:
        log.write(' '.join([name] + sys.argv[1:]) + '\\n')
    if os.environ.get('STUB_FAIL') == name:
        sys.exit(3)
    base, staging = Path(os.environ['STUB_BASE']), Path(os.environ['STUB_STAGING'])
    if name == 'snapshot_run.py':
        shutil.copytree(staging / 'runs' / os.environ['STUB_RUN'], base / 'runs' / os.environ['STUB_RUN'])
    if name == 'build_execution_manifest.py':
        for part in ('manifests/' + sys.argv[1], 'approvals', 'execution_logs'):
            shutil.copytree(staging / part, base / part, dirs_exist_ok=True)
''')


class PlanCommandTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.repo, self.base, staging = root / 'repo', root / 'base', root / 'staging'
        for folder in (self.repo, self.base, staging):
            folder.mkdir()
        Fixture(staging)
        (self.base / 'runs').mkdir()
        for _, args in migratarr_plan.STEPS:
            (self.repo / args[0]).write_text(STUB)
        self.log = root / 'steps.log'
        env = dict(STUB_LOG=str(self.log), STUB_BASE=str(self.base), STUB_STAGING=str(staging), STUB_RUN=RUN)
        self.enterContext_(patch.dict(os.environ, env))
        self.enterContext_(patch.object(migratarr_plan, 'preflight', return_value=([], self.base)))

    def enterContext_(self, manager):  # TestCase.enterContext needs 3.11; CI starts at 3.10
        manager.__enter__()
        self.addCleanup(manager.__exit__, None, None, None)

    def run_plan(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = migratarr_plan.main(list(argv), repo=self.repo)
        return code, out.getvalue()

    def steps_run(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def test_runs_every_step_in_order_and_hands_the_new_run_to_the_manifest(self):
        code, out = self.run_plan()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.steps_run(), [
            'dry_run_movies.py', 'dry_run_tv.py', 'build_move_plan.py', 'audit_overrides.py',
            'snapshot_run.py', 'build_execution_manifest.py ' + RUN])
        self.assertIn('MIGRATARR PLAN: COMPLETE', out)
        self.assertIn(f'Run:       {RUN}', out)
        self.assertIn('Planned:   12 moves (1 blocked, 11 in the manifest)', out)
        self.assertIn('CROSS_DISK_TRANSFER: 11 (66.00 GB)', out)
        self.assertIn('nothing has moved', out)

    def test_stops_at_the_first_failure(self):
        os.environ['STUB_FAIL'] = 'dry_run_tv.py'
        code, out = self.run_plan()
        self.assertEqual(code, 1)
        self.assertEqual(self.steps_run(), ['dry_run_movies.py', 'dry_run_tv.py'])
        self.assertIn('Step 2 (Score TV) failed; later steps were not run.', out)
        self.assertIn('No new run was created.', out)
        self.assertEqual(list((self.base / 'runs').iterdir()), [])

    def test_manifest_failure_names_the_run_without_a_manifest(self):
        os.environ['STUB_FAIL'] = 'build_execution_manifest.py'
        code, out = self.run_plan()
        self.assertEqual(code, 1)
        self.assertIn(f'Run {RUN} was created but has no manifest; do not approve from it.', out)

    def test_preflight_failure_runs_nothing_and_check_runs_only_preflight(self):
        with patch.object(migratarr_plan, 'preflight', return_value=(['tmdb missing'], self.base)):
            code, out = self.run_plan()
        self.assertEqual(code, 2)
        self.assertIn('PREFLIGHT FAILED', out)
        self.assertEqual(self.steps_run(), [])
        code, out = self.run_plan('--check')
        self.assertEqual((code, self.steps_run()), (0, []))


class PreflightTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name)
        git = ['git', '-c', 'user.name=t', '-c', 'user.email=t@example.invalid']
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        (self.repo / 'tracked.py').write_text('x = 1\n')
        subprocess.run(['git', 'add', '.'], cwd=self.repo, check=True)
        subprocess.run([*git, 'commit', '-qm', 'init'], cwd=self.repo, check=True)
        self.config = load_config(ROOT / 'config/examples/runtime.v1.example.json', environ={})

    def preflight(self, allow_dirty=False, failing=()):
        def read_key(service, runtime=None):
            if service in failing:
                raise SecretError(f'Service credentials: {service} credential (file /x) is missing or empty')
            return 'value'
        with patch('runtime_config.get_config', return_value=self.config), \
                patch('planner_settings.get_settings', return_value=None), \
                patch('service_keys.read_key', read_key):
            return migratarr_plan.preflight(self.repo, allow_dirty)

    def test_clean_repository_and_readable_credentials_pass(self):
        self.assertEqual(self.preflight(), ([], self.config.base_path))

    def test_each_unreadable_credential_is_named_without_its_value(self):
        problems, _ = self.preflight(failing=('tmdb', 'sonarr'))
        self.assertEqual(len(problems), 2)
        self.assertTrue(any('tmdb credential' in p for p in problems))
        self.assertTrue(any('sonarr credential' in p for p in problems))
        self.assertFalse(any('value' in p for p in problems))

    def test_uncommitted_changes_refuse_unless_allowed(self):
        (self.repo / 'tracked.py').write_text('x = 2\n')
        problems, _ = self.preflight()
        self.assertEqual(len(problems), 1)
        self.assertIn('uncommitted changes (tracked.py)', problems[0])
        self.assertEqual(self.preflight(allow_dirty=True)[0], [])

    def test_invalid_runtime_config_stops_before_other_checks(self):
        with patch('runtime_config.get_config', side_effect=ValueError('bad base_path')):
            problems, base = migratarr_plan.preflight(self.repo, False)
        self.assertEqual((problems, base), (['Runtime config: bad base_path'], None))


class StepListTests(unittest.TestCase):
    def test_steps_match_the_documented_pipeline_and_exist(self):
        self.assertEqual([args[0] for _, args in migratarr_plan.STEPS], [
            'dry_run_movies.py', 'dry_run_tv.py', 'build_move_plan.py', 'audit_overrides.py',
            'snapshot_run.py', 'build_execution_manifest.py'])
        for _, args in migratarr_plan.STEPS:
            self.assertTrue((ROOT / args[0]).is_file(), args[0])

    def test_no_step_approves_executes_or_batches(self):
        for _, args in migratarr_plan.STEPS:
            self.assertFalse(args[0].startswith(('approve_', 'execute_', 'batch_', 'recover_')), args)
            self.assertFalse({'--execute', '--yes', '--approve', '--approve-batch'} & set(args), args)

if __name__ == '__main__':
    unittest.main()
