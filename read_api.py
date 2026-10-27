"""Compatibility wrapper: the code is in migratarr/read_api.py (since 0.1.0).

Keeps `python3 read_api.py` and `import read_api` working in a git clone. Not installed by pip:
an installed copy uses the `migratarr` command instead.
"""
import importlib
import runpy
import sys

if __name__ == '__main__':
    runpy.run_module('migratarr.read_api', run_name='__main__')  # argv[0] stays this script's name
else:
    sys.modules[__name__] = importlib.import_module('migratarr.read_api')
