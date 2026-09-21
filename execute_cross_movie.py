"""One approved cross-disk movie transfer; check-only by default."""
import argparse

import csv

import ctypes

import errno

import hashlib

import io

import inspect

import json

import os

from pathlib import Path, PurePosixPath

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

import xml.etree.ElementTree as ET

from runtime_config import get_config
RUNTIME = get_config()

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
    require(row.get('transfer_type') == 'CROSS_DISK_TRANSFER', 'Only CROSS_DISK_TRANSFER is supported')
    require(row.get('status') == 'READY_FOR_REVIEW' and not row.get('blockers'), 'Row is blocked')
    require(row.get('executed') == 'NO', 'Manifest row already executed or invalid')
    return row, digest(raw)

def canonical_existing(p):
    require(p.resolve(strict=True) == p, 'Symlink or noncanonical filesystem path: ' + str(p))

def progress(message):
    print('[%s] %s' % (datetime.now().strftime('%H:%M:%S'), message), file=sys.stderr, flush=True)

def file_metadata(root):
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
                found[key] = [s.st_size, s.st_ino, s.st_mtime_ns]
    visit(root)
    return found

def inventory_metadata(items):
    return {k: v if len(v) == 1 else [v[0], v[2], v[3]] for k, v in items.items()}

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

def run_progress(command, label, input=None, timeout=7200):
    started = time.monotonic()
    progress(label + ' started')
    with subprocess.Popen(command, stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as process:
        try:
            while True:
                remaining = timeout - (time.monotonic() - started)
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(command, timeout)
                try:
                    stdout, stderr = process.communicate(input=input, timeout=min(15, remaining))
                    break
                except subprocess.TimeoutExpired:
                    input = None
                    progress('%s still running; elapsed %.0fs' % (label, time.monotonic() - started))
        except BaseException:
            process.kill()
            process.communicate()
            raise
        require(process.returncode == 0, label + ' failed or is uncertain: ' + stderr.strip())
    progress('%s complete; elapsed %.0fs' % (label, time.monotonic() - started))
    return stdout

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
        xml = subprocess.check_output(['docker', 'exec', RUNTIME.containers['radarr'], 'cat', '/config/config.xml'], timeout=30)
        config = ET.fromstring(xml)
        self.key = config.findtext('ApiKey')
        self.url_base = (config.findtext('UrlBase') or '').rstrip('/')
        require(self.key, 'Radarr API key missing')
        # Disable redirects so an API key cannot be forwarded to another server.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        self.opener = urllib.request.build_opener(NoRedirect, urllib.request.ProxyHandler({}))

    def api(self, path, body=None):
        req = urllib.request.Request(RUNTIME.urls["radarr"] + self.url_base + '/api/v3/' + path,
                                     data=None if body is None else json.dumps(body).encode(),
                                     headers={'X-Api-Key': self.key, 'Content-Type': 'application/json'},
                                     method='GET' if body is None else 'PUT')
        with self.opener.open(req, timeout=30) as response:
            data = response.read()
            return json.loads(data) if data else None

    def visible(self, path, kind='-f'):
        r = subprocess.run(['docker', 'exec', RUNTIME.containers['radarr'], 'test', kind, path], timeout=30,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        require(r.returncode == 0, 'Radarr container cannot see required path: ' + path)

    def verify_file(self, path, expected_hash):
        self.visible(path)
        result = run_progress(['docker', 'exec', RUNTIME.containers['radarr'], 'sha256sum', '--', path],
                              'Radarr file-content verification')
        require(result.split()[0] == expected_hash, 'Radarr-visible file content mismatch')

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
                              'NAS ' + operation, input=json.dumps(payload))
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


DISKS = RUNTIME.remote_disks


def paths(row):
    result = []
    for field, category, disk in [('source_path', 'current', 'source_disk'),
                                  ('target_path', 'recommended', 'target_disk')]:
        raw = row[field]
        p = PurePosixPath(raw)
        require(str(p) == raw and '..' not in p.parts and len(p.parts) == 7
                and p.parts[:3] == RUNTIME.mount_root.parts and p.parts[3] in DISKS
                and p.parts[3] == row[disk] and p.parts[4] == 'Movies'
                and p.parts[5] == row[category]
                and row[category] in {'Common', 'Rare', 'Library', 'Archive'}, 'Invalid manifest path')
        result.append(Path(raw))
    src, dst = result
    require(src.name == dst.name and row['source_disk'] != row['target_disk'],
            'Expected distinct disks and unchanged movie folder name')
    return src, dst, '/media/Movies/' + row['current'] + '/' + src.name, \
        '/media/Movies/' + row['recommended'] + '/' + dst.name


def remote_path(path):
    p = PurePosixPath(str(path))
    require(len(p.parts) == 7 and p.parts[:3] == RUNTIME.mount_root.parts
            and p.parts[3] in DISKS and p.parts[4] == 'Movies' and '..' not in p.parts,
            'Invalid NAS mapping')
    return DISKS[p.parts[3]] + '/' + '/'.join(p.parts[4:])


def content_only(items):
    return {k: v if len(v) == 1 else v[:2] for k, v in items.items()}


def check_journal(journal):
    if journal.exists():
        events = [json.loads(line) for line in journal.read_text().splitlines()]
        blocked = {'COPY_INTENT', 'COPIED', 'RADARR_UPDATE_INTENT', 'DELETE_INTENT',
                   'SOURCE_REMOVED', 'SUCCESS', 'RENAME_INTENT'}
        require(not any(e['event'] in blocked for e in events),
                'Previous live attempt exists; reconcile manually before retrying')


def verify_destination(dst, expected, timeout=60):
    deadline = time.monotonic() + timeout
    while True:
        try:
            canonical_existing(dst)
            actual = inventory(dst)
            require(content_only(actual) == content_only(expected), 'Destination content mismatch')
            return actual
        except OSError as exc:
            if exc.errno not in {errno.ENOENT, errno.ESTALE}:
                raise
            require(time.monotonic() < deadline, 'Destination visibility timed out')
            progress('Waiting for NFS destination visibility')
            time.sleep(2)


def wait_source_absent(src, timeout=60):
    deadline = time.monotonic() + timeout
    while True:
        try:
            src.lstat()
        except FileNotFoundError:
            return
        require(time.monotonic() < deadline, 'Source still visible after NAS deletion')
        time.sleep(2)


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
    require('migratarr-lock' not in tags, 'Movie is locked')
    overrides = tags & {'migratarr-common', 'migratarr-rare', 'migratarr-library', 'migratarr-archive', 'migratarr-current'}
    require(not overrides or overrides == {'migratarr-' + row['recommended'].lower()}, 'Conflicting override')
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


def copy_tree(src, stage, expected):
    """Copy regular files only; exclusive creation, no symlink traversal fallback."""
    stage.mkdir(mode=0o755)
    for name, info in sorted(expected.items(), key=lambda x: (len(PurePosixPath(x[0]).parts), x[0])):
        target = stage / name
        if len(info) == 1:
            target.mkdir(mode=0o755)
            continue
        source = src / name
        fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as reader, target.open('xb') as writer:
            s = os.fstat(reader.fileno())
            require(stat.S_ISREG(s.st_mode) and [s.st_size, s.st_ino, s.st_mtime_ns] ==
                    [info[0], info[2], info[3]], 'Source changed before copy')
            progress('Copying ' + name)
            shutil.copyfileobj(reader, writer, 1024 * 1024)
            writer.flush()
            os.fsync(writer.fileno())
        os.chmod(target, stat.S_IMODE(s.st_mode))
        os.utime(target, ns=(s.st_atime_ns, s.st_mtime_ns))
        with target.open('rb') as copied:
            os.fsync(copied.fileno())
    for folder in sorted([stage] + [stage / n for n, v in expected.items() if len(v) == 1],
                         key=lambda p: len(p.parts), reverse=True):
        fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def remove_verified_tree(root, expected):
    """Only remove known verified entries. New entries make rmdir fail, never recurse blindly."""
    require(inventory(root) == expected, 'Quarantined source changed; do not delete')
    for name, info in expected.items():
        if len(info) == 1:
            continue
        p = root / name
        canonical_existing(p)
        s = p.lstat()
        require(stat.S_ISREG(s.st_mode) and [s.st_size, s.st_ino, s.st_mtime_ns] ==
                [info[0], info[2], info[3]], 'File changed before unlink')
        p.unlink()
    for name, info in sorted(expected.items(), key=lambda x: len(PurePosixPath(x[0]).parts), reverse=True):
        if len(info) == 1:
            (root / name).rmdir()
    root.rmdir()


def nas_operation(data):
    operation = data['operation']
    require(operation in {'check', 'copy', 'delete'}, 'Unknown operation')
    execution_id = data['execution_id']
    require(re.fullmatch(r'\d{8}T\d{6}Z-\d{4,}', execution_id), 'Invalid execution ID')
    src, dst = Path(data['source']), Path(data['destination'])
    for p in (src, dst):
        require(len(p.parts) == 6 and str(Path(*p.parts[:3])) in DISKS.values()
                and p.parts[3] == 'Movies' and p.parts[4] in {'Common', 'Rare', 'Library', 'Archive'}
                and '..' not in p.parts, 'Invalid NAS movie path')
    require(src.name == dst.name and src.parts[:3] != dst.parts[:3], 'Not a cross-disk pair')
    canonical_existing(src)
    canonical_existing(dst.parent)
    require(src.is_dir() and dst.parent.is_dir(), 'Missing source or target parent')
    require(src.stat().st_dev != dst.parent.stat().st_dev, 'NAS filesystems are not distinct')
    stage = dst.parent / ('.migratarr-stage-' + execution_id)
    quarantine = src.parent / ('.migratarr-delete-' + execution_id)
    require(not os.path.lexists(stage) and not os.path.lexists(quarantine), 'Previous work directory exists')
    expected = data['inventory']
    source = inventory(src)
    require(content_only(source) == content_only(expected), 'NAS source content mismatch')
    source_id = [src.stat().st_dev, src.stat().st_ino]
    if operation in {'check', 'copy'}:
        require(not os.path.lexists(dst), 'Destination exists')
        needed = sum(v[0] for v in source.values() if len(v) == 4)
        require(shutil.disk_usage(dst.parent).free >= needed + 1024**3, 'Insufficient destination space including 1 GiB reserve')
        sync_parents(src, dst)
        if operation == 'copy':
            copy_tree(src, stage, source)
            require(content_only(inventory(stage)) == content_only(source), 'Staged copy verification failed')
            require(inventory(src) == source, 'Source changed during copy')
            rename_noreplace(stage, dst)
            sync_parents(src, dst)
            require(content_only(inventory(dst)) == content_only(source), 'Published copy verification failed')
        return dict(result='OK', operation=operation, source_inventory=source, source_id=source_id)
    receipt = data['receipt']
    require(receipt.get('result') == 'OK' and receipt.get('operation') == 'copy'
            and receipt.get('source_id') == source_id and receipt.get('source_inventory') == source,
            'Source identity differs from copied source')
    canonical_existing(dst)
    require(content_only(inventory(dst)) == content_only(source), 'Destination differs; source retained')
    # Make source unavailable to new ordinary path writers before deleting verified entries.
    rename_noreplace(src, quarantine)
    sync_parents(src, quarantine)
    require([quarantine.stat().st_dev, quarantine.stat().st_ino] == source_id, 'Source directory identity changed')
    remove_verified_tree(quarantine, source)
    sync_parents(src, dst)
    require(not os.path.lexists(src) and not os.path.lexists(quarantine), 'Source removal incomplete')
    require(content_only(inventory(dst)) == content_only(source), 'Final NAS destination mismatch')
    return dict(result='OK', operation=operation)


def remote_program():
    imports = ('import ctypes, hashlib, json, os, stat, sys, fcntl, time, shutil, re\n'
               'from pathlib import Path, PurePosixPath\nfrom datetime import datetime\n')
    functions = [Refused, require, progress, canonical_existing, file_metadata, inventory_metadata,
                 inventory, rename_noreplace, sync_parents, content_only, copy_tree,
                 remove_verified_tree, nas_operation]
    code = imports + 'DISKS = ' + repr(DISKS) + '\n\n'
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
