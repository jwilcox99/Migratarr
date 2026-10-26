"""migratarr/engine: shared executor code (unified-execution-core, gate 2).

nas_side.py is shipped to the NAS as source text, so it must stay
standard-library only (D4). Shared functions are bound into each executor so
they resolve that executor's own helpers, exactly as when they were pasted in.
"""
import ast
from contextlib import ExitStack
import importlib
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

from migratarr.engine import nas_side, primitives
from planner_settings import load_settings
from runtime_config import load_config
from storage_targets import load_targets

ROOT = Path(__file__).resolve().parents[1]
EXECUTORS = ('execute_movie_nas', 'execute_tv_nas', 'execute_cross_movie', 'execute_cross_tv')
SHARED_MODULES = (nas_side, primitives)


def top_level_names(path):
    tree = ast.parse(Path(path).read_text(encoding='utf-8'))
    return {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))}


def shared_names(module):
    return {name for name, obj in vars(module).items()
            if isinstance(obj, (types.FunctionType, type)) and obj.__module__ == module.__name__}


class EngineLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        stack = ExitStack()
        config = load_config(ROOT / 'config/examples/runtime.v1.example.json', environ={})
        with patch('runtime_config.get_config', return_value=config), \
                patch('planner_settings.get_settings',
                      return_value=load_settings(ROOT / 'config/planner.example.json')), \
                patch('media_layout.get_targets',
                      return_value=load_targets(ROOT / 'config/storage-targets.example.json')):
            cls.modules = {name: importlib.import_module(name) for name in EXECUTORS}
        stack.close()

    def test_nas_side_imports_only_the_standard_library(self):
        tree = ast.parse((ROOT / 'migratarr/engine/nas_side.py').read_text(encoding='utf-8'))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split('.')[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                self.assertEqual(node.level, 0, 'relative import in nas_side.py')
                imported.add(node.module.split('.')[0])
        self.assertTrue(imported)
        self.assertEqual(imported - set(sys.stdlib_module_names), set())
        # Only imports and definitions at top level: nothing runs when it is shipped or imported.
        for node in tree.body:
            self.assertIsInstance(node, (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.ClassDef, ast.Expr))

    def test_no_executor_keeps_its_own_copy_of_shared_code(self):
        shared = set().union(*(shared_names(m) for m in SHARED_MODULES)) - {'bind'}
        for name in EXECUTORS:
            with self.subTest(executor=name):
                self.assertEqual(top_level_names(ROOT / (name + '.py')) & shared, set())

    def test_shared_functions_are_bound_to_each_executor(self):
        for name, module in self.modules.items():
            for source in SHARED_MODULES:
                for attr in shared_names(source) - {'bind'}:
                    original = getattr(source, attr)
                    if not hasattr(module, attr) or not isinstance(original, types.FunctionType):
                        continue
                    with self.subTest(executor=name, function=attr):
                        bound = getattr(module, attr)
                        self.assertIs(bound.__code__, original.__code__)
                        self.assertIs(bound.__globals__, vars(module))

    def test_refused_is_one_shared_class(self):
        for module in self.modules.values():
            self.assertIs(module.Refused, nas_side.Refused)
            with self.assertRaises(nas_side.Refused):
                module.require(False, 'x')

    def test_bind_resolves_names_in_the_target_namespace(self):
        def helper():
            return marker()  # noqa: F821 - supplied by the namespace it is bound into
        namespace = {'marker': lambda: 'mine'}
        primitives.bind(namespace, helper, nas_side.Refused)
        self.assertEqual(namespace['helper'](), 'mine')
        self.assertIs(namespace['Refused'], nas_side.Refused)
        namespace['marker'] = lambda: 'patched'  # as a test patch of an executor global would
        self.assertEqual(namespace['helper'](), 'patched')


if __name__ == '__main__':
    unittest.main()
