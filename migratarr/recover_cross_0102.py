#!/usr/bin/env python3
"""Recover Moneyball's published copy after the NAS copy response timed out."""
import argparse
from datetime import datetime, timezone
import json
import inspect
import os
from pathlib import Path, PurePosixPath
import sys
import shlex

import execute_cross_movie as m

from runtime_config import get_config
from arr_files import host_path, host_sha256
from planner_settings import get_settings
RUNTIME = get_config()
OVERRIDES = get_settings().overrides

EXECUTION_ID = '20260915T192959Z-0102'
OPERATION_TIMEOUT = 7200


def load_recovery(base):
    row, manifest_hash = m.load_plan(base, EXECUTION_ID)
    journal = base / 'execution_logs' / (EXECUTION_ID + '.jsonl')
    events = [json.loads(line) for line in journal.read_text().splitlines()]
    m.require(events and all(e.get('execution_id') == EXECUTION_ID for e in events),
              'Journal execution ID mismatch')
    m.require(events[-1].get('event') == 'STOPPED'
              and events[-1].get('error_type') == 'TimeoutExpired'
              and events[-1].get('reason', '').endswith('timed out after 1800 seconds'),
              'Journal is not at the diagnosed copy timeout; reconcile manually')
    for kind in ('COPY_INTENT',):
        m.require(sum(e['event'] == kind for e in events) == 1,
                  'Expected exactly one ' + kind)
    forbidden = {'COPIED', 'RADARR_UPDATE_INTENT', 'DELETE_INTENT', 'SOURCE_REMOVED', 'SUCCESS', 'RECOVERY_STARTED'}
    m.require(not any(e['event'] in forbidden for e in events), 'A later live attempt already exists')
    m.require([e['event'] for e in events[-2:]] == ['COPY_INTENT', 'STOPPED'],
              'Unexpected mutation sequence')
    plan = next(e for e in reversed(events) if e['event'] == 'PREFLIGHT_OK')
    expected = events[-2]['inventory']
    receipt = None
    m.require(plan['manifest_sha256'] == manifest_hash, 'Manifest differs from copied plan')
    src, dst, logical_src, logical_dst = m.paths(row)
    m.require(plan['source'] == str(src) and plan['destination'] == str(dst)
              and plan['logical_source'] == logical_src and plan['logical_destination'] == logical_dst,
              'Manifest and journal paths disagree')
    return row, manifest_hash, plan, expected, receipt, src, dst, logical_src, logical_dst


def check_radarr(radarr, plan, row, expected, logical_src, logical_dst):
    movie = radarr.api(f"movie/{plan['radarr_id']}")
    file_record = dict(movie.get('movieFile', {}))
    relative = file_record.get('relativePath', '')
    m.require(file_record.get('id') == plan['movie_file_id']
              and relative in expected and len(expected[relative]) == 4
              and file_record.get('size') == expected[relative][0], 'Radarr movie-file identity changed')
    m.verify_movie(radarr, plan['radarr_id'], logical_src,
                   str(PurePosixPath(logical_src).parent), file_record)
    m.require(not any(x.get('path') == logical_dst for x in radarr.api('movie')),
              'Radarr destination is already assigned')
    root = str(PurePosixPath(logical_dst).parent)
    m.require(any(x.get('path') == root and x.get('accessible') is True
                  for x in radarr.api('rootfolder')), 'Destination root inaccessible')
    labels = {t['id']: t['label'].lower() for t in radarr.api('tag')}
    tags = {labels.get(t, '') for t in movie.get('tags', [])}
    m.require(not OVERRIDES.locked(tags), 'Movie is now locked')
    m.require(OVERRIDES.agrees(tags, row['recommended']), 'Current override conflicts')
    return movie, file_record, root


def recover(base, live, radarr, transport, log):
    row, digest, plan, expected, receipt, src, dst, logical_src, logical_dst = load_recovery(base)
    for p in (src, dst):
        m.canonical_existing(p)
        m.require(p.is_dir(), 'Expected both source and destination directories')
    for p in (src.parent / ('.migratarr-delete-' + EXECUTION_ID),
              dst.parent / ('.migratarr-stage-' + EXECUTION_ID)):
        m.require(not os.path.lexists(p), 'Unexpected work directory: ' + str(p))
    m.progress('Rechecking existing source and destination; no copy will be performed')
    m.require(m.inventory(src) == expected, 'Original source inventory changed')
    destination = m.verify_destination(dst, expected)
    movie, media, root = check_radarr(radarr, plan, row, expected, logical_src, logical_dst)
    radarr.verify_file(logical_dst + '/' + media['relativePath'], expected[media['relativePath']][1])
    # Hash both NAS-local trees under the executor's NAS lock and reconstruct
    # an identity receipt from observed state, not from the timed-out response.
    receipt = transport.call('receipt', src, dst, expected, EXECUTION_ID)
    m.require(receipt.get('result') == 'OK' and receipt.get('operation') == 'copy'
              and receipt.get('reconstructed') is True
              and len(receipt['source_id']) == 2
              and m.content_only(receipt['source_inventory']) == m.content_only(expected),
              'NAS recovery receipt is invalid')
    if not live:
        return 'CHECK_ONLY'
    m.require(m.load_plan(base, EXECUTION_ID) == (row, digest), 'Approval or manifest changed')
    m.require(m.file_metadata(src) == m.inventory_metadata(expected), 'Source changed during verification')
    m.require(m.file_metadata(dst) == m.inventory_metadata(destination), 'Destination changed during verification')
    # Fetch fresh settings after the lengthy checks. Preserve these settings in the PUT.
    movie, current_media, root = check_radarr(radarr, plan, row, expected, logical_src, logical_dst)
    m.require(all(current_media.get(k) == media.get(k) for k in ('id', 'relativePath', 'size')),
              'Media changed during recovery')
    log('RECOVERY_STARTED', reason='Reconcile published copy after NAS timeout', receipt=receipt)
    log('RADARR_UPDATE_INTENT', recovery=True)
    radarr.api(f"movie/{plan['radarr_id']}?moveFiles=false",
               dict(movie, path=logical_dst, rootFolderPath=root))
    after = m.verify_movie(radarr, plan['radarr_id'], logical_dst, root, media)
    m.require(all(after.get(k) == movie.get(k) for k in ('monitored', 'qualityProfileId', 'tags', 'minimumAvailability')),
              'Radarr settings changed; source retained')
    radarr.verify_file(logical_dst + '/' + media['relativePath'], expected[media['relativePath']][1])
    m.require(m.load_plan(base, EXECUTION_ID) == (row, digest), 'Approval changed before deletion')
    m.require(m.file_metadata(dst) == m.inventory_metadata(destination), 'Destination changed before deletion')
    m.verify_movie(radarr, plan['radarr_id'], logical_dst, root, media)
    log('DELETE_INTENT', receipt=receipt, recovery=True)
    transport.call('delete', src, dst, expected, EXECUTION_ID, receipt)
    log('SOURCE_REMOVED', recovery=True)
    m.wait_source_absent(src)
    m.require(m.file_metadata(dst) == m.inventory_metadata(destination), 'Final destination metadata differs')
    m.verify_movie(radarr, plan['radarr_id'], logical_dst, root, media)
    radarr.visible(logical_dst + '/' + media['relativePath'])
    log('SUCCESS', manifest_sha256=digest, recovery=True)
    return 'SUCCESS'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', type=Path, default=RUNTIME.base_path)
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args()
    m.require(sys.platform.startswith('linux'), 'Run on the media host')
    import fcntl
    logs = args.base / 'execution_logs'
    with (logs / 'executor.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # Validate before opening the journal for append; check-only leaves it unchanged.
        load_recovery(args.base)
        journal = logs / (EXECUTION_ID + '.jsonl')
        with journal.open('a', encoding='utf-8') as stream:
            started = False
            def log(event, **details):
                nonlocal started
                started = True
                stream.write(json.dumps(dict(utc=datetime.now(timezone.utc).isoformat(),
                                             execution_id=EXECUTION_ID, event=event, **details)) + '\n')
                stream.flush()
                os.fsync(stream.fileno())
                m.progress(event.replace('_', ' '))
            try:
                with RecoveryTransport() as transport:
                    result = recover(args.base, args.execute, RecoveryRadarr(), transport, log)
                print(result + ': ' + EXECUTION_ID)
            except BaseException as exc:
                if started:
                    log('STOPPED', error_type=type(exc).__name__, reason=str(exc))
                print('STOPPED: ' + str(exc), file=sys.stderr)
                return 1
    return 0


def nas_receipt(data):
    # This function runs on the NAS, where the core helper functions are globals.
    require(data['execution_id'] == '20260915T192959Z-0102' and data['operation'] == 'receipt',
            'Unexpected recovery ID or operation')
    src, dst = Path(data['source']), Path(data['destination'])
    require(str(src) == '/volume3/media04/Movies/Library/Moneyball (2011)'
            and str(dst) == '/volume4/media01/Movies/Common/Moneyball (2011)',
            'Unexpected Moneyball paths')
    for p in (src, dst):
        canonical_existing(p)
        require(p.is_dir(), 'Expected both NAS directories')
    require(src.stat().st_dev != dst.stat().st_dev, 'Expected distinct NAS filesystems')
    for p in (src.parent / '.migratarr-delete-20260915T192959Z-0102',
              dst.parent / '.migratarr-stage-20260915T192959Z-0102'):
        require(not os.path.lexists(p), 'Unexpected work directory')
    source_id = [src.stat().st_dev, src.stat().st_ino]
    source = inventory(src)
    destination = inventory(dst)
    require(content_only(source) == content_only(data['inventory']) == content_only(destination),
            'NAS content mismatch; source retained')
    require(file_metadata(src) == inventory_metadata(source)
            and [src.stat().st_dev, src.stat().st_ino] == source_id,
            'NAS source changed during verification')
    sync_parents(src, dst)
    return dict(result='OK', operation='copy', reconstructed=True,
                source_inventory=source, source_id=source_id)


def receipt_program():
    program = m.remote_program()
    marker = 'data = json.load(sys.stdin)'
    dispatch = 'print(json.dumps(nas_operation(data)))'
    m.require(program.count(marker) == 1 and program.count(dispatch) == 1,
              'Installed executor helper differs; no recovery attempted')
    program = program.replace(marker, inspect.getsource(nas_receipt) + '\n' + marker)
    return program.replace(dispatch, 'print(json.dumps(nas_receipt(data)))')


class RecoveryTransport(m.NasTransport):
    def call(self, operation, src, dst, before, execution_id, receipt=None):
        m.require(operation in {'receipt', 'delete'} and execution_id == EXECUTION_ID,
                  'Recovery cannot copy or handle other execution IDs')
        payload = dict(operation=operation, source=m.remote_path(src), destination=m.remote_path(dst),
                       inventory=before, execution_id=execution_id, receipt=receipt)
        program = receipt_program() if operation == 'receipt' else m.remote_program()
        output = m.run_progress(['ssh', *self.options, '-o', 'BatchMode=yes', RUNTIME.ssh_target,
                                 shlex.quote(RUNTIME.remote_python) + ' -c ' + shlex.quote(program)],
                                'NAS recovery ' + operation, input=json.dumps(payload),
                                timeout=OPERATION_TIMEOUT)
        response = json.loads(output)
        m.require(response.get('result') == 'OK'
                  and response.get('operation') == ('copy' if operation == 'receipt' else 'delete'),
                  'Unexpected NAS response; reconcile manually')
        return response


class RecoveryRadarr(m.Radarr):
    def verify_file(self, path, expected_hash):
        self.visible(path)
        check = RUNTIME.arr_file_checks['radarr']
        if check['mode'] == 'host':
            digest = host_sha256(host_path(check, m.TARGETS.arr_root, path))
            m.require(digest == expected_hash, 'Radarr-visible content mismatch')
            return
        output = m.run_progress(['docker', 'exec', check['container'], 'sha256sum', '--', path],
                                'Radarr recovery content verification', timeout=OPERATION_TIMEOUT)
        m.require(output.split()[0] == expected_hash, 'Radarr-visible content mismatch')


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (m.Refused, OSError) as exc:
        sys.exit('STOPPED: ' + str(exc))
