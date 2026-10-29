#!/usr/bin/env python3
"""Record that a flagged move was checked by hand (reconcile_store.py).

    migratarr reconcile 20261023T212605Z-0031 --outcome not_moved \
        --note "NAS copy ended silently; source intact, Radarr unchanged, staging folder removed by hand"
    migratarr reconcile 20261023T212605Z-0031 --show

Outcomes: moved, not_moved, other. Only moves in NEEDS_RECONCILIATION, or
UNFINISHED with nothing running, can be recorded. Changes no media, Arr
record, journal or approval; the move becomes RECONCILED and is never run
again from this manifest. Any later journal entry undoes the record.
"""
import argparse
import json
from pathlib import Path
import sys

from migratarr import read_api
from migratarr import reconcile_store


def main(argv=None, out=sys.stdout):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('execution_id')
    parser.add_argument('--outcome', choices=sorted(reconcile_store.OUTCOMES))
    parser.add_argument('--note', help='What you checked and found')
    parser.add_argument('--show', action='store_true', help='Print the state and any record; change nothing')
    parser.add_argument('--base', type=Path, help='Migratarr base path (default: runtime.json base_path)')
    args = parser.parse_args(argv)
    if args.base is None:
        from migratarr.runtime_config import get_config
        args.base = get_config().base_path
    match = read_api.EXECUTION_ID.fullmatch(args.execution_id)
    if not match:
        parser.error('invalid execution ID')
    try:
        manifest = read_api.get_manifest(args.base, match.group(1))
        state = read_api.execution_state(args.base, args.execution_id, manifest['manifest_hash'])
        if args.show or not args.outcome:
            if not args.show:
                parser.error('--outcome and --note are required (or use --show)')
            print(json.dumps(dict(state=state, record=reconcile_store.read_record(args.base, args.execution_id)),
                             indent=2), file=out)
            return 0
        entry = reconcile_store.record(args.base, args.execution_id, args.outcome, args.note,
                                       manifest_hash=manifest['manifest_hash'], source='cli')
    except (read_api.ReadError, reconcile_store.ReconcileError) as exc:
        print('Not recorded: ' + str(exc), file=sys.stderr)
        return 1
    print(f'RECONCILED: {args.execution_id} ({entry["outcome"]}; was {entry["state_before"]})', file=out)
    return 0


if __name__ == '__main__':
    sys.exit(main())
