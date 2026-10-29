#!/usr/bin/env python3
"""Check that Migratarr is set up correctly for this deployment.

    migratarr doctor              # every check
    migratarr doctor --offline    # settings files only; contacts nothing
    migratarr doctor --only services,nas
    migratarr doctor --json       # for scripts
    migratarr doctor --migrate    # move runtime.json version 1 to version 2 first

Checks (migratarr/health.py): settings files, each service (Radarr, Sonarr,
Jellyfin, TMDB) reachable with a working key, Radarr/Sonarr root folders
covering every category folder, the Arr file checks able to run (in the
containers, or on this machine), each disk mounted / writable / above its
reserve, the NAS over SSH and its Python (topology nas_ssh), and group write
on media folders. Read-only except --migrate. Never prints a credential.

Exit status: 0 when nothing failed (warnings allowed), 1 when a check failed.
"""
import argparse
import json
import sys

from migratarr import health, paths, settings

SYMBOL = {health.OK: ' ok ', health.WARN: 'warn', health.FAIL: 'FAIL', health.SKIP: 'skip'}


def render(checks, out):
    group = None
    for check in checks:
        if check.group != group:
            group = check.group
            print('\n' + group.replace('_', ' ').capitalize(), file=out)
        print(f'  [{SYMBOL[check.status]}] {check.label}: {check.detail}', file=out)
        if check.fix and check.status in (health.WARN, health.FAIL):
            print(f'         -> {check.fix}', file=out)
    counts = {s: sum(c.status == s for c in checks) for s in SYMBOL}
    print(f'\n{counts[health.OK]} ok, {counts[health.WARN]} warnings, {counts[health.FAIL]} failed'
          + (f', {counts[health.SKIP]} skipped' if counts[health.SKIP] else ''), file=out)


def main(argv=None, out=sys.stdout):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--offline', action='store_true', help='Check the settings files only')
    parser.add_argument('--only', help='Comma-separated groups: ' + ', '.join(health.GROUPS))
    parser.add_argument('--json', action='store_true', help='Print JSON instead of text')
    parser.add_argument('--migrate', action='store_true',
                        help='Migrate runtime.json version 1 to version 2 (with a backup) before checking')
    args = parser.parse_args(argv)
    groups = health.GROUPS
    if args.only:
        groups = tuple(g.strip() for g in args.only.split(',') if g.strip())
        unknown = set(groups) - set(health.GROUPS)
        if unknown:
            parser.error('unknown groups: ' + ', '.join(sorted(unknown)))
    if args.offline:
        groups = ('config',)
    migration = None
    if args.migrate:
        migration = settings.migrate_if_needed(log=None) or 'runtime.json is already version 2'
    report = settings.load()
    checks = health.run_checks(report, groups=groups)
    if args.json:
        json.dump(dict(ok=health.worst(checks) != health.FAIL, migration=migration,
                       data_folder=str(paths.home()), data_folder_source=paths.home_source(),
                       settings=report.as_dict(), checks=[c.as_dict() for c in checks]), out, indent=2)
        out.write('\n')
    else:
        if migration:
            print(migration, file=out)
        topology = report.topology or 'unknown'
        print(f'Migratarr doctor: topology {topology}; settings from {report.files.runtime}', file=out)
        source = paths.home_source()
        print(f'Data folder: {paths.home()} (from {source}'
              + ('' if source == 'MIGRATARR_HOME' else '; set MIGRATARR_HOME to use another') + ')', file=out)
        if report.missing:
            print('Not set up yet: run "migratarr ui" and open it in a browser to set up.', file=out)
        render(checks, out)
    return 1 if health.worst(checks) == health.FAIL else 0


if __name__ == '__main__':
    sys.exit(main())
