"""One approved same-disk TV series rename. Python 3.9+, Linux host only."""
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
import time
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
    require(row.get('media_type') == 'TV', 'Only TV is supported')
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
                and p.parts[4] == 'TV' and p.parts[5] == row[category]
                and p.parts[5] in {'Current', 'Rare', 'Library', 'Archive'}
                and p.parts[3] == row[disk_field], 'Path/category/disk mismatch')
        result.append(Path(raw))
    src, dst = result
    require(src.name == dst.name and src != dst and row['source_disk'] == row['target_disk'],
            'Not a same-disk category rename')
    return src, dst, '/media/TV/' + row['current'] + '/' + src.name, \
        '/media/TV/' + row['recommended'] + '/' + dst.name


def canonical_existing(p):
    require(p.resolve(strict=True) == p, 'Symlink or noncanonical filesystem path: ' + str(p))


def filesystem_ready(src, dst):
    canonical_existing(src)
    canonical_existing(dst.parent)
    require(src.is_dir() and dst.parent.is_dir(), 'Missing source or destination parent')
    require(not os.path.lexists(dst), 'Destination already exists')
    require(src.stat().st_dev == dst.parent.stat().st_dev, 'Different filesystem devices')


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


def run_progress(command, label, input=None, timeout=1800):
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


class Sonarr:
    def __init__(self):
        xml = subprocess.check_output(['docker', 'exec', 'sonarr', 'cat', '/config/config.xml'], timeout=30)
        config = ET.fromstring(xml)
        self.key = config.findtext('ApiKey')
        self.url_base = (config.findtext('UrlBase') or '').rstrip('/')
        require(self.key, 'Sonarr API key missing')
        # Disable redirects so an API key cannot be forwarded to another server.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        self.opener = urllib.request.build_opener(NoRedirect, urllib.request.ProxyHandler({}))

    def api(self, path, body=None):
        req = urllib.request.Request('http://localhost:8989' + self.url_base + '/api/v3/' + path,
                                     data=None if body is None else json.dumps(body).encode(),
                                     headers={'X-Api-Key': self.key, 'Content-Type': 'application/json'},
                                     method='GET' if body is None else 'PUT')
        with self.opener.open(req, timeout=30) as response:
            data = response.read()
            return json.loads(data) if data else None

    def visible(self, path, kind='-f'):
        r = subprocess.run(['docker', 'exec', 'sonarr', 'test', kind, path], timeout=30,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        require(r.returncode == 0, 'Sonarr container cannot see required path: ' + path)

    def verify_file(self, path, expected_hash):
        self.visible(path)
        result = run_progress(['docker', 'exec', 'sonarr', 'sha256sum', '--', path],
                              'Sonarr file-content verification')
        require(result.split()[0] == expected_hash, 'Sonarr-visible file content mismatch')


def verify_nfs_after_rename(src, dst, expected, log, timeout=60, interval=2, metadata_only=False):
    """Retry stale visibility only. Never repeat the rename or Sonarr update.

    The deadline bounds retry scheduling, not the duration of filesystem I/O.
    A readable but different inventory is an immediate failure.
    """
    deadline = time.monotonic() + timeout
    attempts = 0
    while True:
        attempts += 1
        try:
            try:
                src.lstat()
                source_present = True
            except FileNotFoundError:
                source_present = False
            if source_present:
                reason = 'Source path is still visible through NFS'
            else:
                canonical_existing(dst)
                require(dst.is_dir(), 'NFS destination is not a directory')
                actual = file_metadata(dst) if metadata_only else inventory(dst)
                wanted = inventory_metadata(expected) if metadata_only else expected
                require(actual == wanted, 'NFS destination inventory mismatch')
                if attempts > 1:
                    log('NFS_VISIBILITY_READY', attempts=attempts)
                return
        except OSError as exc:
            if exc.errno not in {errno.ENOENT, errno.ESTALE}:
                raise
            reason = 'NFS destination missing or stale: ' + str(exc)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise Refused('NFS visibility verification timed out: ' + reason)
        if attempts == 1:
            log('NFS_VISIBILITY_WAIT', reason=reason, retry_window_seconds=timeout)
        time.sleep(min(interval, remaining))


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


def execute(base, execution_id, live, sonarr, log, transport=None):
    progress('Checking manifest, approval, and Sonarr series')
    row, manifest_hash = load_plan(base, execution_id)
    src, dst, logical_src, logical_dst = paths(row)
    # Validate the configured NAS mapping even for a check-only run.
    remote_path(src)
    remote_path(dst)
    filesystem_ready(src, dst)
    system = sonarr.api('system/status')
    require(str(system.get('version', '')).startswith('4.'), 'Only Sonarr v4 is supported')
    all_series = sonarr.api('series')
    matches = [x for x in all_series if x.get('path') == logical_src]
    require(len(matches) == 1, 'Expected exactly one Sonarr series at source')
    series_id = matches[0]['id']
    series = sonarr.api(f'series/{series_id}')
    require(series.get('id') == series_id and series.get('path') == logical_src,
            'Sonarr source changed')
    for field in ('sonarr_id', 'item_id'):
        if row.get(field):
            require(int(row[field]) == series_id, 'Sonarr ID mismatch')
    require(not any(x.get('path') == logical_dst for x in all_series), 'Destination already assigned')
    target_root = str(PurePosixPath(logical_dst).parent)
    require(any(x.get('path') == target_root and x.get('accessible') is True
                for x in sonarr.api('rootfolder')), 'Destination root unavailable')
    labels = {x['id']: x['label'].lower() for x in sonarr.api('tag')}
    tags = {labels.get(x, '') for x in series.get('tags', [])}
    require('migratarr-lock' not in tags, 'Series has migratarr-lock')
    overrides = tags & {'migratarr-current', 'migratarr-library', 'migratarr-rare',
                        'migratarr-archive', 'migratarr-common'}
    require(not overrides or overrides == {'migratarr-' + row['recommended'].lower()},
            'Current override conflicts')
    before = inventory(src)
    records, associations = episode_state(sonarr, series_id, logical_src, before)
    verify_episode_contents(sonarr, logical_src, records, before)
    sonarr.visible(target_root, '-d')
    require(transport is not None, 'NAS transport required')
    transport.call('check', src, dst, before)
    log('PREFLIGHT_OK', manifest_sha256=manifest_hash, source=str(src), destination=str(dst),
        sonarr_id=series_id, sonarr_version=system['version'], logical_source=logical_src,
        logical_destination=logical_dst, episode_file_count=len(records),
        linked_episode_count=sum(bool(v[2]) for v in associations.values()))
    if not live:
        log('CHECK_ONLY', manifest_sha256=manifest_hash)
        return 'CHECK_ONLY'
    require(load_plan(base, execution_id) == (row, manifest_hash), 'Plan or approval changed')
    require(sonarr.api(f'series/{series_id}') == series, 'Sonarr series changed during preflight')
    require(episode_state(sonarr, series_id, logical_src, before) == (records, associations),
            'Episode files or associations changed during preflight')
    filesystem_ready(src, dst)
    require(file_metadata(src) == inventory_metadata(before), 'Source changed during preflight')
    log('RENAME_INTENT', inventory=before, episode_files=records, episode_associations=associations)
    transport.call('rename', src, dst, before)
    log('RENAMED')
    verify_nfs_after_rename(src, dst, before, log)
    for relative, size in records.values():
        sonarr.visible(logical_dst + '/' + relative)
    # Do not overwrite a Sonarr edit made while the NAS was verifying the rename.
    require(sonarr.api(f'series/{series_id}') == series, 'Sonarr changed before path update')
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
    verify_nfs_after_rename(src, dst, before, log, metadata_only=True)
    require(episode_state(sonarr, series_id, logical_dst, before) == (records, associations),
            'Episode associations changed during final verification')
    final = sonarr.api(f'series/{series_id}')
    require(final.get('path') == logical_dst and final.get('rootFolderPath') == target_root,
            'Sonarr path changed during final verification')
    log('SUCCESS', manifest_sha256=manifest_hash, episode_file_count=len(records),
        linked_episode_count=sum(bool(v[2]) for v in associations.values()))
    return 'SUCCESS'


def check_journal(journal):
    if journal.exists():
        events = [json.loads(line) for line in journal.read_text().splitlines()]
        require(not any(e['event'] in {'RENAME_INTENT', 'RENAMED', 'SONARR_UPDATE_INTENT', 'SUCCESS'}
                        for e in events), 'Previous live attempt exists; reconcile manually before retrying')


def remote_path(path):
    p = PurePosixPath(str(path))
    require(p.parts[:4] == ('/', 'mnt', 'nas', 'media04') and len(p.parts) == 7
            and p.parts[4] == 'TV' and '..' not in p.parts,
            'NAS transport is configured only for media04 TV')
    return '/volume3/media04/' + '/'.join(p.parts[4:])


def remote_program():
    # Send fixed Python code as a shell-quoted command; paths and inventories travel
    # separately as JSON on stdin, never as interpolated shell syntax.
    imports = 'import ctypes, hashlib, json, os, stat, sys, fcntl, time\nfrom pathlib import Path\nfrom datetime import datetime\n'
    functions = [Refused, require, progress, file_metadata, inventory_metadata,
                 canonical_existing, filesystem_ready, inventory,
                 rename_noreplace, sync_parents]
    return imports + '\n\n'.join(inspect.getsource(f) for f in functions) + '''
def content_only(items):
    return {k: v if len(v) == 1 else v[:2] for k, v in items.items()}

data = json.load(sys.stdin)
require(data['operation'] in {'check', 'rename'}, 'Unknown operation')
src, dst = Path(data['source']), Path(data['destination'])
for p in (src, dst):
    require(len(p.parts) == 6 and p.parts[:4] == ('/', 'volume3', 'media04', 'TV')
            and p.parts[4] in {'Current', 'Rare', 'Library', 'Archive'}
            and '..' not in p.parts, 'Invalid NAS TV path')
require(src != dst and src.name == dst.name, 'Invalid rename pair')
lockfd = os.open('/tmp/migratarr-movie-' + str(os.getuid()) + '.lock',
                 os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
with os.fdopen(lockfd, 'a') as lock:
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    filesystem_ready(src, dst)
    metadata = file_metadata(src)
    expected_metadata = inventory_metadata(data['inventory'])
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
        self.options = ['-i', str(Path.home() / '.ssh' / 'migratarr_nas'),
                        '-o', 'IdentitiesOnly=yes',
                        '-o', 'BatchMode=yes',
                        '-o', 'StrictHostKeyChecking=yes', '-o', 'ConnectTimeout=15',
                        '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=3',
                        '-o', 'ControlPath=' + self.temp.name + '/socket']
        try:
            # Prompt once through the terminal; no credentials are stored by Python.
            subprocess.run(['ssh', *self.options, '-M', '-N', '-f',
                            '-o', 'ControlPersist=yes', 'jwilc@10.0.0.126'], check=True, timeout=120)
        except BaseException:
            self.temp.cleanup()
            raise
        return self

    def __exit__(self, *exc):
        try:
            subprocess.run(['ssh', *self.options, '-O', 'exit', 'jwilc@10.0.0.126'],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
        finally:
            self.temp.cleanup()

    def call(self, operation, src, dst, before):
        payload = dict(operation=operation, source=remote_path(src), destination=remote_path(dst),
                       inventory=before)
        result = run_progress(['ssh', *self.options, '-o', 'BatchMode=yes', 'jwilc@10.0.0.126',
                                 '/usr/bin/python3 -c ' + shlex.quote(remote_program())],
                              'NAS ' + operation, input=json.dumps(payload))
        require(json.loads(result) == dict(result='OK', operation=operation),
                'Unexpected NAS response; reconcile before retrying')


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


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (Refused, OSError) as exc:
        sys.exit('STOPPED: ' + str(exc))
