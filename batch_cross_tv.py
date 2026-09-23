#!/usr/bin/env python3
"""List pending cross-disk TV series; --execute approves and runs them sequentially."""
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


def run_batch(base, run, execute, runner=subprocess.run):
    pending, completed, manifest_hash = pending_series(base, run)
    print(f'Completed: {completed}; remaining: {len(pending)} cross-disk TV series.', flush=True)
    for row in pending:
        print(f'{row["execution_id"]} | {row["title"]} | {row["size_gb"]} GB', flush=True)
    if not execute:
        print('STATUS ONLY: no approvals, copies, updates, or deletions performed.')
        return 0
    for index, row in enumerate(pending, 1):
        execution_id = row['execution_id']
        # Catch newly completed/uncertain work or manifest edits before each item.
        fresh, _, current_hash = pending_series(base, run)
        m.require(current_hash == manifest_hash, 'Manifest changed during batch')
        if execution_id not in {r['execution_id'] for r in fresh}:
            continue
        print(f'\n=== {index}/{len(pending)} | {row["title"]} ===', flush=True)
        commands = [
            [sys.executable, str(base / 'approve_execution.py'), '--run', run, '--approve', execution_id],
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
    print('BATCH COMPLETE: selected cross-disk TV series succeeded or were already complete.')
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', required=True)
    parser.add_argument('--execute', action='store_true', help='Approve and move all pending eligible series')
    args = parser.parse_args()
    # Approval and both executor subprocesses inherit the same config environment.
    return run_batch(RUNTIME.base_path, args.run, args.execute)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit('STOPPED: interrupted. Inspect the active journal before restarting.')
    except Exception as exc:
        sys.exit('STOPPED: ' + str(exc) + '. Keep files and journals intact; reconcile before retrying.')
