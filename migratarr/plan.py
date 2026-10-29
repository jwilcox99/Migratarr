#!/usr/bin/env python3
"""Run the whole planning pipeline as one command, stopping at the first failure.

    migratarr plan            # preflight, then every planning step
    migratarr plan --check    # preflight only

The dry runs can take a long time on a large library; run it inside tmux.

Steps, in order: dry_run_movies.py, dry_run_tv.py, build_move_plan.py,
audit_overrides.py, snapshot_run.py, build_execution_manifest.py <new run>.
Each runs as its own process with its normal output. Nothing here approves or
moves anything: the result is a new run and manifest with every row
unapproved, and the commands to review it.

Preflight refuses before any step when the runtime config or planner settings
are invalid, a service credential can't be read, any health check fails (the
same checks as `migratarr doctor` and the UI's plan gate; warnings are fine),
or the repository has uncommitted changes (the snapshot records the code it
ran; --allow-dirty overrides that one check).
"""
import argparse
import subprocess
import sys
import time
from pathlib import Path

from migratarr import read_api

from migratarr.paths import CODE as REPO, module_command
STEPS = (
    ('Score movies', ['dry_run_movies.py']),
    ('Score TV', ['dry_run_tv.py']),
    ('Build move plan', ['build_move_plan.py']),
    ('Audit override tags', ['audit_overrides.py']),
    ('Snapshot run', ['snapshot_run.py']),
    ('Build execution manifest', ['build_execution_manifest.py']),  # + the new run ID
)
CREDENTIALS = ('radarr', 'sonarr', 'jellyfin', 'tmdb')


def banner(text):
    print('\n' + '=' * 72 + '\n' + text + '\n' + '=' * 72, flush=True)


def health_failures():
    """Failing health checks: one list for this command, `migratarr doctor` and the UI (decision 2026-10-26)."""
    from migratarr import health, settings
    return [c for c in health.run_checks(settings.load()) if c.status == health.FAIL]


def preflight(repo, allow_dirty):
    """Return (problems, base path); no problems means every step can start."""
    problems = []
    try:
        from migratarr.runtime_config import get_config
        runtime = get_config()
    except Exception as exc:  # any config error stops the pipeline before it starts
        return ['Runtime config: ' + str(exc)], None
    try:
        from migratarr.planner_settings import get_settings
        get_settings()
    except Exception as exc:
        problems.append('Planner settings: ' + str(exc))
    from migratarr.service_keys import SecretError, read_key
    for service in CREDENTIALS:
        try:
            read_key(service, runtime)
        except SecretError as exc:  # messages name the source, never the value
            problems.append(str(exc))
    if not problems:  # the health checks contact every service; only worth it once the basics pass
        for check in health_failures():
            problems.append(f'Health check failed: {check.label}: {check.detail}'
                            + (f' ({check.fix})' if check.fix else ''))
    if not (Path(repo) / '.git').exists():  # an installed copy: the snapshot records version and code hash
        return problems, runtime.base_path
    try:
        status = subprocess.run(['git', 'status', '--porcelain'], cwd=repo, capture_output=True,
                                text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        status = None
        problems.append('Git: cannot read repository status')
    if status and not allow_dirty:
        changed = ', '.join(line[3:] for line in status.splitlines()[:5])
        problems.append(f'Git: uncommitted changes ({changed}); commit them or pass --allow-dirty')
    return problems, runtime.base_path


def run_step(repo, label, args, number, total):
    banner(f'[{number}/{total}] {label}: {" ".join(args)}')
    started = time.monotonic()
    code = subprocess.run(module_command(*args), cwd=repo).returncode
    print(f'[{number}/{total}] {label}: {"OK" if code == 0 else "FAILED (exit %d)" % code}'
          f' in {time.monotonic() - started:.0f}s', flush=True)
    return code


def run_ids(base):
    try:
        return {run['run_id'] for run in read_api.list_runs(base)}
    except OSError:
        return set()


def summary(base, run_id):
    status = read_api.run_status(base, run_id)
    plan = read_api.get_plan(base, run_id)
    blocked = [r for r in plan['rows'] if r.get('status') == 'BLOCKED']
    lines = [f'Run:       {run_id}',
             f'Planned:   {len(plan["rows"])} moves ({len(blocked)} blocked, '
             f'{len(status["rows"])} in the manifest)']
    flow = {}
    for row in status['rows']:
        flow.setdefault(row['transfer_type'], [0, 0.0])
        flow[row['transfer_type']][0] += 1
        flow[row['transfer_type']][1] += float(row['size_gb'] or 0)
    for kind, (count, size) in sorted(flow.items()):
        lines.append(f'           {kind}: {count} ({size:.2f} GB)')
    lines += ['', 'Every row is unapproved and nothing has moved. Next:',
              f'  migratarr status --run {run_id}      # review',
              f'  migratarr ui                                      # or review and approve in the browser',
              f'  migratarr approve --run {run_id} --approve-batch '
              '--media TV --transfer CROSS_DISK_TRANSFER   # preview a batch']
    return '\n'.join(lines)


def main(argv=None, steps=STEPS, repo=REPO):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--check', action='store_true', help='Run the preflight checks only')
    parser.add_argument('--allow-dirty', action='store_true',
                        help='Plan even with uncommitted changes (the snapshot records them)')
    args = parser.parse_args(argv)

    banner('MIGRATARR PLAN: preflight')
    problems, base = preflight(repo, args.allow_dirty)
    for problem in problems:
        print('  - ' + problem)
    if problems:
        print('PREFLIGHT FAILED: no step was run, nothing was written.')
        return 2
    print('Preflight OK: config, planner settings, credentials, health checks and git status.')
    if args.check:
        return 0

    before = run_ids(base)
    new_run = None
    total = len(steps)
    for number, (label, step_args) in enumerate(steps, 1):
        step_args = list(step_args)
        if step_args[0] == 'build_execution_manifest.py':
            step_args.append(new_run)
        if run_step(repo, label, step_args, number, total) != 0:
            banner('MIGRATARR PLAN: STOPPED')
            print(f'Step {number} ({label}) failed; later steps were not run.')
            if new_run:
                print(f'Run {new_run} was created but has no manifest; do not approve from it.')
            else:
                print('No new run was created.')
            return 1
        if step_args[0] == 'snapshot_run.py':
            created = sorted(run_ids(base) - before)
            if len(created) != 1:
                banner('MIGRATARR PLAN: STOPPED')
                print(f'Expected exactly one new run after the snapshot, found {len(created)}.')
                return 1
            new_run = created[0]

    banner('MIGRATARR PLAN: COMPLETE')
    print(summary(base, new_run))
    return 0


if __name__ == '__main__':
    sys.exit(main())
