"""Shared immutable manifest and execution-approval verification."""
import csv
import hashlib
import io
import json
import re


REQUIRED_COLUMNS = {
    'execution_id', 'media_type', 'transfer_type', 'status', 'blockers',
    'executed', 'current', 'recommended', 'source_path', 'target_path',
    'source_disk', 'target_disk',
}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def load_approved_plan(base, execution_id, media_type, transfer_type, require):
    """Return one immutable, approved manifest row or call ``require(False)``.

    Executors supply their existing ``require`` function so refusal type and
    messages remain part of each executor's public safety contract.
    """
    require(re.fullmatch(r'\d{8}T\d{6}Z-\d{4,}', execution_id), 'Invalid execution ID')
    run = execution_id.rsplit('-', 1)[0]
    folder = base / 'manifests' / run
    sums = {}
    for line in (folder / 'SHA256SUMS').read_text().splitlines():
        if not line.strip():
            continue
        value, name = line.split(maxsplit=1)
        name = name.lstrip('*')
        require(name not in sums, 'Duplicate checksum entry')
        sums[name] = value

    blobs = {}
    for name in ('execution_manifest.csv', 'manifest_metadata.json'):
        blobs[name] = (folder / name).read_bytes()
        require(digest(blobs[name]) == sums.get(name), 'Checksum mismatch: ' + name)

    metadata = json.loads(blobs['manifest_metadata.json'])
    require(metadata.get('manifest_version') == 1 and metadata.get('run_id') == run
            and metadata.get('snapshot_verified') is True,
            'Manifest metadata does not identify a verified snapshot')
    raw = blobs['execution_manifest.csv']
    reader = csv.DictReader(io.StringIO(raw.decode('utf-8-sig')))
    headers = reader.fieldnames or []
    require(REQUIRED_COLUMNS <= set(headers) and len(headers) == len(set(headers)),
            'Invalid manifest columns')
    rows = list(reader)
    require(all(None not in row and all(value is not None for value in row.values())
                for row in rows), 'Malformed manifest row')
    ids = [row['execution_id'] for row in rows]
    require(len(ids) == len(set(ids)), 'Duplicate execution IDs')
    selected = [row for row in rows if row['execution_id'] == execution_id]
    require(len(selected) == 1, 'Execution ID absent from frozen manifest')
    row = selected[0]

    approvals = json.loads((base / 'approvals' / (run + '.json')).read_bytes())
    require(isinstance(approvals.get('approved_execution_ids'), list)
            and isinstance(approvals.get('history'), list), 'Invalid approval record')
    require(approvals.get('run_id') == run, 'Approval run mismatch')
    require(execution_id in approvals.get('approved_execution_ids', []), 'Row is unapproved')
    history = [event for event in approvals['history']
               if event.get('execution_id') == execution_id]
    require(history and history[-1].get('action') == 'APPROVE'
            and history[-1].get('manifest_sha256') == digest(raw),
            'Latest approval does not approve this exact manifest hash')
    require(row.get('media_type') == media_type, 'Only ' + media_type + ' is supported')
    require(row.get('transfer_type') == transfer_type, 'Only ' + transfer_type + ' is supported')
    require(row.get('status') == 'READY_FOR_REVIEW' and not row.get('blockers'), 'Row is blocked')
    require(row.get('executed') == 'NO', 'Manifest row already executed or invalid')
    return row, digest(raw)


def approval_is_current(base, run, execution_id, manifest_hash, require):
    """True when ``execution_id`` holds a current approval for this exact manifest hash.

    Mirrors the approval half of ``load_approved_plan`` (approved list plus a
    latest APPROVE history entry bound to ``manifest_hash``) so batch runners
    can tell approved rows from ones awaiting approval without refusing. A
    missing approval file means nothing is approved; a malformed one refuses.
    """
    path = base / 'approvals' / (run + '.json')
    if not path.exists():
        return False
    approvals = json.loads(path.read_bytes())
    require(isinstance(approvals.get('approved_execution_ids'), list)
            and isinstance(approvals.get('history'), list), 'Invalid approval record')
    require(approvals.get('run_id') == run, 'Approval run mismatch')
    if execution_id not in approvals['approved_execution_ids']:
        return False
    history = [event for event in approvals['history']
               if event.get('execution_id') == execution_id]
    return bool(history) and history[-1].get('action') == 'APPROVE' \
        and history[-1].get('manifest_sha256') == manifest_hash
