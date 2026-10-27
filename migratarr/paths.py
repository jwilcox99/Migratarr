"""Where Migratarr's code is, and where its data goes (decision 2026-10-27).

    data folder = $MIGRATARR_HOME
                  else the git clone the code runs from (a clone keeps working exactly as before)
                  else $XDG_DATA_HOME/migratarr, i.e. ~/.local/share/migratarr (a pip install)

The data folder holds config/ (runtime.json, storage-targets.json, planner.json,
backups/). Runs, manifests, approvals, jobs and logs go under runtime.json's
base_path, which the setup wizard sets to the data folder for a new install.
Pasted keys stay in their own owner-only folder (migratarr.settings.keys).

Pipeline steps and executors are started as `python -m <module>` with the code
folder as the working directory, so the same command works from a clone and
from site-packages. Standard library only; nothing here writes.
"""
import hashlib
import os
from pathlib import Path
import sys

PACKAGE = Path(__file__).resolve().parent  # migratarr/: every module (since 0.1.0)
CODE = PACKAGE.parent  # the clone, or site-packages: the working directory for `python -m migratarr.<x>`
# Old top-level script names whose module dropped the "migratarr_" prefix in the package.
RENAMED = {'migratarr_cli': 'cli', 'migratarr_doctor': 'doctor', 'migratarr_jobs': 'jobs', 'migratarr_plan': 'plan',
           'migratarr_reconcile': 'reconcile', 'migratarr_status': 'status', 'migratarr_ui': 'ui'}


def is_clone(code=None):
    return ((code or CODE) / '.git').exists()


def home(environ=None):
    env = os.environ if environ is None else environ
    if env.get('MIGRATARR_HOME'):
        return Path(env['MIGRATARR_HOME']).expanduser()
    if is_clone():
        return CODE
    return Path(env.get('XDG_DATA_HOME') or '~/.local/share').expanduser() / 'migratarr'


def home_source(environ=None):
    """Why the data folder is where it is, for `migratarr doctor`."""
    env = os.environ if environ is None else environ
    if env.get('MIGRATARR_HOME'):
        return 'MIGRATARR_HOME'
    return 'the git clone' if is_clone() else 'default for an installed copy'


def config_dir(environ=None):
    return home(environ) / 'config'


def module_name(script):
    """'execute_cross_tv.py' -> 'migratarr.execute_cross_tv'; 'migratarr_jobs.py' -> 'migratarr.jobs'."""
    stem = Path(script).stem
    return 'migratarr.' + RENAMED.get(stem, stem)


def module_command(script, *args, python=None):
    """The command that runs one of Migratarr's scripts as a module: works from a clone and when installed."""
    return [python or sys.executable, '-m', module_name(script), *map(str, args)]


def version():
    from migratarr.cli import __version__
    return __version__


def code_digest(names, code=None):
    """sha256 over the named modules' bytes (name and content), for run snapshots."""
    h = hashlib.sha256()
    for name in sorted(names):
        path = (code or PACKAGE) / name
        h.update(name.encode() + b'\0')
        h.update(path.read_bytes() if path.exists() else b'<missing>')
        h.update(b'\0')
    return h.hexdigest()
