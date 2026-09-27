#!/usr/bin/env python3
"""Finish a cross-disk move whose NAS copy was published but whose NAS call then timed out.

Only for this exact, diagnosed state (incidents 0102 and 0014 in docs/storage-targets.md):

- the journal ends ``START(live) -> PREFLIGHT_OK -> COPY_INTENT -> STOPPED(TimeoutExpired)``,
  with no later mutation or recovery event;
- the source still hash-matches the COPY_INTENT inventory, and the published
  destination hash-matches it too (checked locally and again on the NAS);
- no staging or quarantine folder is left, and Radarr/Sonarr still point at the
  source with the same file(s) (TV: the same episode files and associations).

Check-only by default: it re-verifies everything and writes nothing. With
--execute it journals RECOVERY_STARTED, then performs the executor's own
remaining steps (Arr path update, verification, verified NAS deletion of the
source) and ends with SUCCESS bound to the manifest hash.
"""
import argparse
from datetime import datetime, timezone
import inspect
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import sys

import execute_cross_movie
import execute_cross_tv
from executor_command import NAS_IDLE_TIMEOUT
import read_api

LATER_EVENTS = frozenset({'COPIED', 'RADARR_UPDATE_INTENT', 'SONARR_UPDATE_INTENT', 'DELETE_INTENT',
                          'SOURCE_REMOVED', 'SUCCESS', 'RECOVERY_STARTED'})
MOVIE_SETTINGS = ('monitored', 'qualityProfileId', 'tags', 'minimumAvailability')
SERIES_SETTINGS = ('seriesType', 'seasonFolder', 'monitored', 'qualityProfileId', 'tags')


def executor_for(base, execution_id):
    """The cross-disk executor module for this row's media type."""
    match = read_api.EXECUTION_ID.fullmatch(execution_id or '')
    if not match:
        raise execute_cross_movie.Refused('Invalid execution ID')
    manifest = read_api.get_manifest(base, match.group(1))
    execute_cross_movie.require(manifest['integrity_ok'], 'Manifest integrity check failed')
    rows = [r for r in manifest['rows'] if r['execution_id'] == execution_id]
    execute_cross_movie.require(len(rows) == 1, 'Execution ID absent from manifest')
    media = rows[0].get('media_type')
    execute_cross_movie.require(media in ('Movie', 'TV'), 'Unsupported media type')
    return execute_cross_tv if media == 'TV' else execute_cross_movie


def normalized(value):
    # Journals store tuples as lists and integer keys as strings.
    return json.loads(json.dumps(value))


def load_recovery(m, base, execution_id):
    """Validate the journal is at exactly the diagnosed state; returns what recovery needs."""
    row, manifest_hash = m.load_plan(base, execution_id)
    journal = base / 'execution_logs' / (execution_id + '.jsonl')
    m.require(journal.exists(), 'No journal for ' + execution_id)
    events = [json.loads(line) for line in journal.read_text().splitlines() if line.strip()]
    m.require(events and all(e.get('execution_id') == execution_id for e in events),
              'Journal execution ID mismatch')
    m.require(not any(e.get('event') in LATER_EVENTS for e in events),
              'A later mutation or recovery already exists; reconcile manually')
    m.require(sum(e.get('event') == 'COPY_INTENT' for e in events) == 1, 'Expected exactly one COPY_INTENT')
    m.require(len(events) >= 4 and [e.get('event') for e in events[-4:]]
              == ['START', 'PREFLIGHT_OK', 'COPY_INTENT', 'STOPPED']
              and events[-4].get('live') is True,
              'Journal does not end with a live attempt stopped after COPY_INTENT')
    m.require(events[-1].get('error_type') == 'TimeoutExpired',
              'Attempt did not stop on a NAS timeout; reconcile manually')
    plan, intent = events[-3], events[-2]
    m.require(plan.get('manifest_sha256') == manifest_hash, 'Manifest differs from the copied plan')
    src, dst, logical_src, logical_dst = m.paths(row)
    m.require(plan.get('source') == str(src) and plan.get('destination') == str(dst)
              and plan.get('logical_source') == logical_src
              and plan.get('logical_destination') == logical_dst, 'Manifest and journal paths disagree')
    m.require(isinstance(intent.get('inventory'), dict) and intent['inventory'], 'COPY_INTENT has no inventory')
    return dict(row=row, manifest_hash=manifest_hash, plan=plan, intent=intent,
                expected=intent['inventory'], src=src, dst=dst,
                logical_src=logical_src, logical_dst=logical_dst,
                root=str(PurePosixPath(logical_dst).parent))


def check_tags(m, arr, item, row, noun):
    labels = {t['id']: t['label'].lower() for t in arr.api('tag')}
    tags = {labels.get(t, '') for t in item.get('tags', [])}
    m.require(not m.OVERRIDES.locked(tags), noun + ' is now locked')
    m.require(m.OVERRIDES.agrees(tags, row['recommended']), 'Current override conflicts')


def check_arr_at_source(m, arr, r):
    """Arr still points at the source, with the same media identity as when the copy started."""
    m.require(any(x.get('path') == r['root'] and x.get('accessible') is True for x in arr.api('rootfolder')),
              'Destination root inaccessible')
    if m is execute_cross_tv:
        series_id = r['plan']['sonarr_id']
        series = arr.api(f'series/{series_id}')
        m.require(series.get('id') == series_id and series.get('path') == r['logical_src'],
                  'Sonarr no longer points at the source')
        m.require(not any(x.get('path') == r['logical_dst'] for x in arr.api('series')),
                  'Sonarr destination is already assigned')
        check_tags(m, arr, series, r['row'], 'Series')
        state = m.episode_state(arr, series_id, r['logical_src'], r['expected'])
        m.require(normalized(state) == normalized((r['intent'].get('episode_files'),
                                                    r['intent'].get('episode_associations'))),
                  'Episode files or associations changed since the copy started')
        return series, state
    movie_id = r['plan']['radarr_id']
    movie = arr.api(f'movie/{movie_id}')
    media = dict(movie.get('movieFile', {}))
    relative = media.get('relativePath', '')
    m.require(media.get('id') == r['plan'].get('movie_file_id') and relative in r['expected']
              and len(r['expected'][relative]) == 4 and media.get('size') == r['expected'][relative][0],
              'Radarr movie-file identity changed')
    m.verify_movie(arr, movie_id, r['logical_src'], str(PurePosixPath(r['logical_src']).parent), media)
    m.require(not any(x.get('path') == r['logical_dst'] for x in arr.api('movie')),
              'Radarr destination is already assigned')
    check_tags(m, arr, movie, r['row'], 'Movie')
    return movie, media


def verify_arr_content(m, arr, r, state, logical):
    if m is execute_cross_tv:
        m.verify_episode_contents(arr, logical, state[0], r['expected'])
    else:
        arr.verify_file(logical + '/' + state['relativePath'], r['expected'][state['relativePath']][1])


def recover(m, base, execution_id, live, arr, transport, log):
    r = load_recovery(m, base, execution_id)
    src, dst = r['src'], r['dst']
    for p in (src, dst):
        m.require(p.is_dir(), 'Expected both source and published destination directories')
        m.canonical_existing(p)
    for p in (src.parent / ('.migratarr-delete-' + execution_id), dst.parent / ('.migratarr-stage-' + execution_id)):
        m.require(not os.path.lexists(p), 'Unexpected work directory: ' + str(p))
    m.progress('Rechecking source and published destination; nothing will be copied')
    m.require(m.inventory(src) == r['expected'], 'Source changed since the copy started; keep both copies')
    destination = m.verify_destination(dst, r['expected'])
    item, state = check_arr_at_source(m, arr, r)
    verify_arr_content(m, arr, r, state, r['logical_dst'])
    # Hash both trees on the NAS under the executors' NAS lock and rebuild the copy
    # receipt from what is observed there, not from the lost response.
    receipt = transport.call('receipt', src, dst, r['expected'], execution_id)
    m.require(receipt.get('result') == 'OK' and receipt.get('operation') == 'copy'
              and receipt.get('reconstructed') is True and len(receipt.get('source_id') or []) == 2
              and m.content_only(receipt['source_inventory']) == m.content_only(r['expected']),
              'NAS recovery receipt is invalid')
    if not live:
        return 'CHECK_ONLY'

    m.require(m.load_plan(base, execution_id) == (r['row'], r['manifest_hash']), 'Approval or manifest changed')
    m.require(m.file_metadata(src) == m.inventory_metadata(r['expected']), 'Source changed during verification')
    m.require(m.file_metadata(dst) == m.inventory_metadata(destination), 'Destination changed during verification')
    item, current = check_arr_at_source(m, arr, r)  # fresh record: preserve its settings in the update
    m.require(normalized(current) == normalized(state), 'Arr media changed during recovery')
    log('RECOVERY_STARTED', reason='Finish published copy after NAS timeout', receipt=receipt)
    tv = m is execute_cross_tv
    item_id = r['plan']['sonarr_id'] if tv else r['plan']['radarr_id']
    endpoint = ('series/' if tv else 'movie/') + str(item_id)
    log('SONARR_UPDATE_INTENT' if tv else 'RADARR_UPDATE_INTENT', recovery=True)
    arr.api(endpoint + '?moveFiles=false', dict(item, path=r['logical_dst'], rootFolderPath=r['root']))

    def verify_at_destination():
        if tv:
            after = arr.api(endpoint)
            m.require(after.get('id') == item_id and after.get('path') == r['logical_dst']
                      and after.get('rootFolderPath') == r['root'], 'Sonarr path verification failed')
            m.require(normalized(m.episode_state(arr, item_id, r['logical_dst'], r['expected']))
                      == normalized(state), 'Episode files or associations changed after update')
            return after
        return m.verify_movie(arr, item_id, r['logical_dst'], r['root'], state)

    after = verify_at_destination()
    for field in (SERIES_SETTINGS if tv else MOVIE_SETTINGS):
        m.require(after.get(field) == item.get(field), 'Arr setting changed: ' + field + '; source retained')
    verify_arr_content(m, arr, r, state, r['logical_dst'])
    m.require(m.load_plan(base, execution_id) == (r['row'], r['manifest_hash']), 'Approval changed before deletion')
    m.require(m.file_metadata(dst) == m.inventory_metadata(destination), 'Destination changed before deletion')
    verify_at_destination()
    log('DELETE_INTENT', receipt=receipt, recovery=True)
    transport.call('delete', src, dst, r['expected'], execution_id, receipt)
    log('SOURCE_REMOVED', recovery=True)
    m.wait_source_absent(src)
    m.require(m.file_metadata(dst) == m.inventory_metadata(destination), 'Final destination metadata differs')
    verify_at_destination()
    log('SUCCESS', manifest_sha256=r['manifest_hash'], recovery=True)
    return 'SUCCESS'


def nas_receipt(data):
    # Runs on the NAS inside the executor's shipped program, where these helpers are globals.
    require(data['operation'] == 'receipt', 'Unexpected recovery operation')
    execution_id = data['execution_id']
    require(re.fullmatch(r'\d{8}T\d{6}Z-\d{4,}', execution_id), 'Invalid execution ID')
    src, dst = nas_pair(data['source'], data['destination'], NAS_LAYOUT)
    for p in (src, dst):
        canonical_existing(p)
        require(p.is_dir(), 'Expected both NAS directories')
    require(src.stat().st_dev != dst.stat().st_dev, 'Expected distinct NAS filesystems')
    for p in (src.parent / ('.migratarr-delete-' + execution_id),
              dst.parent / ('.migratarr-stage-' + execution_id)):
        require(not os.path.lexists(p), 'Unexpected work directory')
    source_id = [src.stat().st_dev, src.stat().st_ino]
    source = inventory(src)
    destination = inventory(dst)
    require(content_only(source) == content_only(data['inventory']) == content_only(destination),
            'NAS content mismatch; source retained')
    require(file_metadata(src) == inventory_metadata(source)
            and [src.stat().st_dev, src.stat().st_ino] == source_id, 'NAS source changed during verification')
    sync_parents(src, dst)
    return dict(result='OK', operation='copy', reconstructed=True, source_inventory=source, source_id=source_id)


def receipt_program(m):
    program = m.remote_program()
    marker = 'data = json.load(sys.stdin)'
    dispatch = 'print(json.dumps(nas_operation(data)))'
    m.require(program.count(marker) == 1 and program.count(dispatch) == 1,
              'Installed executor helper differs; no recovery attempted')
    program = program.replace(marker, inspect.getsource(nas_receipt) + '\n' + marker)
    return program.replace(dispatch, 'print(json.dumps(nas_receipt(data)))')


def transport_class(m):
    class RecoveryTransport(m.NasTransport):
        def call(self, operation, src, dst, before, execution_id, receipt=None):
            m.require(operation in {'receipt', 'delete'}, 'Recovery never copies')
            payload = dict(operation=operation, source=m.remote_path(src), destination=m.remote_path(dst),
                           inventory=before, execution_id=execution_id, receipt=receipt)
            program = receipt_program(m) if operation == 'receipt' else m.remote_program()
            output = m.run_progress(['ssh', *self.options, '-o', 'BatchMode=yes', m.RUNTIME.ssh_target,
                                     shlex.quote(m.RUNTIME.remote_python) + ' -c ' + shlex.quote(program)],
                                    'NAS recovery ' + operation, input=json.dumps(payload),
                                    timeout=None, idle_timeout=NAS_IDLE_TIMEOUT)
            response = json.loads(output)
            m.require(response.get('result') == 'OK'
                      and response.get('operation') == ('copy' if operation == 'receipt' else 'delete'),
                      'Unexpected NAS response; reconcile manually')
            return response
    return RecoveryTransport


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('execution_id')
    parser.add_argument('--base', type=Path, default=None)
    parser.add_argument('--execute', action='store_true', help='Finish the move; default is check only')
    args = parser.parse_args(argv)
    base = args.base or execute_cross_movie.RUNTIME.base_path
    m = executor_for(base, args.execution_id)
    m.require(sys.platform.startswith('linux'), 'Run on the Linux media host')
    import fcntl
    logs = base / 'execution_logs'
    with (logs / 'executor.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        load_recovery(m, base, args.execution_id)  # validate before opening the journal for append
        journal = logs / (args.execution_id + '.jsonl')
        with journal.open('a', encoding='utf-8') as stream:
            started = False

            def log(event, **details):
                nonlocal started
                started = True
                stream.write(json.dumps(dict(utc=datetime.now(timezone.utc).isoformat(),
                                             execution_id=args.execution_id, event=event, **details)) + '\n')
                stream.flush()
                os.fsync(stream.fileno())
                m.progress(event.replace('_', ' '))
            try:
                arr = m.Sonarr() if m is execute_cross_tv else m.Radarr()
                with transport_class(m)() as transport:
                    result = recover(m, base, args.execution_id, args.execute, arr, transport, log)
                print(result + ': ' + args.execution_id)
            except BaseException as exc:
                if started:  # check-only and pre-mutation refusals leave the journal untouched
                    log('STOPPED', error_type=type(exc).__name__, reason=str(exc))
                print('STOPPED: ' + str(exc), file=sys.stderr)
                return 1
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (execute_cross_movie.Refused, execute_cross_tv.Refused, OSError) as exc:
        sys.exit('STOPPED: ' + str(exc))
