"""Read-only view of Migratarr's on-disk state, shared by the CLI and a future UI.

Implements docs/ui-data-contract.md. Every function here only reads files
under the base path: nothing is written, renamed, chmod'ed or locked. In
particular execution_logs/executor.lock is never opened, because executors take
it with flock(LOCK_EX | LOCK_NB) and would refuse to start while a reader held it.
Standard library only.
"""
import csv
import hashlib
import io
import json
import re
import time
from pathlib import Path

RUN_ID = re.compile(r'\d{8}T\d{6}Z')
EXECUTION_ID = re.compile(r'(\d{8}T\d{6}Z)-\d{4,}')

TERMINAL_EVENTS = frozenset({'SUCCESS', 'CHECK_ONLY', 'STOPPED'})
# Any of these means media or Arr state may have changed; see ui-data-contract §4.
MUTATION_EVENTS = frozenset({
    'RENAME_INTENT', 'RENAMED', 'COPY_INTENT', 'COPIED', 'RADARR_UPDATE_INTENT',
    'SONARR_UPDATE_INTENT', 'DELETE_INTENT', 'SOURCE_REMOVED', 'RECOVERY_STARTED',
})

# Execution states (ui-data-contract §4), in rule order.
SUCCEEDED = 'SUCCEEDED'
JOURNAL_UNREADABLE = 'JOURNAL_UNREADABLE'
UNFINISHED = 'UNFINISHED'                   # running now, or interrupted without a STOPPED record
NEEDS_RECONCILIATION = 'NEEDS_RECONCILIATION'
STOPPED_SAFE = 'STOPPED_SAFE'
CHECKED = 'CHECKED'
NOT_STARTED = 'NOT_STARTED'

APPROVED, REVOKED, UNAPPROVED = 'APPROVED', 'REVOKED', 'UNAPPROVED'


class ReadError(ValueError):
    """A requested run, manifest or journal does not exist or is malformed."""


def _sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def _checked_run_id(run_id):
    if not RUN_ID.fullmatch(run_id or ''):
        raise ReadError('Invalid run ID: %r' % (run_id,))
    return run_id


def verify_checksums(folder):
    """Return a list of integrity problems for ``folder/SHA256SUMS`` (empty = verified)."""
    folder = Path(folder)
    sums = folder / 'SHA256SUMS'
    if not sums.is_file():
        return ['SHA256SUMS missing']
    problems, seen = [], set()
    for line in sums.read_text(encoding='utf-8').splitlines():
        if not line.strip():
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            problems.append('Malformed checksum line')
            continue
        expected, name = parts[0], parts[1].strip().lstrip('*')
        if name in seen:
            problems.append('Duplicate checksum entry: ' + name)
        seen.add(name)
        path = folder / name
        if '..' in Path(name).parts or not path.is_file():
            problems.append('Missing: ' + name)
        elif _sha256(path) != expected:
            problems.append('Checksum mismatch: ' + name)
    return problems


def _read_csv(path):
    text = Path(path).read_bytes().decode('utf-8-sig')
    return list(csv.DictReader(io.StringIO(text))) if text.strip() else []


def list_runs(base):
    """Snapshot runs, newest first, with counts, integrity and manifest presence."""
    base = Path(base)
    runs = []
    folder = base / 'runs'
    for run_dir in sorted(folder.iterdir() if folder.is_dir() else [], reverse=True):
        if not (run_dir.is_dir() and RUN_ID.fullmatch(run_dir.name)):
            continue
        problems = verify_checksums(run_dir)
        metadata = {}
        try:
            metadata = json.loads((run_dir / 'metadata.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            problems.append('metadata.json unreadable')
        runs.append(dict(
            run_id=run_dir.name,
            created_utc=metadata.get('created_utc'),
            git_commit=metadata.get('git', {}).get('commit'),
            counts=metadata.get('counts', {}),
            integrity_ok=not problems,
            integrity_problems=problems,
            manifest_present=(base / 'manifests' / run_dir.name).is_dir(),
        ))
    return runs


def get_plan(base, run_id):
    """Every planned move of a run, including BLOCKED rows (they never reach the manifest)."""
    run_dir = Path(base) / 'runs' / _checked_run_id(run_id)
    if not run_dir.is_dir():
        raise ReadError('Run not found: ' + run_id)
    problems = verify_checksums(run_dir)
    rows = [] if problems else _read_csv(run_dir / 'move_plan.csv')
    for row in rows:
        row['blocker_codes'] = [c for c in row.get('blockers', '').split(';') if c]
        row['warning_codes'] = [c for c in row.get('warnings', '').split(';') if c]
    return dict(run_id=run_id, integrity_ok=not problems, integrity_problems=problems, rows=rows)


def get_manifest(base, run_id):
    """Executable rows of a run's manifest and the manifest hash approvals are bound to."""
    folder = Path(base) / 'manifests' / _checked_run_id(run_id)
    if not folder.is_dir():
        raise ReadError('Manifest not found: ' + run_id)
    problems = verify_checksums(folder)
    rows, manifest_hash, metadata = [], None, {}
    if not problems:
        manifest_hash = _sha256(folder / 'execution_manifest.csv')
        rows = _read_csv(folder / 'execution_manifest.csv')
        metadata = json.loads((folder / 'manifest_metadata.json').read_text(encoding='utf-8'))
    return dict(run_id=run_id, integrity_ok=not problems, integrity_problems=problems,
                manifest_hash=manifest_hash, counts=metadata.get('counts', {}), rows=rows)


def _load_json_retry(path, attempts=2, delay=0.2):
    # approve_execution.py rewrites this file in place; retry once on a partial read.
    for attempt in range(attempts):
        try:
            return json.loads(Path(path).read_text(encoding='utf-8'))
        except ValueError:
            if attempt + 1 == attempts:
                raise
            time.sleep(delay)


def get_approvals(base, run_id, manifest_hash):
    """Approval state per execution ID, same rule as executor_manifest.approval_is_current()."""
    path = Path(base) / 'approvals' / (_checked_run_id(run_id) + '.json')
    if not path.exists():
        return dict(run_id=run_id, record_present=False, states={}, batches=[])
    try:
        record = _load_json_retry(path)
    except ValueError as exc:
        raise ReadError('Approval record unreadable: ' + str(exc)) from exc
    if not (isinstance(record.get('approved_execution_ids'), list)
            and isinstance(record.get('history'), list) and record.get('run_id') == run_id):
        raise ReadError('Invalid approval record for ' + run_id)
    approved_ids = set(record['approved_execution_ids'])
    latest, batches = {}, {}
    for entry in record['history']:
        latest[entry.get('execution_id')] = entry
        batch = entry.get('batch')
        if entry.get('action') == 'APPROVE' and isinstance(batch, dict):
            group = batches.setdefault(batch.get('utc'), dict(batch, execution_ids=[]))
            group['execution_ids'].append(entry.get('execution_id'))
    states = {}
    for execution_id, entry in latest.items():
        if entry.get('action') == 'REVOKE':
            state = REVOKED
        elif (entry.get('action') == 'APPROVE' and execution_id in approved_ids
              and entry.get('manifest_sha256') == manifest_hash):
            state = APPROVED
        else:
            state = UNAPPROVED
        states[execution_id] = dict(state=state, utc=entry.get('utc'),
                                    batch_utc=(entry.get('batch') or {}).get('utc'))
    return dict(run_id=run_id, record_present=True, states=states,
                batches=sorted(batches.values(), key=lambda b: b.get('utc') or ''))


def journal_path(base, execution_id):
    if not EXECUTION_ID.fullmatch(execution_id or ''):
        raise ReadError('Invalid execution ID: %r' % (execution_id,))
    return Path(base) / 'execution_logs' / (execution_id + '.jsonl')


def events(base, execution_id):
    """Parsed journal events. Raises ReadError for a truncated or malformed journal."""
    path = journal_path(base, execution_id)
    if not path.exists():
        return []
    parsed = []
    for number, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except ValueError as exc:
            raise ReadError('Journal line %d unreadable' % number) from exc
        if not isinstance(event, dict) or not isinstance(event.get('event'), str):
            raise ReadError('Journal line %d is not an event' % number)
        parsed.append(event)
    return parsed


def execution_state(base, execution_id, manifest_hash):
    """Derive one move's execution state from its journal (ui-data-contract §4)."""
    try:
        journal = events(base, execution_id)
    except ReadError as exc:
        return dict(state=JOURNAL_UNREADABLE, detail=str(exc), last_event=None, last_utc=None,
                    recovered=False, attempts=0)
    last = journal[-1] if journal else {}
    info = dict(last_event=last.get('event'), last_utc=last.get('utc'),
                recovered=any(e['event'] == 'RECOVERY_STARTED' for e in journal),
                attempts=sum(e['event'] == 'START' for e in journal), detail=None)
    if not journal:
        return dict(info, state=NOT_STARTED)
    if last['event'] == 'SUCCESS' and last.get('manifest_sha256') == manifest_hash:
        return dict(info, state=SUCCEEDED)
    if last['event'] not in TERMINAL_EVENTS:
        # Journals are silent during long copies/hashes, so a stale file time does not
        # mean the process died; only the host can tell (see ui-data-contract §5, gap 2).
        return dict(info, state=UNFINISHED)
    if any(e['event'] in MUTATION_EVENTS for e in journal) or last['event'] == 'SUCCESS':
        return dict(info, state=NEEDS_RECONCILIATION,
                    detail='SUCCESS bound to a different manifest'
                    if last['event'] == 'SUCCESS' else last.get('reason'))
    if last['event'] == 'STOPPED':
        return dict(info, state=STOPPED_SAFE, detail=last.get('reason'))
    return dict(info, state=CHECKED)


def run_status(base, run_id):
    """Per-row approval + execution state for a run, plus counts and GB per state."""
    manifest = get_manifest(base, run_id)
    result = dict(run_id=run_id, integrity_ok=manifest['integrity_ok'],
                  integrity_problems=manifest['integrity_problems'],
                  manifest_hash=manifest['manifest_hash'], rows=[], totals={})
    if not manifest['integrity_ok']:
        return result
    approvals = get_approvals(base, run_id, manifest['manifest_hash'])
    for row in manifest['rows']:
        execution_id = row['execution_id']
        approval = approvals['states'].get(execution_id, dict(state=UNAPPROVED, utc=None,
                                                               batch_utc=None))
        execution = execution_state(base, execution_id, manifest['manifest_hash'])
        result['rows'].append(dict(
            execution_id=execution_id, media_type=row.get('media_type'), title=row.get('title'),
            transfer_type=row.get('transfer_type'), current=row.get('current'),
            recommended=row.get('recommended'), size_gb=row.get('size_gb'),
            approval=approval['state'], approval_batch_utc=approval['batch_utc'],
            execution=execution['state'], last_event=execution['last_event'],
            last_utc=execution['last_utc'], detail=execution['detail'],
            recovered=execution['recovered']))
        total = result['totals'].setdefault(execution['state'], dict(count=0, size_gb=0.0))
        total['count'] += 1
        try:
            total['size_gb'] = round(total['size_gb'] + float(row.get('size_gb') or 0), 2)
        except ValueError:
            pass
    return result
