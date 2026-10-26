"""Reconciliation records: "I checked this move by hand, and here is what I found."

A move whose journal shows media or Arr state may have changed
(NEEDS_RECONCILIATION), or that ended with no final event and nothing running
(UNFINISHED), stays flagged until someone looks. Two real cases on
2026-10-26: -0004 (delete refused, quarantine removed by hand) and -0031 (NAS
copy ended silently, staging folder removed by hand). This records that look:

    execution_logs/reconciled/<execution_id>.json
    {"execution_id": ..., "history": [{"utc", "outcome", "note", "source",
                                       "journal_sha256", "journal_events", "state_before"}]}

The latest entry counts only while the journal is byte-for-byte the one it
was written against (journal_sha256): any later attempt makes the move
"needs attention" again. read_api shows a matching record as RECONCILED, and
move jobs never run a RECONCILED row (it isn't in RUNNABLE_STATES); the next
plan re-plans the title from what is actually on disk.

Nothing here changes media, Arr, journals or approvals. Writes are atomic and
refused while a UI job or an executor is running. Standard library only.
"""
import contextlib
import fcntl
import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import read_api

OUTCOMES = {
    'moved': 'The move completed by hand: media is at the destination and Radarr/Sonarr point there',
    'not_moved': 'Nothing moved: media is at the source and Radarr/Sonarr still point there',
    'other': 'Something else; the note says what',
}
RECONCILABLE = (read_api.NEEDS_RECONCILIATION, read_api.UNFINISHED)
MAX_NOTE = 2000


class ReconcileError(ValueError):
    """Not recorded; nothing was written."""


def record_path(base, execution_id):
    read_api.journal_path(base, execution_id)  # validates the ID
    return Path(base) / 'execution_logs' / 'reconciled' / (execution_id + '.json')


def journal_digest(base, execution_id):
    path = read_api.journal_path(base, execution_id)
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except FileNotFoundError:
        return None


def read_record(base, execution_id):
    path = record_path(base, execution_id)
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return None
    except ValueError:
        return None  # an unreadable record never hides a flagged move
    if not isinstance(data, dict) or not isinstance(data.get('history'), list) or not data['history']:
        return None
    return data


def current(base, execution_id):
    """The latest entry if it still matches the journal, else None."""
    data = read_record(base, execution_id)
    if data is None:
        return None
    entry = data['history'][-1]
    if not isinstance(entry, dict) or entry.get('journal_sha256') != journal_digest(base, execution_id):
        return None
    return entry


def record(base, execution_id, outcome, note, *, manifest_hash, source='cli', now=None):
    """Record a reconciliation for one move; returns the entry written."""
    if outcome not in OUTCOMES:
        raise ReconcileError('Outcome must be one of: ' + ', '.join(OUTCOMES))
    note = (note or '').strip()
    if not note:
        raise ReconcileError('Say what you checked and found; the note is the record')
    if len(note) > MAX_NOTE or any(ord(c) < 32 and c not in '\n\t' for c in note):
        raise ReconcileError(f'The note must be plain text up to {MAX_NOTE} characters')
    path = record_path(base, execution_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    from migratarr.settings.store import SettingsBusy, hold_locks
    with open(path.parent / '.lock', 'a') as lock, contextlib.ExitStack() as held:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            # Hold the job and executor locks, so no attempt starts while this is written.
            held.enter_context(hold_locks(base))
        except SettingsBusy as exc:
            raise ReconcileError(str(exc).replace('Settings were not changed', 'Not recorded')) from None
        state = read_api.execution_state(base, execution_id, manifest_hash)
        if state['state'] not in RECONCILABLE:
            raise ReconcileError(f'This move is {state["state"]}; only moves that need attention or ended '
                                 'without a final event can be marked reconciled')
        digest = journal_digest(base, execution_id)
        entry = dict(utc=(now or datetime.now(timezone.utc)).isoformat(), outcome=outcome, note=note,
                     source=source, state_before=state['state'], journal_sha256=digest,
                     journal_events=len(read_api.events(base, execution_id)))
        data = read_record(base, execution_id) or dict(execution_id=execution_id, history=[])
        data['history'].append(entry)
        fd, tmp = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as handle:
                json.dump(data, handle, indent=2)
                handle.write('\n')
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
        return entry
