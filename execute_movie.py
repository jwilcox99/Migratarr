#!/usr/bin/env python3
"""One approved same-disk movie rename. Python 3.9+, Linux host only."""
import argparse
import csv
import ctypes
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
from datetime import datetime, timezone
import urllib.request
import xml.etree.ElementTree as ET


class Refused(RuntimeError):
    pass


def require(ok, message):
    if not ok:
        raise Refused(message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def load_plan(base, execution_id):
    require(re.fullmatch(r'\d{8}T\d{6}Z-\d{4,}', execution_id), 'Invalid execution ID')
    run = execution_id.rsplit('-', 1)[0]
    folder = base / 'manifests' / run
    sums = {}
    for line in (folder / 'SHA256SUMS').read_text().splitlines():
        if not line.strip():
            continue
        h, name = line.split(maxsplit=1)
        name = name.lstrip('*')
        require(name not in sums, 'Duplicate checksum entry')
        sums[name] = h
    blobs = {}
    for name in ('execution_manifest.csv', 'manifest_metadata.json'):
        blobs[name] = (folder / name).read_bytes()
        require(digest(blobs[name]) == sums.get(name), 'Checksum mismatch: ' + name)
    metadata = json.loads(blobs['manifest_metadata.json'])
    require(metadata.get('manifest_version') == 1 and metadata.get('run_id') == run and metadata.get('snapshot_verified') is True,
            'Manifest metadata does not identify a verified snapshot')
    raw = blobs['execution_manifest.csv']
    reader = csv.DictReader(io.StringIO(raw.decode('utf-8-sig')))
    headers = reader.fieldnames or []
    required = {'execution_id', 'media_type', 'transfer_type', 'status', 'blockers',
                'executed', 'current', 'recommended', 'source_path', 'target_path',
                'source_disk', 'target_disk'}
    require(required <= set(headers) and len(headers) == len(set(headers)), 'Invalid manifest columns')
    rows = list(reader)
    require(all(None not in r and all(v is not None for v in r.values()) for r in rows),
            'Malformed manifest row')
    ids = [r['execution_id'] for r in rows]
    require(len(ids) == len(set(ids)), 'Duplicate execution IDs')
    selected = [r for r in rows if r['execution_id'] == execution_id]
    require(len(selected) == 1, 'Execution ID absent from frozen manifest')
    row = selected[0]
    approvals = json.loads((base / 'approvals' / (run + '.json')).read_bytes())
    require(isinstance(approvals.get('approved_execution_ids'), list)
            and isinstance(approvals.get('history'), list), 'Invalid approval record')
    require(approvals.get('run_id') == run, 'Approval run mismatch')
    require(execution_id in approvals.get('approved_execution_ids', []), 'Row is unapproved')
    history = [e for e in approvals.get('history', []) if e.get('execution_id') == execution_id]
    require(history and history[-1].get('action') == 'APPROVE'
            and history[-1].get('manifest_sha256') == digest(raw),
            'Latest approval does not approve this exact manifest hash')
    require(row.get('media_type') == 'Movie', 'Only Movie is supported')
    require(row.get('transfer_type') == 'SAME_DISK_RENAME', 'Only SAME_DISK_RENAME is supported')
    require(row.get('status') == 'READY_FOR_REVIEW' and not row.get('blockers'), 'Row is blocked')
    require(row.get('executed') == 'NO', 'Manifest row already executed or invalid')
    return row, digest(raw)


def paths(row):
    result = []
    for field, category, disk_field in [('source_path', 'current', 'source_disk'),
                                         ('target_path', 'recommended', 'target_disk')]:
        raw = row[field]
        p = PurePosixPath(raw)
        require(str(p) == raw and '..' not in p.parts, 'Noncanonical path')
        require(len(p.parts) == 7 and p.parts[:3] == ('/', 'mnt', 'nas')
                and p.parts[3] in {'media01', 'media02', 'media03', 'media04'}
                and p.parts[4] == 'Movies' and p.parts[5] == row[category]
                and p.parts[5] in {'Common', 'Rare', 'Library', 'Archive'}
                and p.parts[3] == row[disk_field], 'Path/category/disk mismatch')
        result.append(Path(raw))
    src, dst = result
    require(src.name == dst.name and src != dst and row['source_disk'] == row['target_disk'],
            'Not a same-disk category rename')
    return src, dst, '/media/Movies/' + row['current'] + '/' + src.name, \
        '/media/Movies/' + row['recommended'] + '/' + dst.name


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
        xml = subprocess.check_output(['docker', 'exec', 'radarr', 'cat', '/config/config.xml'], timeout=30)
        self.key = ET.fromstring(xml).findtext('ApiKey')
        require(self.key, 'Radarr API key missing')
        # Disable redirects so an API key cannot be forwarded to another server.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        self.opener = urllib.request.build_opener(NoRedirect, urllib.request.ProxyHandler({}))

    def api(self, path, body=None):
        req = urllib.request.Request('http://localhost:7878/api/v3/' + path,
                                     data=None if body is None else json.dumps(body).encode(),
                                     headers={'X-Api-Key': self.key, 'Content-Type': 'application/json'},
                                     method='GET' if body is None else 'PUT')
        with self.opener.open(req, timeout=30) as response:
            data = response.read()
            return json.loads(data) if data else None

    def visible(self, path, kind='-f'):
        r = subprocess.run(['docker', 'exec', 'radarr', 'test', kind, path], timeout=30,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        require(r.returncode == 0, 'Radarr container cannot see required path: ' + path)

    def verify_file(self, path, expected_hash):
        self.visible(path)
        result = subprocess.check_output(
            ['docker', 'exec', 'radarr', 'sha256sum', '--', path], timeout=1800, text=True)
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
    require('migratarr-lock' not in tags, 'Movie now has migratarr-lock')
    overrides = tags & {'migratarr-common', 'migratarr-rare', 'migratarr-library', 'migratarr-archive', 'migratarr-current'}
    require(not overrides or overrides == {'migratarr-' + row['recommended'].lower()}, 'Current override conflicts')
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
    parser.add_argument('--base', type=Path, default=Path('/opt/media-stack/migratarr'))
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
