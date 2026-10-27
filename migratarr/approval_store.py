"""Approval records (approvals/<run_id>.json): locked, atomic writes shared by the CLI and the UI.

approve_execution.py and migratarr_ui.py both write through here so that:

- every write replaces the file atomically (temp file in the same folder, fsync,
  os.replace), so a crash or a concurrent reader never sees half a record;
- a read-modify-write holds approvals/.<run_id>.lock (flock) for its whole
  duration, so the CLI and the UI can't overwrite each other's entries.

The record format is unchanged: ``approved_execution_ids`` plus an append-only
``history`` of APPROVE/REVOKE entries bound to the manifest hash, which is what
executor_manifest.load_approved_plan() verifies. Standard library only.
"""
import contextlib
import fcntl
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from migratarr import read_api


class ApprovalError(ValueError):
    """The request can't be recorded; nothing was written."""


def record_path(base, run_id):
    return Path(base) / 'approvals' / (read_api._checked_run_id(run_id) + '.json')


def empty_record(run_id):
    return {'run_id': run_id, 'approved_execution_ids': [], 'history': []}


def read_record(base, run_id):
    path = record_path(base, run_id)
    if not path.exists():
        return empty_record(run_id)
    record = json.loads(path.read_text(encoding='utf-8'))
    if not (isinstance(record.get('approved_execution_ids'), list)
            and isinstance(record.get('history'), list) and record.get('run_id') == run_id):
        raise ApprovalError('Invalid approval record for ' + run_id)
    return record


def write_record(base, run_id, record):
    """Atomically replace approvals/<run_id>.json with ``record``."""
    path = record_path(base, run_id)
    path.parent.mkdir(exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.' + path.name + '.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(json.dumps(record, indent=2) + '\n')
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    with contextlib.suppress(OSError):
        dirfd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dirfd)
        finally:
            os.close(dirfd)
    return path


@contextlib.contextmanager
def locked(base, run_id):
    """Hold the run's approval lock (blocking) for a read-modify-write."""
    path = record_path(base, run_id)
    path.parent.mkdir(exist_ok=True)
    with open(path.parent / ('.' + run_id + '.lock'), 'a') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def now():
    return datetime.now(timezone.utc).isoformat()


def eligible_rows(base, run_id):
    """Manifest rows that may be approved now, keyed by execution ID, and the manifest hash.

    Same rule as approve_execution.py --approve-batch: READY_FOR_REVIEW, no
    blockers, executed NO, and the move hasn't already succeeded.
    """
    manifest = read_api.get_manifest(base, run_id)
    if not manifest['integrity_ok']:
        raise ApprovalError('Manifest failed its integrity check: ' + '; '.join(manifest['integrity_problems']))
    rows = {}
    for row in manifest['rows']:
        if (row.get('status') == 'READY_FOR_REVIEW' and not row.get('blockers') and row.get('executed') == 'NO'
                and read_api.execution_state(base, row['execution_id'], manifest['manifest_hash'])['state']
                != read_api.SUCCEEDED):
            rows[row['execution_id']] = row
    return rows, manifest['manifest_hash']


def approve(base, run_id, execution_ids, expected_hash, source='ui'):
    """Approve ``execution_ids`` as one batch bound to ``expected_hash``; return the IDs newly approved.

    Refuses (writes nothing) if the manifest no longer has that hash or any ID is
    not eligible, so what was reviewed is exactly what gets approved.
    """
    ids = list(dict.fromkeys(execution_ids))
    if not ids:
        raise ApprovalError('Nothing selected.')
    with locked(base, run_id):
        rows, manifest_hash = eligible_rows(base, run_id)
        if manifest_hash != expected_hash:
            raise ApprovalError('The manifest changed since this page was opened; review it again.')
        bad = [i for i in ids if i not in rows]
        if bad:
            raise ApprovalError('Not eligible for approval: ' + ', '.join(bad))
        record = read_record(base, run_id)
        approved = set(record['approved_execution_ids'])
        new = [i for i in ids if i not in approved or _latest(record, i).get('manifest_sha256') != manifest_hash
               or _latest(record, i).get('action') != 'APPROVE']
        if not new:
            return []
        utc = now()
        batch = {'utc': utc, 'source': source, 'size': len(new),
                 'media_type': _common(rows, new, 'media_type'),
                 'transfer_type': _common(rows, new, 'transfer_type')}
        for execution_id in new:
            approved.add(execution_id)
            record['history'].append({'action': 'APPROVE', 'execution_id': execution_id, 'utc': utc,
                                      'manifest_sha256': manifest_hash, 'batch': batch})
        record['approved_execution_ids'] = sorted(approved)
        write_record(base, run_id, record)
        return new


def revoke(base, run_id, execution_id):
    """Revoke one approval; return True if it was approved."""
    with locked(base, run_id):
        manifest = read_api.get_manifest(base, run_id)
        if not manifest['integrity_ok']:
            raise ApprovalError('Manifest failed its integrity check.')
        record = read_record(base, run_id)
        if execution_id not in record['approved_execution_ids']:
            return False
        record['approved_execution_ids'] = sorted(set(record['approved_execution_ids']) - {execution_id})
        record['history'].append({'action': 'REVOKE', 'execution_id': execution_id, 'utc': now(),
                                  'manifest_sha256': manifest['manifest_hash']})
        write_record(base, run_id, record)
        return True


def _latest(record, execution_id):
    entries = [e for e in record['history'] if e.get('execution_id') == execution_id]
    return entries[-1] if entries else {}


def _common(rows, ids, key):
    values = {rows[i].get(key) for i in ids}
    return values.pop() if len(values) == 1 else 'MIXED'
