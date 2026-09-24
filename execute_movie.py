#!/usr/bin/env python3
"""One approved same-disk movie rename. Python 3.9+, Linux host only."""
import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path, PurePath, PurePosixPath
import re
import stat
import subprocess
import sys
from datetime import datetime, timezone
import urllib.request

from runtime_config import get_config
from planner_settings import get_settings
from media_layout import MediaLayout, canonical, get_targets, split_media_path
from service_keys import SecretError, service_endpoint
from arr_files import host_path, host_sha256, host_visible
from executor_manifest import digest, load_approved_plan
RUNTIME = get_config()
OVERRIDES = get_settings().overrides
TARGETS = get_targets()


class Refused(RuntimeError):
    pass


def require(ok, message):
    if not ok:
        raise Refused(message)


def load_plan(base, execution_id):
    return load_approved_plan(base, execution_id, 'Movie', 'SAME_DISK_RENAME', require)


MEDIA = 'Movie'


def layout():
    # Built per call from RUNTIME, so a patched or reloaded runtime is honored.
    return MediaLayout(RUNTIME, TARGETS)


def posix(path):
    return path.as_posix() if isinstance(path, PurePath) else str(path)


def paths(row):
    result = []
    for field, category, disk_field in [('source_path', 'current', 'source_disk'),
                                         ('target_path', 'recommended', 'target_disk')]:
        raw = row[field]
        require(canonical(raw), 'Noncanonical path')
        found = layout().parse_local(raw, MEDIA)
        require(found is not None and found[0] == row[disk_field] and found[1] == row[category],
                'Path/category/disk mismatch')
        result.append(Path(raw))
    src, dst = result
    require(src.name == dst.name and src != dst and row['source_disk'] == row['target_disk'],
            'Not a same-disk category rename')
    return src, dst, layout().logical(MEDIA, row['current'], src.name), \
        layout().logical(MEDIA, row['recommended'], dst.name)


def canonical_existing(p):
    require(p.resolve(strict=True) == p, 'Symlink or noncanonical filesystem path: ' + str(p))


def filesystem_ready(src, dst):
    canonical_existing(src)
    canonical_existing(dst.parent)
    require(src.is_dir() and dst.parent.is_dir(), 'Missing source or destination parent')
    require(not os.path.lexists(dst), 'Destination already exists')
    require(src.stat().st_dev == dst.parent.stat().st_dev, 'Different filesystem devices')


def inventory(root):
    """Hash every regular file; reject links, devices, and nested mount points."""
    device = root.stat().st_dev
    found = {}
    def visit(folder):
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
                after = p.stat()
                require((s.st_ino, s.st_size, s.st_mtime_ns) ==
                        (after.st_ino, after.st_size, after.st_mtime_ns), 'File changed while hashing')
                found[key] = [s.st_size, h.hexdigest(), s.st_ino, s.st_mtime_ns]
    visit(root)
    require(any(len(v) == 4 and v[0] > 0 for v in found.values()), 'Source contains no media data')
    return found


def rename_noreplace(src, dst):
    # Atomic collision protection. Never fall back to copy/delete or replacing rename.
    libc = ctypes.CDLL(None, use_errno=True)
    require(hasattr(libc, 'renameat2'), 'renameat2 unavailable; refusing unsafe fallback')
    fn = libc.renameat2
    fn.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    fn.restype = ctypes.c_int
    if fn(-100, os.fsencode(src), -100, os.fsencode(dst), 1) != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))


def sync_parents(src, dst):
    for parent in {src.parent, dst.parent}:
        fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


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
        result = subprocess.check_output(
            ['docker', 'exec', check['container'], 'sha256sum', '--', path], timeout=1800, text=True)
        require(result.split()[0] == expected_hash, 'Radarr-visible file content mismatch')


def execute(base, execution_id, live, radarr, log):
    row, manifest_hash = load_plan(base, execution_id)
    system = radarr.api('system/status')
    require(system.get('version'), 'Radarr system status is invalid')
    src, dst, logical_src, logical_dst = paths(row)
    filesystem_ready(src, dst)
    movies = radarr.api('movie')
    matches = [m for m in movies if m.get('path') == logical_src]
    require(len(matches) == 1, 'Expected exactly one Radarr movie at source path')
    movie_id = matches[0]['id']
    movie = radarr.api(f'movie/{movie_id}')
    require(movie['path'] == logical_src and movie.get('hasFile'), 'Radarr source changed or has no file')
    for field in ('radarr_id', 'item_id'):
        if row.get(field):
            require(int(row[field]) == movie_id, 'Radarr ID mismatch')
    require(not any(m.get('path') == logical_dst for m in movies), 'Radarr destination already assigned')
    roots = radarr.api('rootfolder')
    target_root = str(PurePosixPath(logical_dst).parent)
    require(any(r.get('path') == target_root and r.get('accessible') is True for r in roots),
            'Destination is not an accessible Radarr root')
    labels = {t['id']: t['label'].lower() for t in radarr.api('tag')}
    tags = {labels.get(t, '') for t in movie.get('tags', [])}
    require(not OVERRIDES.locked(tags), 'Movie now has ' + OVERRIDES.lock_tag)
    require(OVERRIDES.agrees(tags, row['recommended']), 'Current override conflicts')
    relative = movie.get('movieFile', {}).get('relativePath', '')
    rp = PurePosixPath(relative)
    require(relative and not rp.is_absolute() and '..' not in rp.parts, 'Invalid Radarr movie file')
    before = inventory(src)
    require(relative in before and len(before[relative]) == 4 and before[relative][0] > 0, 'Radarr media file missing')
    radarr.verify_file(logical_src + '/' + relative, before[relative][1])
    radarr.visible(target_root, '-d')
    log('PREFLIGHT_OK', manifest_sha256=manifest_hash, source=str(src), destination=str(dst),
        radarr_id=movie_id, radarr_version=system['version'],
        logical_source=logical_src, logical_destination=logical_dst)
    if not live:
        log('CHECK_ONLY', manifest_sha256=manifest_hash)
        return 'CHECK_ONLY'
    # Revalidate authorization and mutable inputs immediately before durable intent.
    require(load_plan(base, execution_id) == (row, manifest_hash), 'Plan/approval changed')
    require(radarr.api(f'movie/{movie_id}') == movie, 'Radarr record changed during preflight')
    filesystem_ready(src, dst)
    require(inventory(src) == before, 'Source changed during preflight')
    log('RENAME_INTENT', inventory=before)
    rename_noreplace(src, dst)
    log('RENAMED')
    sync_parents(src, dst)
    require(not os.path.lexists(src) and inventory(dst) == before, 'Filesystem verification failed')
    radarr.verify_file(logical_dst + '/' + relative, before[relative][1])
    updated = dict(movie, path=logical_dst, rootFolderPath=target_root)
    log('RADARR_UPDATE_INTENT')
    radarr.api(f'movie/{movie_id}?moveFiles=false', updated)
    after = radarr.api(f'movie/{movie_id}')
    require(after.get('id') == movie_id and after.get('path') == logical_dst
            and after.get('rootFolderPath') == target_root
            and after.get('hasFile') and after.get('movieFile', {}).get('id') == movie['movieFile']['id']
            and after.get('movieFile', {}).get('relativePath') == relative,
            'Radarr post-update verification failed')
    require(not os.path.lexists(src) and inventory(dst) == before, 'Final filesystem verification failed')
    radarr.verify_file(logical_dst + '/' + relative, before[relative][1])
    log('SUCCESS', manifest_sha256=manifest_hash)
    return 'SUCCESS'


def check_journal(journal):
    if journal.exists():
        events = [json.loads(line) for line in journal.read_text().splitlines()]
        require(not any(e['event'] in {'RENAME_INTENT', 'SUCCESS'} for e in events),
                'Previous live attempt exists; inspect journal and reconcile manually before retrying')


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
                result = execute(args.base, args.execution_id, args.execute, Radarr(), log)
                print(result + ': ' + args.execution_id)
            except BaseException as exc:
                log('STOPPED', error_type=type(exc).__name__, reason=str(exc))
                print('STOPPED: ' + str(exc), file=sys.stderr)
                print('Inspect journal: ' + str(journal) + '. No automatic rollback or retry.', file=sys.stderr)
                return 1
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (Refused, OSError) as exc:
        sys.exit('STOPPED: ' + str(exc))
