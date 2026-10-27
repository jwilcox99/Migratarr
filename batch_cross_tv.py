"""Compatibility wrapper: the code is in migratarr/batch_cross_tv.py (since 0.1.0).

Keeps `python3 batch_cross_tv.py` and `import batch_cross_tv` working in a git clone. Not installed by pip:
an installed copy uses the `migratarr` command instead.
"""
import importlib
import runpy
import sys

if __name__ == '__main__':
    runpy.run_module('migratarr.batch_cross_tv', run_name='__main__')  # argv[0] stays this script's name
else:
    sys.modules[__name__] = importlib.import_module('migratarr.batch_cross_tv')
