#!/usr/bin/env python3
"""Reconcile only the diagnosed pre-update stop for execution 20260915T192959Z-0116."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path, PurePosixPath
import sys

import execute_cross_movie as m

EXECUTION_ID = '20260915T192959Z-0116'


def load_recovery(base):
    row, manifest_hash = m.load_plan(base, EXECUTION_ID)
    journal = base / 'execution_logs' / (EXECUTION_ID + '.jsonl')
    events = [json.loads(line) for line in journal.read_text().splitlines()]
    m.require(events and all(e.get('execution_id') == EXECUTION_ID for e in events),
              'Journal execution ID mismatch')
    m.require(events[-1].get('event') == 'STOPPED'
              and events[-1].get('reason') == 'Radarr changed before update',
              'Journal is not at the diagnosed pre-update stop; reconcile manually')
    for kind in ('COPY_INTENT', 'COPIED'):
        m.require(sum(e['event'] == kind for e in events) == 1,
                  'Expected exactly one ' + kind)
    forbidden = {'RADARR_UPDATE_INTENT', 'DELETE_INTENT', 'SOURCE_REMOVED', 'SUCCESS', 'RECOVERY_STARTED'}
    m.require(not any(e['event'] in forbidden for e in events), 'A later live attempt already exists')
    m.require([e['event'] for e in events[-3:]] == ['COPY_INTENT', 'COPIED', 'STOPPED'],
              'Unexpected mutation sequence')
    plan = next(e for e in reversed(events) if e['event'] == 'PREFLIGHT_OK')
    expected = events[-3]['inventory']
    receipt = events[-2]['receipt']
    m.require(plan['manifest_sha256'] == manifest_hash, 'Manifest differs from copied plan')
    m.require(receipt.get('result') == 'OK' and receipt.get('operation') == 'copy'
              and m.content_only(receipt['source_inventory']) == m.content_only(expected)
              and len(receipt['source_id']) == 2, 'Invalid original copy receipt')
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
    m.require('migratarr-lock' not in tags, 'Movie is now locked')
    overrides = tags & {'migratarr-common', 'migratarr-rare', 'migratarr-library',
                        'migratarr-archive', 'migratarr-current'}
    m.require(not overrides or overrides == {'migratarr-' + row['recommended'].lower()},
              'Current override conflicts')
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
    if not live:
        return 'CHECK_ONLY'
    m.require(m.load_plan(base, EXECUTION_ID) == (row, digest), 'Approval or manifest changed')
    m.require(m.file_metadata(src) == m.inventory_metadata(expected), 'Source changed during verification')
    m.require(m.file_metadata(dst) == m.inventory_metadata(destination), 'Destination changed during verification')
    # Fetch fresh settings after the lengthy checks. Preserve these settings in the PUT.
    movie, current_media, root = check_radarr(radarr, plan, row, expected, logical_src, logical_dst)
    m.require(all(current_media.get(k) == media.get(k) for k in ('id', 'relativePath', 'size')),
              'Media changed during recovery')
    log('RECOVERY_STARTED', reason='Reconcile verified copy after Radarr permission change')
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
    parser.add_argument('--base', type=Path, default=Path('/opt/media-stack/migratarr'))
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
                if args.execute:
                    with m.NasTransport() as transport:
                        result = recover(args.base, True, m.Radarr(), transport, log)
                else:
                    result = recover(args.base, False, m.Radarr(), None, log)
                print(result + ': ' + EXECUTION_ID)
            except BaseException as exc:
                if started:
                    log('STOPPED', error_type=type(exc).__name__, reason=str(exc))
                print('STOPPED: ' + str(exc), file=sys.stderr)
                return 1
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (m.Refused, OSError) as exc:
        sys.exit('STOPPED: ' + str(exc))
