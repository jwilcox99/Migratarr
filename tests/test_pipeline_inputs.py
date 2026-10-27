"""Planning and snapshotting refuse pipeline files left over from an earlier session.

On 2026-10-15 both dry runs stopped on a missing TMDB token, build_move_plan.py
then read the 2026-09-23 dry-run CSVs, and snapshot_run.py froze the result as
run 20261015T152923Z: a TV-only plan of moves that had already happened.
"""
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from pipeline_inputs import OVERRIDE_VARIABLE, input_problems, require_fresh_inputs

ROOT = Path(__file__).resolve().parents[1]
HOUR = 3600


class Files:
    def __init__(self, case):
        tmp = tempfile.TemporaryDirectory()  # enterContext needs 3.11; CI starts at 3.10
        case.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.now = time.time()

    def write(self, name, hours_ago, text='header\n'):
        path = self.base / name
        path.write_text(text)
        stamp = self.now - hours_ago * HOUR
        os.utime(path, (stamp, stamp))
        return path


class InputProblemTests(unittest.TestCase):
    def setUp(self):
        self.f = Files(self)

    def test_recent_inputs_pass(self):
        paths = [self.f.write('movie_dry_run.csv', 1), self.f.write('tv_dry_run.csv', 0.5)]
        self.assertEqual(input_problems(paths, now=self.f.now), [])
        require_fresh_inputs('step', paths, environ={}, now=self.f.now)

    def test_missing_and_stale_inputs_are_named(self):
        old = self.f.write('movie_dry_run.csv', 22 * 24)
        problems = input_problems([old, self.f.base / 'tv_dry_run.csv'], now=self.f.now)
        self.assertEqual(len(problems), 2)
        self.assertTrue(any('movie_dry_run.csv is 528.0 h old' in p for p in problems), problems)
        self.assertIn('tv_dry_run.csv is missing', problems)

    def test_age_limit_boundary(self):
        self.assertEqual(input_problems([self.f.write('a.csv', 11.9)], now=self.f.now), [])
        self.assertEqual(len(input_problems([self.f.write('b.csv', 12.1)], now=self.f.now)), 1)

    def test_output_must_follow_its_inputs_but_inputs_may_be_in_any_order(self):
        movie = self.f.write('movie_dry_run.csv', 2)
        tv = self.f.write('tv_dry_run.csv', 3)   # written before the movie file: fine
        plan = self.f.write('move_plan.csv', 2.5)  # but built before the movie dry run
        problems = input_problems([movie, tv, plan], newer_than=[movie, tv], now=self.f.now)
        self.assertEqual(len(problems), 1)
        self.assertIn('move_plan.csv', problems[0])
        self.assertIn('older than movie_dry_run.csv', problems[0])
        os.utime(plan, (self.f.now, self.f.now))
        self.assertEqual(input_problems([movie, tv, plan], newer_than=[movie, tv], now=self.f.now), [])

    def test_refusal_message_and_explicit_override(self):
        old = self.f.write('movie_dry_run.csv', 48)
        with self.assertRaises(SystemExit) as refused:
            require_fresh_inputs('build_move_plan.py', [old], environ={}, now=self.f.now)
        self.assertIn('REFUSED: build_move_plan.py', str(refused.exception))
        self.assertIn(OVERRIDE_VARIABLE + '=1', str(refused.exception))
        for value in ('', '0', 'yes', 'true'):
            with self.subTest(value=value), self.assertRaises(SystemExit):
                require_fresh_inputs('step', [old], environ={OVERRIDE_VARIABLE: value}, now=self.f.now)
        require_fresh_inputs('step', [old], environ={OVERRIDE_VARIABLE: '1'}, now=self.f.now)


class PipelineScriptTests(unittest.TestCase):
    """Run the real scripts against a temporary base; both must refuse before any I/O."""

    def setUp(self):
        self.f = Files(self)
        self.env = dict(os.environ, MIGRATARR_CONFIG=str(ROOT / 'config/examples/runtime.v1.example.json'),
                        MIGRATARR_BASE_PATH=str(self.f.base),
                        MIGRATARR_PLANNER_CONFIG=str(ROOT / 'config/planner.example.json'),
                        MIGRATARR_STORAGE_TARGETS=str(ROOT / 'config/storage-targets.example.json'))
        self.env.pop(OVERRIDE_VARIABLE, None)

    def run_script(self, name):
        return subprocess.run([sys.executable, str(ROOT / name)], cwd=self.f.base, env=self.env,
                              capture_output=True, text=True, timeout=60)

    def test_planner_refuses_dry_runs_from_an_earlier_session(self):
        self.f.write('movie_dry_run.csv', 22 * 24)
        self.f.write('tv_dry_run.csv', 22 * 24)
        result = self.run_script('build_move_plan.py')
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('REFUSED: build_move_plan.py', result.stderr)
        self.assertFalse((self.f.base / 'move_plan.csv').exists())

    def test_snapshot_refuses_stale_or_out_of_order_inputs(self):
        cases = {
            'stale dry runs': (22 * 24, 22 * 24, 0.1),
            'plan older than dry run': (1, 1, 2),
            'missing plan': (1, 1, None),
        }
        for label, (movie, tv, plan) in cases.items():
            with self.subTest(label):
                for name in ('movie_dry_run.csv', 'tv_dry_run.csv', 'move_plan.csv'):
                    (self.f.base / name).unlink(missing_ok=True)
                self.f.write('movie_dry_run.csv', movie)
                self.f.write('tv_dry_run.csv', tv)
                if plan is not None:
                    self.f.write('move_plan.csv', plan)
                result = self.run_script('snapshot_run.py')
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn('REFUSED: snapshot_run.py', result.stderr)
                self.assertFalse((self.f.base / 'runs').exists(), 'no run directory before refusing')

    def test_snapshot_records_the_check_with_the_run_code(self):
        source = (ROOT / 'migratarr' / 'snapshot_run.py').read_text()
        self.assertIn('"pipeline_inputs.py",', source.split('CODE_FILES = [', 1)[1].split(']', 1)[0])


if __name__ == '__main__':
    unittest.main()
