"""Golden characterization of the four live executors (unified-execution-core, gate 1).

Records, for every scenario, the executor's result or refusal, every journal
event with its normalized details, and the ordered trace of Arr reads/updates,
NAS operations, approval reloads and filesystem inventories. The refactor must
reproduce these byte for byte. Also pins each CLI wrapper's stdout/stderr/exit
code/journal framing and the NAS programs' operation allowlists and lock name.

Nothing here changes executor code. Regenerate only for an intended, reviewed
behavior change:  MIGRATARR_UPDATE_GOLDEN=1 python3 -m unittest tests.test_executor_golden
"""
import ast
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

# Modules, not classes: importing TestCase classes by name would make discovery
# run their tests a second time from this module.
import test_executor_safety as safety
import test_executor_safety_phase2 as phase2
from planner_settings import load_settings
from runtime_config import load_config
from storage_targets import load_targets

EXECUTION = safety.EXECUTION
ROOT = safety.ROOT

GOLDEN = Path(__file__).resolve().parent / 'golden' / 'executor_sequences.json'
UPDATE = bool(os.environ.get('MIGRATARR_UPDATE_GOLDEN'))
LIVE_EXECUTORS = ('execute_movie_nas', 'execute_tv_nas', 'execute_cross_movie', 'execute_cross_tv')


def load_golden():
    return json.loads(GOLDEN.read_text(encoding='utf-8')) if GOLDEN.exists() else {}


def check_golden(test, key, record):
    record = json.loads(json.dumps(record))  # tuples -> lists, int keys -> str
    if UPDATE:
        data = load_golden()
        data[key] = record
        GOLDEN.parent.mkdir(exist_ok=True)
        GOLDEN.write_text(json.dumps(data, indent=1, sort_keys=True) + '\n', encoding='utf-8')
        return
    data = load_golden()
    test.assertIn(key, data, 'No golden record; regenerate with MIGRATARR_UPDATE_GOLDEN=1 after review')
    test.assertEqual(record, data[key], 'Executor behavior changed for ' + key)


def normalize(value, base):
    if isinstance(value, dict):
        return {str(k): normalize(v, base) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        # Hashed inventory entry: [size, sha256, inode, mtime_ns]; inode/mtime vary per run.
        if (len(value) == 4 and isinstance(value[0], int) and isinstance(value[1], str)
                and len(value[1]) == 64 and isinstance(value[2], int) and isinstance(value[3], int)):
            return [value[0], value[1], '<ino>', '<mtime_ns>']
        return [normalize(v, base) for v in value]
    if isinstance(value, str):
        return value.replace(str(base), '<BASE>')
    return value


def golden_only(cls):
    """Reuse a sequence test's fixture without re-running its inherited tests."""
    for name in dir(cls):
        if name.startswith('test_') and not name.startswith('test_golden'):
            setattr(cls, name, None)
    return cls


class GoldenSequenceMixin:
    KEY = None
    FILENAME = None
    UPDATE_EVENT = None

    def setUp(self):
        super().setUp()
        self.journal = []
        self.filename = self.FILENAME
        m = self.m

        def where(p):
            p = Path(p)
            return 'source' if p == self.src else 'destination' if p == self.dst else 'other:' + p.name

        def traced(name, original, describe):
            def wrapper(*args, **kwargs):
                self.trace.append(describe(*args))
                return original(*args, **kwargs)
            self.stack.enter_context(patch.object(m, name, side_effect=wrapper))

        traced('load_plan', m.load_plan, lambda *a: 'plan:load')
        traced('inventory', m.inventory, lambda root: 'fs:inventory:' + where(root))
        traced('file_metadata', m.file_metadata, lambda root: 'fs:metadata:' + where(root))
        api = self.arr.api

        def traced_api(endpoint, body=None):
            if body is None:
                self.trace.append('arr:get:' + endpoint)
            return api(endpoint, body)
        self.arr.api = traced_api

    def run_move(self, live=True):
        def log(event, **details):
            self.journal.append((event, details))
            self.trace.append('log:' + event)
            self.hook('log:' + event)
        return self.m.execute(self.base, EXECUTION, live, self.arr, log, self.transport)

    def capture(self, scenario, live=True):
        result = error = None
        try:
            result = self.run_move(live)
        except BaseException as exc:  # record, never swallow silently: compared below
            error = [type(exc).__name__, str(exc)]
        record = dict(result=result, error=normalize(error, self.base),
                      journal=[[event, normalize(details, self.base)] for event, details in self.journal],
                      trace=self.trace)
        check_golden(self, self.KEY + '/' + scenario, record)
        return record

    def on(self, event, action):
        def hook(name):
            if name == event:
                action()
        self.hook = hook

    def write(self, root, data):
        (root / self.filename).write_bytes(data)

    # Scenarios shared by all four pairings.
    def test_golden_check_only(self):
        self.assertEqual(self.capture('check_only', live=False)['result'], 'CHECK_ONLY')

    def test_golden_success(self):
        self.assertEqual(self.capture('success')['result'], 'SUCCESS')

    def test_golden_existing_destination(self):
        self.dst.mkdir()
        self.assertIsNotNone(self.capture('existing_destination')['error'])

    def test_golden_lock_tag(self):
        self.arr.record['tags'] = [1]
        self.assertIsNotNone(self.capture('lock_tag')['error'])

    def test_golden_source_change_after_preflight(self):
        self.on('log:PREFLIGHT_OK', lambda: self.write(self.src, self.content + b' changed'))
        self.assertIsNotNone(self.capture('source_change_after_preflight')['error'])

    def test_golden_approval_revoked_after_preflight(self):
        def revoke():
            self.approval['history'].append(dict(execution_id=EXECUTION, action='REVOKE'))
            self.write_approval()
        self.on('log:PREFLIGHT_OK', revoke)
        self.assertIsNotNone(self.capture('approval_revoked_after_preflight')['error'])

    def test_golden_arr_update_failure(self):
        def fail():
            raise RuntimeError('simulated Arr failure')
        self.on('arr:update', fail)
        self.assertIsNotNone(self.capture('arr_update_failure')['error'])

    def test_golden_wrong_arr_path_after_update(self):
        self.on('arr:update', lambda: self.arr.record.__setitem__('path', '/unexpected'))
        self.assertIsNotNone(self.capture('wrong_arr_path_after_update')['error'])


class CrossScenarios:
    def test_golden_copy_timeout(self):
        def fail():
            raise TimeoutError('simulated uncertain copy response')
        self.on('nas:copy', fail)
        self.assertIsNotNone(self.capture('copy_timeout')['error'])

    def test_golden_source_change_after_copy(self):
        self.on('nas:copy', lambda: self.write(self.src, self.content + b' changed during copy'))
        self.assertIsNotNone(self.capture('source_change_after_copy')['error'])

    def test_golden_corrupt_destination(self):
        self.on('nas:copy', lambda: self.write(self.dst, b'corrupt'))
        self.assertIsNotNone(self.capture('corrupt_destination')['error'])

    def test_golden_approval_revoked_after_copy(self):
        def revoke():
            self.approval['history'].append(dict(execution_id=EXECUTION, action='REVOKE'))
            self.write_approval()
        self.on('nas:copy', revoke)
        self.assertIsNotNone(self.capture('approval_revoked_after_copy')['error'])

    def test_golden_approval_revoked_after_arr_update(self):
        def revoke():
            self.approval['approved_execution_ids'] = []
            self.write_approval()
        self.on('arr:update', revoke)
        self.assertIsNotNone(self.capture('approval_revoked_after_arr_update')['error'])

    def test_golden_delete_failure(self):
        def fail():
            raise TimeoutError('simulated uncertain delete response')
        self.on('nas:delete', fail)
        self.assertIsNotNone(self.capture('delete_failure')['error'])


class SameDiskScenarios:
    def test_golden_rename_failure(self):
        def fail():
            raise OSError(22, 'simulated rename refusal')
        self.on('nas:rename', fail)
        self.assertIsNotNone(self.capture('rename_failure')['error'])

    def test_golden_approval_revoked_after_rename(self):
        # Characterizes current behavior, whatever it is: recorded, not judged here.
        def revoke():
            self.approval['history'].append(dict(execution_id=EXECUTION, action='REVOKE'))
            self.write_approval()
        self.on('nas:rename', revoke)
        self.capture('approval_revoked_after_rename')


@golden_only
class CrossMovieGolden(GoldenSequenceMixin, CrossScenarios, safety.CrossDiskSequenceTests):
    KEY, FILENAME = 'execute_cross_movie', 'movie.mkv'


@golden_only
class CrossTvGolden(GoldenSequenceMixin, CrossScenarios, safety.TvCrossDiskSequenceTests):
    KEY, FILENAME = 'execute_cross_tv', 'episode.mkv'


@golden_only
class SameDiskMovieGolden(GoldenSequenceMixin, SameDiskScenarios, phase2.MovieSameDiskTests):
    KEY, FILENAME = 'execute_movie_nas', 'movie.mkv'


@golden_only
class SameDiskTvGolden(GoldenSequenceMixin, SameDiskScenarios, phase2.TVSameDiskTests):
    KEY, FILENAME = 'execute_tv_nas', 'episode.mkv'

    def test_golden_episode_association_change_after_rename(self):
        api = self.arr.api

        def api_changed(endpoint, body=None):
            result = api(endpoint, body)
            if endpoint == 'episode?seriesId=11' and body is None and not self.src.exists():
                result[0]['monitored'] = False
            return result
        self.arr.api = api_changed
        self.assertIsNotNone(self.capture('episode_association_change_after_rename')['error'])


class CliWrapperGolden(safety.OfflineTest):
    """stdout/stderr/exit code and journal framing of each executor's main()."""

    class Transport:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def run_main(self, name, extra=(), outcome='CHECK_ONLY', journal_before=None):
        module = self.modules[name]
        self.fixture(name)
        logs = self.base / 'execution_logs'
        logs.mkdir(exist_ok=True)
        journal = logs / (EXECUTION + '.jsonl')
        if journal_before is not None:
            journal.write_text(''.join(json.dumps(e) + '\n' for e in journal_before))
        calls = []

        def fake_execute(base, execution_id, live, arr, log, transport, *rest):
            calls.append(dict(live=live, extra_args=len(rest)))
            log('PREFLIGHT_OK', manifest_sha256='m')
            if outcome == 'REFUSE':
                raise module.Refused('simulated refusal')
            return outcome

        arr_class = 'Sonarr' if hasattr(module, 'Sonarr') else 'Radarr'
        out, err = io.StringIO(), io.StringIO()
        argv = [name + '.py', EXECUTION, '--base', str(self.base), *extra]
        with patch.object(sys, 'argv', argv), patch.object(module, 'execute', side_effect=fake_execute), \
                patch.object(module, 'NasTransport', self.Transport), \
                patch.object(module, arr_class, return_value=object()), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = module.main()
                raised = None
            except BaseException as exc:
                rc, raised = None, [type(exc).__name__, str(exc)]
        events = [json.loads(line) for line in journal.read_text().splitlines()] if journal.exists() else []
        for event in events:
            event.pop('utc', None)
        return normalize(dict(rc=rc, raised=raised, stdout=out.getvalue(), stderr=err.getvalue(),
                              execute_calls=calls, journal=events), self.base)

    def test_golden_cli_wrappers(self):
        cases = {
            'check_only': dict(),
            'success': dict(extra=('--execute',), outcome='SUCCESS'),
            'refused': dict(extra=('--execute',), outcome='REFUSE'),
            'prior_uncertain_attempt': dict(extra=('--execute',), journal_before=[
                dict(execution_id=EXECUTION, event='START', live=True),
                dict(execution_id=EXECUTION, event='COPY_INTENT'),
                dict(execution_id=EXECUTION, event='RENAME_INTENT')]),
        }
        for name in LIVE_EXECUTORS:
            for case, kwargs in cases.items():
                with self.subTest(executor=name, case=case):
                    self.base_reset()
                    check_golden(self, 'cli/' + name + '/' + case, self.run_main(name, **kwargs))
        with self.subTest(executor='execute_movie_nas', case='resume_without_failed_journal'):
            self.base_reset()
            check_golden(self, 'cli/execute_movie_nas/resume_without_failed_journal',
                         self.run_main('execute_movie_nas', extra=('--resume-nfs-refusal',)))

    def base_reset(self):
        for sub in ('execution_logs', 'manifests', 'approvals'):
            folder = self.base / sub
            if folder.exists():
                for path in sorted(folder.rglob('*'), key=lambda p: len(p.parts), reverse=True):
                    path.rmdir() if path.is_dir() else path.unlink()
                folder.rmdir()


class NasProgramGolden(unittest.TestCase):
    """Operation allowlist and lock file of each generated NAS program (D4, D5).

    Imports the executors without OfflineTest's progress() mock, because the NAS
    program is built from inspect.getsource() of the real functions.
    """

    @classmethod
    def setUpClass(cls):
        config = load_config(ROOT / 'config/runtime.example.json', environ={})
        with patch('runtime_config.get_config', return_value=config), \
                patch('planner_settings.get_settings',
                      return_value=load_settings(ROOT / 'config/planner.example.json')), \
                patch('media_layout.get_targets',
                      return_value=load_targets(ROOT / 'config/storage-targets.example.json')):
            import importlib
            cls.modules = {name: importlib.import_module(name) for name in LIVE_EXECUTORS}

    @staticmethod
    def allowlists(program):
        found = []
        for node in ast.walk(ast.parse(program)):
            if isinstance(node, ast.Compare) and len(node.ops) == 1 and isinstance(node.ops[0], ast.In) \
                    and isinstance(node.comparators[0], ast.Set) and 'operation' in ast.unparse(node.left):
                found.append(sorted(ast.literal_eval(node.comparators[0])))
        return found

    def test_golden_nas_programs(self):
        for name in LIVE_EXECUTORS:
            with self.subTest(executor=name):
                module = self.modules[name]
                program = module.remote_program('media04') if 'cross' not in name else module.remote_program()
                compile(program, name + '-nas', 'exec')
                locks = sorted({line.strip() for line in program.splitlines() if "/tmp/migratarr-" in line})
                check_golden(self, 'nas/' + name, dict(operation_allowlists=self.allowlists(program),
                                                       lock_lines=locks))


if __name__ == '__main__':
    unittest.main()
