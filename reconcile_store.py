"""Compatibility wrapper: the code is in migratarr/reconcile_store.py (since 0.1.0).

Keeps `python3 reconcile_store.py` and `import reconcile_store` working in a git clone. Not installed by pip:
an installed copy uses the `migratarr` command instead.
"""
import importlib
import runpy
import sys

if __name__ == '__main__':
    runpy.run_module('migratarr.reconcile_store', run_name='__main__')  # argv[0] stays this script's name
else:
    sys.modules[__name__] = importlib.import_module('migratarr.reconcile_store')
