#!/usr/bin/env python3
"""The migratarr command: one entry point for the scripts a user runs.

    migratarr ui [--port 8765] [--read-only]   web UI (setup, settings, plan, confirm, run)
    migratarr plan [--check]                    make a new plan (nothing moves)
    migratarr status [...]                      runs, moves and timelines (read-only)
    migratarr approve ...                       approve moves from the terminal
    migratarr doctor [--offline]                check settings, services and disks
    migratarr reconcile <move> --outcome ...    record that a flagged move was checked by hand
    migratarr demo [--dir FOLDER]               try it on a made-up library, read-only
    migratarr --version

Each subcommand runs the existing script unchanged, with the rest of the
arguments, so `migratarr plan --check` is exactly `python3 migratarr_plan.py --check`.
Installed with `pip install -e .` from a clone (see pyproject.toml).
"""
import importlib.util
import runpy
import sys

__version__ = '0.2.0rc1'

COMMANDS = {  # name: (module, one-line help)
    'ui': ('migratarr.ui', 'web UI (setup, settings, plan, confirm, run)'),
    'plan': ('migratarr.plan', 'make a new plan; nothing moves'),
    'status': ('migratarr.status', 'runs, moves and timelines (read-only)'),
    'approve': ('migratarr.approve_execution', 'approve moves from the terminal'),
    'doctor': ('migratarr.doctor', 'check settings, services and disks'),
    'reconcile': ('migratarr.reconcile', 'record that a flagged move was checked by hand'),
    'demo': ('migratarr.demo', 'try it on a made-up library, read-only (no services needed)'),
    'password': ('migratarr.auth', 'set or change the web UI login'),
}


def usage():
    width = max(map(len, COMMANDS))
    lines = [f'  {name.ljust(width)}  {text}' for name, (_, text) in COMMANDS.items()]
    return ('usage: migratarr <command> [arguments]\n\ncommands:\n' + '\n'.join(lines) +
            '\n\nRun "migratarr <command> --help" for a command\'s own options.\n')


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ('-h', '--help', 'help'):
        sys.stdout.write(usage())
        return 0
    if argv[0] == '--version':
        print('migratarr ' + __version__)
        return 0
    name, rest = argv[0], argv[1:]
    if name not in COMMANDS:
        sys.stderr.write(f'migratarr: unknown command "{name}"\n\n' + usage())
        return 2
    module = COMMANDS[name][0]
    if importlib.util.find_spec(module) is None:
        sys.stderr.write(f'migratarr {name} is not available in this version yet.\n')
        return 2
    saved = sys.argv
    sys.argv = ['migratarr ' + name, *rest]
    try:
        runpy.run_module(module, run_name='__main__')  # argv[0] stays 'migratarr <command>' for --help
    except SystemExit as exc:
        if exc.code is None or isinstance(exc.code, int):
            return exc.code or 0
        sys.stderr.write(str(exc.code) + '\n')  # sys.exit('message'), as Python itself would print it
        return 1
    except Exception as exc:  # settings that are missing or invalid: say so, not a traceback
        from migratarr.planner_settings import SettingsError
        from migratarr.runtime_config import ConfigError
        if not isinstance(exc, (ConfigError, SettingsError)):
            raise
        sys.stderr.write(f'migratarr {name}: {exc}\n'
                         'Set up or fix the settings with "migratarr ui", or check them with "migratarr doctor".\n')
        return 2
    finally:
        sys.argv = saved
    return 0


if __name__ == '__main__':
    sys.exit(main())
