"""One approved same-disk movie rename. Python 3.9+, Linux host only."""
import argparse
import ctypes
import hashlib
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
import time
from datetime import datetime, timezone
import urllib.request
import xml.etree.ElementTree as ET

from runtime_config import get_config
from executor_manifest import digest, load_approved_plan
from executor_nfs import verify_after_rename
from executor_command import run_command
RUNTIME = get_config()


class Refused(RuntimeError):
    pass


def require(ok, message):
    if not ok:
        raise Refused(message)


def load_plan(base, execution_id):
    return load_approved_plan(base, execution_id, 'Movie', 'SAME_DISK_RENAME', require)


def paths(row):
    result = []
    for field, category, disk_field in [('source_path', 'current', 'source_disk'),
                                         ('target_path', 'recommended', 'target_disk')]:
        raw = row[field]
        p = PurePosixPath(raw)
        require(str(p) == raw and '..' not in p.parts, 'Noncanonical path')
        require(len(p.parts) == 7 and p.parts[:3] == RUNTIME.mount_root.parts
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


def progress(message):
    print('[%s] %s' % (datetime.now().strftime('%H:%M:%S'), message),
          file=sys.stderr, flush=True)


def file_metadata(root):
    """Fast local identity check between content verifications; no file reads."""
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
    require(inventory_metadata(found) == initial == file_metadata(root),
            'Directory contents changed while hashing')
    progress('Hash complete: %.2f GiB; elapsed %.0fs' %
             (checked / 2**30, time.monotonic() - started))
    return found


def run_progress(command, label, input=None, timeout=1800):
    return run_command(command, label, input, timeout, require=require, progress=progress)


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
        self.key = ET.fromstring(xml).findtext('ApiKey')
        require(self.key, 'Radarr API key missing')
        # Disable redirects so an API key cannot be forwarded to another server.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        self.opener = urllib.request.build_opener(NoRedirect, urllib.request.ProxyHandler({}))

    def api(self, path, body=None):
        req = urllib.request.Request(RUNTIME.urls["radarr"] + "/api/v3/" + path,
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
        result = run_progress(
            ['docker', 'exec', RUNTIME.containers['radarr'], 'sha256sum', '--', path], 'Radarr file-content verification')
        require(result.split()[0] == expected_hash, 'Radarr-visible file content mismatch')


def verify_nfs_after_rename(src, dst, expected, log, timeout=60, interval=2, metadata_only=False):
    return verify_after_rename(
        src, dst, expected, log, require=require, canonical_existing=canonical_existing,
        file_metadata=file_metadata, inventory=inventory, inventory_metadata=inventory_metadata,
        timeout=timeout, interval=interval, metadata_only=metadata_only)


def execute(base, execution_id, live, radarr, log, transport=None, recovery=None):
    progress('Checking frozen manifest, approval, paths, and Radarr record')
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
    progress('Verifying source contents on the media host')
    before = inventory(src)
    if recovery is not None:
        old, intent = recovery
        require(old['manifest_sha256'] == manifest_hash and old['source'] == str(src)
                and old['destination'] == str(dst) and intent['inventory'] == before,
                'Failed-attempt manifest, paths, or source contents have changed')
    require(relative in before and len(before[relative]) == 4 and before[relative][0] > 0, 'Radarr media file missing')
    radarr.verify_file(logical_src + '/' + relative, before[relative][1])
    radarr.visible(target_root, '-d')
    require(transport is not None, 'NAS transport required')
    transport.call('check', src, dst, before)
    if recovery is not None:
        log('NFS_RECOVERY_VERIFIED', manifest_sha256=manifest_hash)
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
    require(file_metadata(src) == inventory_metadata(before), 'Source changed during preflight')
    log('RENAME_INTENT', inventory=before)
    transport.call('rename', src, dst, before)
    log('RENAMED')
    verify_nfs_after_rename(src, dst, before, log)
    radarr.visible(logical_dst + '/' + relative)
    updated = dict(movie, path=logical_dst, rootFolderPath=target_root)
    log('RADARR_UPDATE_INTENT')
    radarr.api(f'movie/{movie_id}?moveFiles=false', updated)
    after = radarr.api(f'movie/{movie_id}')
    require(after.get('id') == movie_id and after.get('path') == logical_dst
            and after.get('rootFolderPath') == target_root
            and after.get('hasFile') and after.get('movieFile', {}).get('id') == movie['movieFile']['id']
            and after.get('movieFile', {}).get('relativePath') == relative,
            'Radarr post-update verification failed')
    radarr.verify_file(logical_dst + '/' + relative, before[relative][1])
    verify_nfs_after_rename(src, dst, before, log, metadata_only=True)
    log('SUCCESS', manifest_sha256=manifest_hash)
    return 'SUCCESS'


def check_journal(journal, resume=False):
    if journal.exists():
        events = [json.loads(line) for line in journal.read_text().splitlines()]
        if resume:
            intents = [i for i, e in enumerate(events) if e['event'] == 'RENAME_INTENT']
            require(len(intents) == 1, 'Recovery requires exactly one previous rename intent')
            index = intents[0]
            require(index > 0 and index + 1 < len(events), 'Incomplete failed-attempt journal')
            failure = events[index + 1]
            require(failure.get('event') == 'STOPPED' and failure.get('error_type') == 'OSError'
                    and failure.get('reason') == '[Errno 22] Invalid argument',
                    'Recovery only supports the diagnosed NFS EINVAL refusal')
            require(not any(e['event'] in {'RENAMED', 'RADARR_UPDATE_INTENT', 'SUCCESS'} for e in events),
                    'Journal indicates a completed or uncertain mutation')
            # Only successful check-only recovery runs may follow the original failure.
            tail = events[index + 2:]
            allowed = {'START', 'NFS_RECOVERY_VERIFIED', 'PREFLIGHT_OK', 'CHECK_ONLY'}
            require(all(e['event'] in allowed for e in tail)
                    and (not tail or tail[-1]['event'] == 'CHECK_ONLY')
                    and not any(e['event'] == 'START' and e.get('live') is not False for e in tail),
                    'A later attempt needs manual reconciliation')
            prior = [e for e in events[:index] if e['event'] == 'PREFLIGHT_OK']
            require(prior, 'Missing failed-attempt preflight')
            return prior[-1], events[index]
        require(not any(e['event'] in {'RENAME_INTENT', 'SUCCESS'} for e in events),
                'Previous live attempt exists; inspect journal and reconcile manually before retrying')
    require(not resume, 'No failed journal to recover')


def remote_path(path):
    p = PurePosixPath(str(path))
    require(p.parts[:4] == (RUNTIME.mount_root / 'media04').parts and len(p.parts) == 7
            and p.parts[4] == 'Movies' and '..' not in p.parts,
            'NAS transport is configured only for media04 Movies')
    return RUNTIME.remote_disks['media04'] + '/' + '/'.join(p.parts[4:])


def remote_program():
    # Send fixed Python code as a shell-quoted command; paths and inventories travel
    # separately as JSON on stdin, never as interpolated shell syntax.
    imports = 'import ctypes, hashlib, json, os, stat, sys, fcntl, time\nfrom pathlib import Path\nfrom datetime import datetime\n'
    functions = [Refused, require, progress, file_metadata, inventory_metadata,
                 canonical_existing, filesystem_ready, inventory,
                 rename_noreplace, sync_parents]
    return imports + 'REMOTE_ROOT = ' + repr(RUNTIME.remote_disks['media04']) + '\n' + '\n\n'.join(inspect.getsource(f) for f in functions) + '''
def content_only(items):
    return {k: v if len(v) == 1 else v[:2] for k, v in items.items()}

data = json.load(sys.stdin)
require(data['operation'] in {'check', 'rename'}, 'Unknown operation')
src, dst = Path(data['source']), Path(data['destination'])
for p in (src, dst):
    require(len(p.parts) == 6 and p.parts[:4] == (Path(REMOTE_ROOT) / 'Movies').parts
            and p.parts[4] in {'Common', 'Rare', 'Library', 'Archive'}
            and '..' not in p.parts, 'Invalid NAS movie path')
require(src != dst and src.name == dst.name, 'Invalid rename pair')
lockfd = os.open('/tmp/migratarr-movie-' + str(os.getuid()) + '.lock',
                 os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
with os.fdopen(lockfd, 'a') as lock:
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    filesystem_ready(src, dst)
    metadata = file_metadata(src)
    expected_metadata = inventory_metadata(data['inventory'])
    # Inodes differ between NFS and local views: compare names/types/sizes here.
    require({k: v[:1] for k, v in metadata.items()} ==
            {k: v[:1] for k, v in expected_metadata.items()}, 'NAS source layout mismatch')
    # Check directory syncing on the actual parents before any rename.
    sync_parents(src, dst)
    if data['operation'] == 'rename':
        before = inventory(src)
        require(content_only(before) == content_only(data['inventory']), 'NAS source content mismatch')
        rename_noreplace(src, dst)
        sync_parents(src, dst)
        require(not os.path.lexists(src) and file_metadata(dst) == inventory_metadata(before),
                'NAS post-rename mismatch')
    print(json.dumps({'result': 'OK', 'operation': data['operation']}))
'''


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
            # Dedicated key only; never wait for a terminal password prompt.
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

    def call(self, operation, src, dst, before):
        payload = dict(operation=operation, source=remote_path(src), destination=remote_path(dst),
                       inventory=before)
        result = run_progress(['ssh', *self.options, '-o', 'BatchMode=yes', RUNTIME.ssh_target,
                                 shlex.quote(RUNTIME.remote_python) + ' -c ' + shlex.quote(remote_program())],
                              'NAS ' + operation, input=json.dumps(payload))
        require(json.loads(result) == dict(result='OK', operation=operation),
                'Unexpected NAS response; reconcile before retrying')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('execution_id')
    parser.add_argument('--base', type=Path, default=RUNTIME.base_path)
    parser.add_argument('--execute', action='store_true', help='Perform the approved move; default is check only')
    parser.add_argument('--resume-nfs-refusal', action='store_true',
                        help='Reconcile the single diagnosed NFS EINVAL attempt; retain its journal')
    args = parser.parse_args()
    require(sys.platform.startswith('linux'), 'Run on the Linux media host')
    import fcntl
    require(re.fullmatch(r'\d{8}T\d{6}Z-\d{4,}', args.execution_id), 'Invalid execution ID')
    logs = args.base / 'execution_logs'
    logs.mkdir(exist_ok=True)
    with (logs / 'executor.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        journal = logs / (args.execution_id + '.jsonl')
        recovery = check_journal(journal, args.resume_nfs_refusal)
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
                                     transport, recovery)
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
