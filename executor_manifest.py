"""Compatibility wrapper: the code is in migratarr/executor_manifest.py (since 0.1.0).

Keeps `python3 executor_manifest.py` and `import executor_manifest` working in a git clone. Not installed by pip:
an installed copy uses the `migratarr` command instead.
"""
import importlib
import runpy
import sys

if __name__ == '__main__':
    runpy.run_module('migratarr.executor_manifest', run_name='__main__')  # argv[0] stays this script's name
else:
    sys.modules[__name__] = importlib.import_module('migratarr.executor_manifest')
