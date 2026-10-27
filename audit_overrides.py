"""Compatibility wrapper: the code is in migratarr/audit_overrides.py (since 0.1.0).

Keeps `python3 audit_overrides.py` and `import audit_overrides` working in a git clone. Not installed by pip:
an installed copy uses the `migratarr` command instead.
"""
import importlib
import runpy
import sys

if __name__ == '__main__':
    runpy.run_module('migratarr.audit_overrides', run_name='__main__')  # argv[0] stays this script's name
else:
    sys.modules[__name__] = importlib.import_module('migratarr.audit_overrides')
