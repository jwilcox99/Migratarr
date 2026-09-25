#!/usr/bin/env python3
"""List pending cross-disk TV series; --execute runs the already-approved ones sequentially."""
import argparse
import csv
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys

import execute_cross_tv as m

from executor_manifest import approval_is_current
from runtime_config import get_config, load_config
RUNTIME = get_config()


def pending_series(base, run):
    m.require(re.fullmatch(r'\d{8}T\d{6}Z', run), 'Invalid run ID')
    folder = base / 'manifests' / run
    sums = {}
    for line in (folder / 'SHA256SUMS').read_text().splitlines():
        if line.strip():
            value, name = line.split(maxsplit=1)
            name = name.lstrip('*')
            m.require(name not in sums, 'Duplicate checksum entry')
            sums[name] = value
    blobs = {}
    for name in ('execution_manifest.csv', 'manifest_metadata.json'):
        blobs[name] = (folder / name).read_bytes()
        m.require(m.digest(blobs[name]) == sums.get(name), 'Checksum mismatch: ' + name)
    metadata = json.loads(blobs['manifest_metadata.json'])
    m.require(metadata.get('run_id') == run and metadata.get('manifest_version') == 1
              and metadata.get('snapshot_verified') is True, 'Unverified manifest metadata')
    manifest_hash = m.digest(blobs['execution_manifest.csv'])
    rows = list(csv.DictReader(io.StringIO(blobs['execution_manifest.csv'].decode('utf-8-sig'))))
    ids = [r['execution_id'] for r in rows]
    m.require(len(ids) == len(set(ids)), 'Duplicate execution IDs')
    pending, completed = [], 0
    for row in rows:
        if not (row.get('media_type') == 'TV' and row.get('transfer_type') == 'CROSS_DISK_TRANSFER'
                and row.get('status') == 'READY_FOR_REVIEW' and not row.get('blockers')):
            continue
        execution_id = row['execution_id']
        m.require(re.fullmatch(re.escape(run) + r'-\d{4,}', execution_id), 'Invalid execution ID')
        journal = base / 'execution_logs' / (execution_id + '.jsonl')
        events = [json.loads(line) for line in journal.read_text().splitlines()] if journal.exists() else []
        m.require(all(e.get('execution_id') == execution_id for e in events), 'Journal ID mismatch')
        if events and events[-1]['event'] == 'SUCCESS':
            m.require(events[-1].get('manifest_sha256') == manifest_hash, 'Success manifest hash mismatch')
            completed += 1
            continue
        m.check_journal(journal)
        m.require(not any(e['event'] == 'RECOVERY_STARTED' for e in events), 'Unfinished recovery requires review')
        pending.append(row)
    return sorted(pending, key=lambda r: float(r['size_gb'])), completed, manifest_hash


def split_approved(base, run, pending, manifest_hash):
    """Separate pending rows with a current approval from ones still awaiting approval.

    The batch never approves anything itself: approval is a separate, recorded
    step (approve_execution.py --approve-batch or --approve).
    """
    approved, awaiting = [], []
    for row in pending:
        current = approval_is_current(base, run, row['execution_id'], manifest_hash, m.require)
        (approved if current else awaiting).append(row)
    return approved, awaiting


def run_batch(base, run, execute, runner=subprocess.run, limit=None):
    pending, completed, manifest_hash = pending_series(base, run)
    approved, awaiting = split_approved(base, run, pending, manifest_hash)
    print(f'Completed: {completed}; approved and pending: {len(approved)}; '
          f'awaiting approval: {len(awaiting)} cross-disk series.', flush=True)
    for row in approved:
        print(f'{row["execution_id"]} | {row["title"]} | {row["size_gb"]} GB | APPROVED', flush=True)
    for row in awaiting:
        print(f'{row["execution_id"]} | {row["title"]} | {row["size_gb"]} GB | AWAITING APPROVAL',
              flush=True)
    if awaiting:
        print(f'Approve first (preview, then add --yes): approve_execution.py --run {run} '
              f'--approve-batch --media TV --transfer CROSS_DISK_TRANSFER [--limit N]', flush=True)
    if not execute:
        print('STATUS ONLY: no approvals, copies, updates, or deletions performed.')
        return 0
    if limit is not None:
        m.require(limit > 0, 'Limit must be positive')
        approved = approved[:limit]
        print(f'LIMIT: processing at most {limit} of the approved series this run.', flush=True)
    if not approved:
        print('NOTHING TO EXECUTE: no pending series are approved for this manifest.')
        return 0
    for index, row in enumerate(approved, 1):
        execution_id = row['execution_id']
        # Catch newly completed/uncertain work, manifest edits or revoked approval before each item.
        fresh, _, current_hash = pending_series(base, run)
        m.require(current_hash == manifest_hash, 'Manifest changed during batch')
        if execution_id not in {r['execution_id'] for r in fresh}:
            continue
        if not approval_is_current(base, run, execution_id, manifest_hash, m.require):
            print(f'SKIPPED: approval no longer current for {execution_id}', flush=True)
            continue
        print(f'\n=== {index}/{len(approved)} | {row["title"]} ===', flush=True)
        commands = [
            [sys.executable, str(base / 'execute_cross_tv.py'), execution_id, '--base', str(base)],
            [sys.executable, str(base / 'execute_cross_tv.py'), execution_id, '--base', str(base), '--execute'],
        ]
        for command in commands:
            m.require(load_config(RUNTIME.source_path).as_dict() == RUNTIME.as_dict(),
                      'Runtime configuration changed during batch')
            env = dict(os.environ, MIGRATARR_CONFIG=str(RUNTIME.source_path),
                       MIGRATARR_BASE_PATH=base.as_posix())
            result = runner(command, cwd=base, env=env)
            m.require(result.returncode == 0, 'Command failed for ' + execution_id + '; batch stopped')
        events = [json.loads(line) for line in
                  (base / 'execution_logs' / (execution_id + '.jsonl')).read_text().splitlines()]
        m.require(events[-1]['event'] == 'SUCCESS' and events[-1].get('manifest_sha256') == manifest_hash,
                  'No matching final SUCCESS for ' + execution_id)
    print('BATCH COMPLETE: selected approved cross-disk TV series succeeded or were already complete.')
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', required=True)
    parser.add_argument('--execute', action='store_true', help='Move pending series that are already approved; never approves')
    parser.add_argument('--limit', type=int, default=None,
                         help='Process at most N approved pending series (smallest first) instead of all of them')
    args = parser.parse_args()
    # Both executor subprocesses inherit the same config environment.
    return run_batch(RUNTIME.base_path, args.run, args.execute, limit=args.limit)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit('STOPPED: interrupted. Inspect the active journal before restarting.')
    except Exception as exc:
        sys.exit('STOPPED: ' + str(exc) + '. Keep files and journals intact; reconcile before retrying.')
