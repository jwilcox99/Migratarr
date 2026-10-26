"""The exact program text each executor ships to the NAS, pinned by SHA-256.

Recorded before the execution-core refactor (unified-execution-core, gate 2)
moved shared NAS-side functions into migratarr/engine/nas_side.py. Moving code
must not change one byte of what runs on the NAS. Regenerate only for an
intended, reviewed change to NAS-side code:
    MIGRATARR_UPDATE_GOLDEN=1 python3 -m unittest discover -s tests -p test_nas_program_parity.py
"""
from contextlib import ExitStack
import hashlib
import importlib
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from planner_settings import load_settings
from runtime_config import load_config
from storage_targets import load_targets

ROOT = Path(__file__).resolve().parents[1]
EXECUTORS = ('execute_movie_nas', 'execute_tv_nas', 'execute_cross_movie', 'execute_cross_tv')

GOLDEN = Path(__file__).resolve().parent / 'golden' / 'nas_programs.json'
UPDATE = bool(os.environ.get('MIGRATARR_UPDATE_GOLDEN'))
DISKS = ('media01', 'media02', 'media03', 'media04')


def fingerprint(text):
    return dict(sha256=hashlib.sha256(text.encode()).hexdigest(), length=len(text))


class NasProgramParityTests(unittest.TestCase):
    def setUp(self):
        # Same example configuration as the offline executor tests, but with the
        # executors' real functions: inspect.getsource() can't read a mock.
        stack = ExitStack()
        self.addCleanup(stack.close)
        config = load_config(ROOT / 'config/runtime.example.json', environ={})
        with patch('runtime_config.get_config', return_value=config), \
                patch('planner_settings.get_settings',
                      return_value=load_settings(ROOT / 'config/planner.example.json')), \
                patch('media_layout.get_targets',
                      return_value=load_targets(ROOT / 'config/storage-targets.example.json')):
            self.modules = {name: importlib.import_module(name) for name in EXECUTORS}
        for module in self.modules.values():
            stack.enter_context(patch.object(module, 'RUNTIME', config))

    def programs(self):
        import recover_cross_published as recovery
        found = {}
        for name in ('execute_movie_nas', 'execute_tv_nas'):
            for disk in DISKS:
                found[f'{name}/{disk}'] = self.modules[name].remote_program(disk)
        for name in ('execute_cross_movie', 'execute_cross_tv'):
            module = self.modules[name]
            found[name] = module.remote_program()
            found['recover_cross_published/' + name] = recovery.receipt_program(module)
        return found

    def test_shipped_programs_are_byte_identical(self):
        programs = self.programs()
        record = {key: fingerprint(text) for key, text in sorted(programs.items())}
        if UPDATE:
            GOLDEN.write_text(json.dumps(record, indent=1, sort_keys=True) + '\n', encoding='utf-8')
            return
        golden = json.loads(GOLDEN.read_text(encoding='utf-8'))
        self.assertEqual(sorted(record), sorted(golden))
        for key, text in programs.items():
            with self.subTest(program=key):
                compile(text, key, 'exec')
                self.assertEqual(record[key], golden[key],
                                 'NAS program changed for %s; generated text follows:\n%s' % (key, text))


if __name__ == '__main__':
    unittest.main()
