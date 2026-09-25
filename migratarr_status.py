#!/usr/bin/env python3
"""Show runs, per-move status and execution timelines. Read-only: never approves,
moves, writes or locks anything (see read_api.py and docs/ui-data-contract.md)."""
import argparse
import json
from pathlib import Path
import sys

import read_api


def default_base():
    from runtime_config import get_config
    return get_config().base_path


def show_runs(base, as_json):
    runs = read_api.list_runs(base)
    if as_json:
        return runs
    if not runs:
        print('No runs found.')
    for run in runs:
        flag = 'OK' if run['integrity_ok'] else 'INTEGRITY FAILED'
        print(f"{run['run_id']} | planned {run['counts'].get('planned_moves', '?')} | "
              f"manifest {'yes' if run['manifest_present'] else 'no'} | {flag}")
    return None


def show_run(base, run_id, as_json):
    status = read_api.run_status(base, run_id)
    if as_json:
        return status
    print(f'Run: {run_id}')
    if not status['integrity_ok']:
        print('INTEGRITY FAILED: ' + '; '.join(status['integrity_problems']))
        return None
    for row in status['rows']:
        note = f" ({row['detail']})" if row['detail'] else ''
        print(f"{row['execution_id']} | {row['media_type']} | {row['title']} | "
              f"{row['current']} -> {row['recommended']} | {row['size_gb']} GB | "
              f"{row['approval']} | {row['execution']}{note}")
    print()
    for state, total in sorted(status['totals'].items()):
        print(f"{state}: {total['count']} ({total['size_gb']:.2f} GB)")
    return None


def show_execution(base, execution_id, as_json):
    events = read_api.events(base, execution_id)
    if as_json:
        return events
    if not events:
        print('No journal for ' + execution_id)
    for event in events:
        extra = {k: v for k, v in event.items()
                 if k not in ('utc', 'execution_id', 'event', 'inventory', 'receipt',
                              'episode_files', 'episode_associations')}
        print(f"{event.get('utc', '')} {event['event']} {json.dumps(extra) if extra else ''}".rstrip())
    return None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', type=Path, help='Migratarr base path (default: runtime.json base_path)')
    target = parser.add_mutually_exclusive_group()
    target.add_argument('--run', help='Show every manifest row of this run with its status')
    target.add_argument('--execution', help="Show one move's journal timeline")
    parser.add_argument('--json', action='store_true', help='Print machine-readable JSON')
    args = parser.parse_args(argv)
    base = args.base or default_base()
    try:
        if args.run:
            data = show_run(base, args.run, args.json)
        elif args.execution:
            data = show_execution(base, args.execution, args.json)
        else:
            data = show_runs(base, args.json)
    except read_api.ReadError as exc:
        print('ERROR: ' + str(exc), file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(data, indent=2, default=str))
    return 0


if __name__ == '__main__':
    sys.exit(main())
