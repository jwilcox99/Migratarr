"""Compatibility wrapper: the code is in migratarr/arr_files.py (since 0.1.0).

Keeps `python3 arr_files.py` and `import arr_files` working in a git clone. Not installed by pip:
an installed copy uses the `migratarr` command instead.
"""
import importlib
import runpy
import sys

if __name__ == '__main__':
    runpy.run_module('migratarr.arr_files', run_name='__main__')  # argv[0] stays this script's name
else:
    sys.modules[__name__] = importlib.import_module('migratarr.arr_files')
