"""One approved cross-disk TV series transfer; check-only by default."""
import argparse

import ctypes

import errno

import hashlib

import inspect

import json

import os

from pathlib import Path, PurePath, PurePosixPath

import re

import stat

import shlex

import subprocess

import sys

import tempfile
import shutil

import time

from datetime import datetime, timezone

import urllib.request


from runtime_config import get_config
from planner_settings import get_settings
from media_layout import MediaLayout, canonical, get_targets, split_media_path
from service_keys import SecretError, service_endpoint
from arr_files import host_path, host_sha256, host_visible
from executor_manifest import digest, load_approved_plan
from executor_command import NAS_IDLE_TIMEOUT, run_command
from executor_inventory import scan_metadata, metadata_from_inventory
from migratarr.engine import nas_side, primitives

# Shared code, bound into this module so it resolves names (inventory, nas_pair,
# RUNTIME, ...) here, exactly as when it was defined in this file.
primitives.bind(globals(), nas_side.Refused, nas_side.require, nas_side.canonical_existing, nas_side.file_metadata, nas_side.inventory_metadata, nas_side.rename_noreplace, nas_side.sync_parents, nas_side.content_only, nas_side.copy_tree, nas_side.remove_verified_tree, nas_side.nas_operation,
                primitives.layout, primitives.posix, primitives.remote_path, primitives.verify_destination, primitives.wait_source_absent)
RUNTIME = get_config()
OVERRIDES = get_settings().overrides
TARGETS = get_targets()


def load_plan(base, execution_id):
    return load_approved_plan(base, execution_id, 'TV', 'CROSS_DISK_TRANSFER', require)


def progress(message):
    print('[%s] %s' % (datetime.now().strftime('%H:%M:%S'), message), file=sys.stderr, flush=True)


def inventory(root):
    """Hash every regular file; reject links, devices, and nested mount points."""
    device = root.stat().st_dev
    found = {}
    initial = file_metadata(root)
    total = sum(v[0] for v in initial.values() if len(v) == 3)
    started = last = time.monotonic()
    checked = 0
    progress('Hashing %s: %.2f GiB' % (root, total / 2**30))
    def visit(folder):
        nonlocal checked, last
        for p in sorted(folder.iterdir()):
            s = p.lstat()
            require(s.st_dev == device and not stat.S_ISLNK(s.st_mode), 'Link or nested device found')
            key = p.relative_to(root).as_posix()
            if stat.S_ISDIR(s.st_mode):
                found[key] = ['directory']
                visit(p)
            else:
                require(stat.S_ISREG(s.st_mode), 'Nonregular file found')
                h = hashlib.sha256()
                with p.open('rb') as f:
                    for chunk in iter(lambda: f.read(1024 * 1024), b''):
                        h.update(chunk)
                        checked += len(chunk)
                        now = time.monotonic()
                        if now - last >= 15:
                            progress('Hashed %.2f / %.2f GiB; elapsed %.0fs' %
                                     (checked / 2**30, total / 2**30, now - started))
                            last = now
                after = p.stat()
                require((s.st_ino, s.st_size, s.st_mtime_ns) ==
                        (after.st_ino, after.st_size, after.st_mtime_ns), 'File changed while hashing')
                found[key] = [s.st_size, h.hexdigest(), s.st_ino, s.st_mtime_ns]
    visit(root)
    require(any(len(v) == 4 and v[0] > 0 for v in found.values()), 'Source contains no media data')
    require(inventory_metadata(found) == initial == file_metadata(root), 'Files changed while hashing')
    progress('Hash complete: %.2f GiB; elapsed %.0fs' % (checked / 2**30, time.monotonic() - started))
    return found

def run_progress(command, label, input=None, timeout=7200, idle_timeout=None):
    return run_command(command, label, input, timeout, require=require, progress=progress,
                       idle_timeout=idle_timeout)


class Sonarr:
    def __init__(self):
        # runtime.json urls + "secrets" (default: ApiKey/UrlBase from the container's config.xml).
        try:
            self.api_root, self.key = service_endpoint('sonarr', RUNTIME)
        except SecretError as exc:
            require(False, str(exc))
        require(self.key, 'Sonarr API key missing')
        # Disable redirects so an API key cannot be forwarded to another server.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        self.opener = urllib.request.build_opener(NoRedirect, urllib.request.ProxyHandler({}))

    def api(self, path, body=None):
        req = urllib.request.Request(self.api_root + '/api/v3/' + path,
                                     data=None if body is None else json.dumps(body).encode(),
                                     headers={'X-Api-Key': self.key, 'Content-Type': 'application/json'},
                                     method='GET' if body is None else 'PUT')
        with self.opener.open(req, timeout=30) as response:
            data = response.read()
            return json.loads(data) if data else None

    def visible(self, path, kind='-f'):
        # runtime.json arr_file_checks (default docker; see arr_files.py).
        check = RUNTIME.arr_file_checks['sonarr']
        if check['mode'] == 'host':
            require(host_visible(host_path(check, TARGETS.arr_root, path), kind),
                    'Sonarr path not visible on this host: ' + path)
            return
        r = subprocess.run(['docker', 'exec', check['container'], 'test', kind, path], timeout=30,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        require(r.returncode == 0, 'Sonarr container cannot see required path: ' + path)

    def verify_file(self, path, expected_hash):
        self.visible(path)
        check = RUNTIME.arr_file_checks['sonarr']
        if check['mode'] == 'host':
            digest = host_sha256(host_path(check, TARGETS.arr_root, path))
            require(digest == expected_hash, 'Sonarr-visible file content mismatch')
            return
        result = run_progress(['docker', 'exec', check['container'], 'sha256sum', '--', path],
                              'Sonarr file-content verification')
        require(result.split()[0] == expected_hash, 'Sonarr-visible file content mismatch')


def episode_state(sonarr, series_id, logical_path, before):
    files = sonarr.api(f'episodefile?seriesId={series_id}')
    require(files, 'Series has no episode files')
    records = {}
    names = set()
    for f in files:
        relative = f.get('relativePath', '')
        path = PurePosixPath(relative)
        require(relative and str(path) == relative and not path.is_absolute()
                and '..' not in path.parts and relative not in names, 'Invalid or duplicate episode path')
        require(f.get('seriesId') == series_id and f['id'] not in records, 'Episode file identity mismatch')
        require(f.get('path') == logical_path + '/' + relative, 'Sonarr episode path mismatch')
        require(relative in before and len(before[relative]) == 4
                and before[relative][0] > 0 and f.get('size') == before[relative][0],
                'Episode file missing or size differs: ' + relative)
        records[f['id']] = (relative, f['size'])
        names.add(relative)
    episodes = sonarr.api(f'episode?seriesId={series_id}')
    associations = {}
    for e in episodes:
        require(e.get('seriesId') == series_id and e['id'] not in associations,
                'Episode identity mismatch')
        file_id = e.get('episodeFileId', 0)
        require(not file_id or file_id in records, 'Episode references unknown file')
        require(bool(e.get('hasFile')) == bool(file_id), 'Inconsistent episode file association')
        associations[e['id']] = (e.get('seasonNumber'), e.get('episodeNumber'), file_id,
                                  e.get('monitored'))
    require({v[2] for v in associations.values() if v[2]} == set(records),
            'Episode file is not linked to any episode')
    return records, associations


def verify_episode_contents(sonarr, logical_path, records, before):
    for index, (relative, size) in enumerate(sorted(records.values()), 1):
        progress('Sonarr file %d/%d: %s (%.2f GiB)' %
                 (index, len(records), relative, size / 2**30))
        sonarr.verify_file(logical_path + '/' + relative, before[relative][1])


class NasTransport:
    def __enter__(self):
        self.temp = tempfile.TemporaryDirectory(prefix='migratarr-ssh-')
        self.options = ['-i', RUNTIME.ssh_key,
                        '-o', 'IdentitiesOnly=yes',
                        '-o', 'BatchMode=yes',
                        '-o', 'StrictHostKeyChecking=yes', '-o', 'ConnectTimeout=15',
                        '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=3',
                        '-o', 'ControlPath=' + self.temp.name + '/socket']
        try:
            # Prompt once through the terminal; no credentials are stored by Python.
            subprocess.run(['ssh', *self.options, '-M', '-N', '-f',
                            '-o', 'ControlPersist=yes', RUNTIME.ssh_target], check=True, timeout=120)
        except BaseException:
            self.temp.cleanup()
            raise
        return self

    def __exit__(self, *exc):
        try:
            subprocess.run(['ssh', *self.options, '-O', 'exit', RUNTIME.ssh_target],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
        finally:
            self.temp.cleanup()

    def call(self, operation, src, dst, before, execution_id, receipt=None):
        payload = dict(operation=operation, source=remote_path(src), destination=remote_path(dst),
                       inventory=before, execution_id=execution_id, receipt=receipt)
        result = run_progress(['ssh', *self.options, '-o', 'BatchMode=yes', RUNTIME.ssh_target,
                                 shlex.quote(RUNTIME.remote_python) + ' -c ' + shlex.quote(remote_program())],
                              'NAS ' + operation, input=json.dumps(payload),
                              timeout=None, idle_timeout=NAS_IDLE_TIMEOUT)
        response = json.loads(result)
        require(response.get('result') == 'OK' and response.get('operation') == operation,
                'Unexpected NAS response; reconcile before retrying')
        return response

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('execution_id')
    parser.add_argument('--base', type=Path, default=RUNTIME.base_path)
    parser.add_argument('--execute', action='store_true', help='Perform the approved move; default is check only')
    args = parser.parse_args()
    require(sys.platform.startswith('linux'), 'Run on the Linux media host')
    import fcntl
    require(re.fullmatch(r'\d{8}T\d{6}Z-\d{4,}', args.execution_id), 'Invalid execution ID')
    logs = args.base / 'execution_logs'
    logs.mkdir(exist_ok=True)
    with (logs / 'executor.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        journal = logs / (args.execution_id + '.jsonl')
        check_journal(journal)
        with journal.open('a', encoding='utf-8') as stream:
            def log(event, **details):
                progress(event.replace('_', ' '))
                stream.write(json.dumps(dict(utc=datetime.now(timezone.utc).isoformat(),
                                             execution_id=args.execution_id, event=event, **details)) + '\n')
                stream.flush()
                os.fsync(stream.fileno())
            # Persist the journal directory entry before any media mutation.
            fd = os.open(logs, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            try:
                log('START', live=args.execute)
                with NasTransport() as transport:
                    result = execute(args.base, args.execution_id, args.execute, Sonarr(), log,
                                     transport)
                print(result + ': ' + args.execution_id)
            except BaseException as exc:
                log('STOPPED', error_type=type(exc).__name__, reason=str(exc))
                print('STOPPED: ' + str(exc), file=sys.stderr)
                print('Inspect journal: ' + str(journal) + '. No automatic rollback or retry.', file=sys.stderr)
                return 1
    return 0


MEDIA = 'TV'


def paths(row):
    result = []
    for field, category, disk in [('source_path', 'current', 'source_disk'),
                                  ('target_path', 'recommended', 'target_disk')]:
        raw = row[field]
        found = layout().parse_local(raw, MEDIA) if canonical(raw) else None
        require(found is not None and found[0] == row[disk] and found[1] == row[category],
                'Invalid manifest path')
        result.append(Path(raw))
    src, dst = result
    require(src.name == dst.name and row['source_disk'] != row['target_disk'],
            'Expected distinct disks and unchanged series folder name')
    return src, dst, layout().logical(MEDIA, row['current'], src.name), \
        layout().logical(MEDIA, row['recommended'], dst.name)


def remote_layout():
    """Disk roots and TV category folders as the NAS sees them; shipped with the program."""
    return layout().remote_roots, layout().category_dirs[MEDIA]


# In-process view of what remote_program() ships (bound at import, as DISKS was).
NAS_LAYOUT = remote_layout()


def nas_pair(source, destination, nas_layout):
    roots, category_dirs = nas_layout
    found = [split_media_path(p, roots, category_dirs) for p in (source, destination)]
    for item in found:
        require(item is not None, 'Invalid NAS TV path')
    require(found[0][2] == found[1][2] and found[0][0] != found[1][0], 'Not a cross-disk pair')
    return Path(source), Path(destination)


def check_journal(journal):
    if journal.exists():
        events = [json.loads(line) for line in journal.read_text().splitlines()]
        blocked = {'COPY_INTENT', 'COPIED', 'SONARR_UPDATE_INTENT', 'DELETE_INTENT',
                   'SOURCE_REMOVED', 'SUCCESS', 'RENAME_INTENT'}
        require(not any(e['event'] in blocked for e in events),
                'Previous live attempt exists; reconcile manually before retrying')


def execute(base, execution_id, live, sonarr, log, transport):
    row, manifest_hash = load_plan(base, execution_id)
    src, dst, logical_src, logical_dst = paths(row)
    canonical_existing(src)
    canonical_existing(dst.parent)
    require(src.is_dir() and dst.parent.is_dir() and not os.path.lexists(dst), 'Source/destination not ready')
    system = sonarr.api('system/status')
    require(str(system.get('version', '')).startswith('4.'), 'Only Sonarr v4 is supported')
    all_series = sonarr.api('series')
    matches = [x for x in all_series if x.get('path') == logical_src]
    require(len(matches) == 1 and not any(x.get('path') == logical_dst for x in all_series),
            'Sonarr source missing/ambiguous or destination occupied')
    series_id = matches[0]['id']
    series = sonarr.api(f'series/{series_id}')
    require(series.get('path') == logical_src, 'Sonarr source changed')
    for field in ('sonarr_id', 'item_id'):
        if row.get(field):
            require(int(row[field]) == series_id, 'Sonarr ID mismatch')
    target_root = str(PurePosixPath(logical_dst).parent)
    require(any(r.get('path') == target_root and r.get('accessible') is True for r in sonarr.api('rootfolder')),
            'Sonarr destination root inaccessible')
    labels = {t['id']: t['label'].lower() for t in sonarr.api('tag')}
    tags = {labels.get(t, '') for t in series.get('tags', [])}
    require(not OVERRIDES.locked(tags), 'Series is locked')
    require(OVERRIDES.agrees(tags, row['recommended']), 'Conflicting override')
    before = inventory(src)
    records, associations = episode_state(sonarr, series_id, logical_src, before)
    verify_episode_contents(sonarr, logical_src, records, before)
    sonarr.visible(target_root, '-d')
    transport.call('check', src, dst, before, execution_id)
    log('PREFLIGHT_OK', manifest_sha256=manifest_hash, source=str(src), destination=str(dst),
        logical_source=logical_src, logical_destination=logical_dst, sonarr_id=series_id,
        sonarr_version=system['version'], episode_file_count=len(records),
        linked_episode_count=sum(bool(v[2]) for v in associations.values()))
    if not live:
        log('CHECK_ONLY', manifest_sha256=manifest_hash)
        return 'CHECK_ONLY'
    require(load_plan(base, execution_id) == (row, manifest_hash), 'Approval or plan changed')
    require(sonarr.api(f'series/{series_id}') == series, 'Sonarr changed during preflight')
    require(episode_state(sonarr, series_id, logical_src, before) == (records, associations),
            'Episode files or associations changed during preflight')
    require(file_metadata(src) == inventory_metadata(before), 'Source changed during preflight')
    log('COPY_INTENT', inventory=before, episode_files=records, episode_associations=associations)
    receipt = transport.call('copy', src, dst, before, execution_id)
    log('COPIED', receipt=receipt)
    destination = verify_destination(dst, before)
    require(inventory(src) == before, 'Source changed while copying; retain both copies')
    verify_episode_contents(sonarr, logical_dst, records, before)
    require(load_plan(base, execution_id) == (row, manifest_hash), 'Approval changed after copy')
    require(sonarr.api(f'series/{series_id}') == series, 'Sonarr changed before update')
    require(episode_state(sonarr, series_id, logical_src, before) == (records, associations),
            'Episode associations changed before path update')
    log('SONARR_UPDATE_INTENT')
    sonarr.api(f'series/{series_id}?moveFiles=false', dict(series, path=logical_dst,
                                                        rootFolderPath=target_root))
    after = sonarr.api(f'series/{series_id}')
    require(after.get('id') == series_id and after.get('path') == logical_dst
            and after.get('rootFolderPath') == target_root, 'Sonarr path verification failed')
    for field in ('seriesType', 'seasonFolder', 'monitored', 'qualityProfileId', 'tags'):
        require(after.get(field) == series.get(field), 'Sonarr setting changed: ' + field)
    require(episode_state(sonarr, series_id, logical_dst, before) == (records, associations),
            'Episode files or associations changed after update')
    verify_episode_contents(sonarr, logical_dst, records, before)
    require(file_metadata(dst) == inventory_metadata(destination), 'Destination changed after verification')
    require(load_plan(base, execution_id) == (row, manifest_hash), 'Approval changed before deletion')
    require(episode_state(sonarr, series_id, logical_dst, before) == (records, associations),
            'Episode associations changed before deletion')
    log('DELETE_INTENT', receipt=receipt)
    transport.call('delete', src, dst, before, execution_id, receipt)
    log('SOURCE_REMOVED')
    wait_source_absent(src)
    require(file_metadata(dst) == inventory_metadata(destination), 'Final destination metadata mismatch')
    require(episode_state(sonarr, series_id, logical_dst, before) == (records, associations),
            'Episode associations changed during final verification')
    verify_episode_contents(sonarr, logical_dst, records, before)
    final = sonarr.api(f'series/{series_id}')
    require(final.get('path') == logical_dst and final.get('rootFolderPath') == target_root,
            'Sonarr path changed during final verification')
    log('SUCCESS', manifest_sha256=manifest_hash, episode_file_count=len(records),
        linked_episode_count=sum(bool(v[2]) for v in associations.values()))
    return 'SUCCESS'


def remote_program():
    imports = ('import ctypes, hashlib, json, os, stat, sys, fcntl, time, shutil, re\n'
               'from pathlib import Path, PurePosixPath\nfrom datetime import datetime\n')
    functions = [scan_metadata, metadata_from_inventory, Refused, require, progress, split_media_path,
                 nas_pair, canonical_existing,
                 file_metadata, inventory_metadata, inventory, rename_noreplace, sync_parents, content_only,
                 copy_tree, remove_verified_tree, nas_operation]
    code = imports + 'NAS_LAYOUT = ' + repr(remote_layout()) + '\n\n'
    code += '\n\n'.join(inspect.getsource(f) for f in functions)
    return code + '''
data = json.load(sys.stdin)
lockfd = os.open('/tmp/migratarr-movie-' + str(os.getuid()) + '.lock',
                 os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
with os.fdopen(lockfd, 'a') as lock:
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    print(json.dumps(nas_operation(data)))
'''


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (Refused, OSError) as exc:
        sys.exit('STOPPED: ' + str(exc))
