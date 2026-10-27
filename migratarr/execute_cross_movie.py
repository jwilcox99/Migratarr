"""One approved cross-disk movie transfer; check-only by default."""
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


from migratarr.runtime_config import get_config
from migratarr.planner_settings import get_settings
from migratarr.media_layout import MediaLayout, canonical, get_targets, split_media_path
from migratarr.service_keys import SecretError, service_endpoint
from migratarr.arr_files import host_path, host_sha256, host_visible
from migratarr.executor_manifest import digest, load_approved_plan
from migratarr.executor_command import NAS_IDLE_TIMEOUT, run_command
from migratarr.executor_inventory import scan_metadata, metadata_from_inventory
from migratarr.engine import nas_side, primitives, transport

# Shared code, bound into this module so it resolves names (inventory, nas_pair,
# RUNTIME, ...) here, exactly as when it was defined in this file.
primitives.bind(globals(), nas_side.Refused, nas_side.require, nas_side.canonical_existing, nas_side.file_metadata, nas_side.inventory_metadata, nas_side.rename_noreplace, nas_side.sync_parents, nas_side.content_only, nas_side.copy_tree, nas_side.remove_verified_tree, nas_side.nas_operation,
                primitives.layout, primitives.posix, primitives.remote_path, primitives.verify_destination, primitives.wait_source_absent)
RUNTIME = get_config()
OVERRIDES = get_settings().overrides
TARGETS = get_targets()


def load_plan(base, execution_id):
    return load_approved_plan(base, execution_id, 'Movie', 'CROSS_DISK_TRANSFER', require)


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


class Radarr:
    def __init__(self):
        # runtime.json urls + "secrets" (default: ApiKey/UrlBase from the container's config.xml).
        try:
            self.api_root, self.key = service_endpoint('radarr', RUNTIME)
        except SecretError as exc:
            require(False, str(exc))
        require(self.key, 'Radarr API key missing')
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
        check = RUNTIME.arr_file_checks['radarr']
        if check['mode'] == 'host':
            require(host_visible(host_path(check, TARGETS.arr_root, path), kind),
                    'Radarr path not visible on this host: ' + path)
            return
        r = subprocess.run(['docker', 'exec', check['container'], 'test', kind, path], timeout=30,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        require(r.returncode == 0, 'Radarr container cannot see required path: ' + path)

    def verify_file(self, path, expected_hash):
        self.visible(path)
        check = RUNTIME.arr_file_checks['radarr']
        if check['mode'] == 'host':
            digest = host_sha256(host_path(check, TARGETS.arr_root, path))
            require(digest == expected_hash, 'Radarr-visible file content mismatch')
            return
        result = run_progress(['docker', 'exec', check['container'], 'sha256sum', '--', path],
                              'Radarr file-content verification')
        require(result.split()[0] == expected_hash, 'Radarr-visible file content mismatch')

class NasTransport(transport.NasChannel):
    """The NAS program's channel (migratarr/engine/transport.py): SSH, or this host for topology local."""
    runtime = property(lambda self: RUNTIME)

    def call(self, operation, src, dst, before, execution_id, receipt=None):
        payload = dict(operation=operation, source=remote_path(src), destination=remote_path(dst),
                       inventory=before, execution_id=execution_id, receipt=receipt)
        result = run_progress(self.command(remote_program()),
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
                    result = execute(args.base, args.execution_id, args.execute, Radarr(), log,
                                     transport)
                print(result + ': ' + args.execution_id)
            except BaseException as exc:
                log('STOPPED', error_type=type(exc).__name__, reason=str(exc))
                print('STOPPED: ' + str(exc), file=sys.stderr)
                print('Inspect journal: ' + str(journal) + '. No automatic rollback or retry.', file=sys.stderr)
                return 1
    return 0


MEDIA = 'Movie'


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
            'Expected distinct disks and unchanged movie folder name')
    return src, dst, layout().logical(MEDIA, row['current'], src.name), \
        layout().logical(MEDIA, row['recommended'], dst.name)


def remote_layout():
    """Disk roots and Movie category folders as the NAS sees them; shipped with the program."""
    return layout().remote_roots, layout().category_dirs[MEDIA]


# In-process view of what remote_program() ships (bound at import, as DISKS was).
NAS_LAYOUT = remote_layout()


def nas_pair(source, destination, nas_layout):
    roots, category_dirs = nas_layout
    found = [split_media_path(p, roots, category_dirs) for p in (source, destination)]
    for item in found:
        require(item is not None, 'Invalid NAS movie path')
    require(found[0][2] == found[1][2] and found[0][0] != found[1][0], 'Not a cross-disk pair')
    return Path(source), Path(destination)


def check_journal(journal):
    if journal.exists():
        events = [json.loads(line) for line in journal.read_text().splitlines()]
        blocked = {'COPY_INTENT', 'COPIED', 'RADARR_UPDATE_INTENT', 'DELETE_INTENT',
                   'SOURCE_REMOVED', 'SUCCESS', 'RENAME_INTENT'}
        require(not any(e['event'] in blocked for e in events),
                'Previous live attempt exists; reconcile manually before retrying')


def verify_movie(radarr, movie_id, logical, root, file_record):
    movie = radarr.api(f'movie/{movie_id}')
    require(movie.get('id') == movie_id and movie.get('path') == logical
            and movie.get('rootFolderPath') == root and movie.get('hasFile'), 'Radarr path verification failed')
    current = movie.get('movieFile', {})
    require(all(current.get(k) == file_record.get(k) for k in ('id', 'relativePath', 'size')),
            'Radarr movie-file identity changed')
    require(current.get('path') == logical + '/' + file_record['relativePath'], 'Radarr file path mismatch')
    return movie


def execute(base, execution_id, live, radarr, log, transport):
    row, manifest_hash = load_plan(base, execution_id)
    src, dst, logical_src, logical_dst = paths(row)
    canonical_existing(src)
    canonical_existing(dst.parent)
    require(src.is_dir() and dst.parent.is_dir() and not os.path.lexists(dst), 'Source/destination not ready')
    system = radarr.api('system/status')
    require(system.get('version'), 'Missing Radarr version')
    movies = radarr.api('movie')
    matches = [m for m in movies if m.get('path') == logical_src]
    require(len(matches) == 1 and not any(m.get('path') == logical_dst for m in movies),
            'Radarr source missing/ambiguous or destination occupied')
    movie_id = matches[0]['id']
    movie = radarr.api(f'movie/{movie_id}')
    require(movie.get('path') == logical_src and movie.get('hasFile'), 'Radarr source changed')
    for field in ('radarr_id', 'item_id'):
        if row.get(field):
            require(int(row[field]) == movie_id, 'Radarr ID mismatch')
    root = str(PurePosixPath(logical_dst).parent)
    require(any(r.get('path') == root and r.get('accessible') is True for r in radarr.api('rootfolder')),
            'Radarr destination root inaccessible')
    labels = {t['id']: t['label'].lower() for t in radarr.api('tag')}
    tags = {labels.get(t, '') for t in movie.get('tags', [])}
    require(not OVERRIDES.locked(tags), 'Movie is locked')
    require(OVERRIDES.agrees(tags, row['recommended']), 'Conflicting override')
    before = inventory(src)
    file_record = dict(movie.get('movieFile', {}))
    relative = file_record.get('relativePath', '')
    require(relative in before and len(before[relative]) == 4
            and before[relative][0] == file_record.get('size') and before[relative][0] > 0,
            'Radarr media file missing or size differs')
    require(not PurePosixPath(relative).is_absolute() and '..' not in PurePosixPath(relative).parts,
            'Invalid media path')
    radarr.verify_file(logical_src + '/' + relative, before[relative][1])
    radarr.visible(root, '-d')
    transport.call('check', src, dst, before, execution_id)
    log('PREFLIGHT_OK', manifest_sha256=manifest_hash, source=str(src), destination=str(dst),
        logical_source=logical_src, logical_destination=logical_dst, radarr_id=movie_id,
        radarr_version=system['version'], movie_file_id=file_record['id'])
    if not live:
        log('CHECK_ONLY', manifest_sha256=manifest_hash)
        return 'CHECK_ONLY'
    require(load_plan(base, execution_id) == (row, manifest_hash), 'Approval or plan changed')
    require(radarr.api(f'movie/{movie_id}') == movie, 'Radarr changed during preflight')
    require(file_metadata(src) == inventory_metadata(before), 'Source changed during preflight')
    log('COPY_INTENT', inventory=before)
    receipt = transport.call('copy', src, dst, before, execution_id)
    log('COPIED', receipt=receipt)
    destination = verify_destination(dst, before)
    require(inventory(src) == before, 'Source changed while copying; retain both copies')
    radarr.verify_file(logical_dst + '/' + relative, before[relative][1])
    require(load_plan(base, execution_id) == (row, manifest_hash), 'Approval changed after copy')
    require(radarr.api(f'movie/{movie_id}') == movie, 'Radarr changed before update')
    log('RADARR_UPDATE_INTENT')
    radarr.api(f'movie/{movie_id}?moveFiles=false', dict(movie, path=logical_dst, rootFolderPath=root))
    updated = verify_movie(radarr, movie_id, logical_dst, root, file_record)
    require(all(updated.get(k) == movie.get(k) for k in ('monitored', 'qualityProfileId', 'tags')),
            'Radarr settings changed; source retained')
    radarr.verify_file(logical_dst + '/' + relative, before[relative][1])
    require(file_metadata(dst) == inventory_metadata(destination), 'Destination changed after verification')
    require(load_plan(base, execution_id) == (row, manifest_hash), 'Approval changed before deletion')
    verify_movie(radarr, movie_id, logical_dst, root, file_record)
    log('DELETE_INTENT', receipt=receipt)
    transport.call('delete', src, dst, before, execution_id, receipt)
    log('SOURCE_REMOVED')
    wait_source_absent(src)
    require(file_metadata(dst) == inventory_metadata(destination), 'Final destination metadata mismatch')
    verify_movie(radarr, movie_id, logical_dst, root, file_record)
    radarr.visible(logical_dst + '/' + relative)
    log('SUCCESS', manifest_sha256=manifest_hash)
    return 'SUCCESS'


def remote_program():
    imports = ('import ctypes, hashlib, json, os, stat, sys, fcntl, time, shutil, re\n'
               'from pathlib import Path, PurePosixPath\nfrom datetime import datetime\n')
    functions = [scan_metadata, metadata_from_inventory, Refused, require, progress, split_media_path,
                 nas_pair, canonical_existing, file_metadata, inventory_metadata,
                 inventory, rename_noreplace, sync_parents, content_only, copy_tree,
                 remove_verified_tree, nas_operation]
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
