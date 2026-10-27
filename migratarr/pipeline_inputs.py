"""Refuse to plan or snapshot from pipeline files left over from an earlier session.

The pipeline scripts read each other's CSVs from the base folder. If a step
fails (for example a dry run stopping on a missing TMDB token), the next step
would otherwise read the previous run's file and produce a plan that looks
valid but describes an old library state.

Set MIGRATARR_ALLOW_STALE_INPUTS=1 to override, e.g. to rebuild a plan from
deliberately kept dry-run files. The override is printed, never silent.
"""
import os
import sys
import time
from datetime import datetime, timezone

MAX_INPUT_AGE_HOURS = 12
OVERRIDE_VARIABLE = 'MIGRATARR_ALLOW_STALE_INPUTS'


def _stamp(mtime):
    return datetime.fromtimestamp(mtime, timezone.utc).strftime('%Y-%m-%d %H:%M UTC')


def input_problems(paths, newer_than=(), max_age_hours=MAX_INPUT_AGE_HOURS, now=None):
    """Problems with `paths`: missing, older than `max_age_hours`, or (for a path not
    itself in `newer_than`) written before any file in `newer_than`."""
    now = time.time() if now is None else now
    problems = []
    mtimes = {}
    for path in dict.fromkeys(list(paths) + list(newer_than)):
        try:
            mtimes[path] = path.stat().st_mtime
        except FileNotFoundError:
            problems.append(f'{path.name} is missing')
    for path in paths:
        if path in mtimes and now - mtimes[path] > max_age_hours * 3600:
            problems.append(f'{path.name} is {(now - mtimes[path]) / 3600:.1f} h old '
                            f'(written {_stamp(mtimes[path])}; limit {max_age_hours} h)')
    for path in paths:
        if path in newer_than:
            continue  # the order applies to outputs, not between the inputs themselves
        for earlier in newer_than:
            if path in mtimes and earlier in mtimes and mtimes[path] < mtimes[earlier]:
                problems.append(f'{path.name} ({_stamp(mtimes[path])}) is older than '
                                f'{earlier.name} ({_stamp(mtimes[earlier])}); rebuild it')
    return problems


def require_fresh_inputs(step, paths, newer_than=(), max_age_hours=MAX_INPUT_AGE_HOURS,
                         environ=None, now=None):
    """Exit with a clear message unless every input is present, recent and in order."""
    environ = os.environ if environ is None else environ
    problems = input_problems(paths, newer_than, max_age_hours, now)
    if not problems:
        return
    if environ.get(OVERRIDE_VARIABLE) == '1':
        print(f'WARNING: {step}: using stale inputs because {OVERRIDE_VARIABLE}=1:', file=sys.stderr)
        for problem in problems:
            print('  - ' + problem, file=sys.stderr)
        return
    lines = [f'REFUSED: {step} will not use these inputs:']
    lines += ['  - ' + problem for problem in problems]
    lines += ['Re-run the earlier pipeline steps (a failed step leaves the previous file in place).',
              f'To use them anyway, set {OVERRIDE_VARIABLE}=1.']
    raise SystemExit('\n'.join(lines))
