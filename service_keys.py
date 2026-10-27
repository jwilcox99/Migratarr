"""Compatibility wrapper: the code is in migratarr/service_keys.py (since 0.1.0).

Keeps `python3 service_keys.py` and `import service_keys` working in a git clone. Not installed by pip:
an installed copy uses the `migratarr` command instead.
"""
import importlib
import runpy
import sys

if __name__ == '__main__':
    runpy.run_module('migratarr.service_keys', run_name='__main__')  # argv[0] stays this script's name
else:
    sys.modules[__name__] = importlib.import_module('migratarr.service_keys')
