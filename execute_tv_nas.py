"""Compatibility wrapper: the code is in migratarr/execute_tv_nas.py (since 0.1.0).

Keeps `python3 execute_tv_nas.py` and `import execute_tv_nas` working in a git clone. Not installed by pip:
an installed copy uses the `migratarr` command instead.
"""
import importlib
import runpy
import sys

if __name__ == '__main__':
    runpy.run_module('migratarr.execute_tv_nas', run_name='__main__')  # argv[0] stays this script's name
else:
    sys.modules[__name__] = importlib.import_module('migratarr.execute_tv_nas')
