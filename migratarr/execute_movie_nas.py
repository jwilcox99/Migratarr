"""One approved same-disk movie rename. Python 3.9+, Linux host only."""
import argparse
import ctypes
import hashlib
import inspect
import json
import os
from pathlib import Path, PurePath
import stat
import subprocess  # patched by tests through this module; the Arr client uses it
import sys
import time
from datetime import datetime

from migratarr.runtime_config import get_config
from migratarr.planner_settings import get_settings
from migratarr.media_layout import MediaLayout, canonical, get_targets, split_media_path
from migratarr.executor_manifest import digest, load_approved_plan  # digest: batch_cross_*.py use m.digest
from migratarr.executor_nfs import verify_after_rename
from migratarr.executor_command import NAS_IDLE_TIMEOUT, run_command
from migratarr.executor_inventory import scan_metadata, metadata_from_inventory
from migratarr.engine import nas_side, primitives, run, transport

# Shared code, bound into this module so it resolves names (inventory, nas_pair,
# RUNTIME, ...) here, exactly as when it was defined in this file.
primitives.bind(globals(), nas_side.Refused, nas_side.require, nas_side.canonical_existing, nas_side.file_metadata, nas_side.inventory_metadata, nas_side.rename_noreplace, nas_side.sync_parents, nas_side.filesystem_ready,
                primitives.layout, primitives.posix, primitives.remote_path, primitives.verify_nfs_after_rename)
RUNTIME = get_config()
OVERRIDES = get_settings().overrides
TARGETS = get_targets()


MEDIA, TRANSFER = 'Movie', 'SAME_DISK_RENAME'
# execute(), check_journal() and the Radarr client come from the engine
# (migratarr/engine: adapters RadarrMovie, strategy SameDiskRename, run()).
NS = run.wire(globals(), MEDIA, TRANSFER)


def load_plan(base, execution_id):
    return load_approved_plan(base, execution_id, MEDIA, TRANSFER, require)


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


def progress(message):
    print('[%s] %s' % (datetime.now().strftime('%H:%M:%S'), message),
          file=sys.stderr, flush=True)


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


def run_progress(command, label, input=None, timeout=1800, idle_timeout=None):
    return run_command(command, label, input, timeout, require=require, progress=progress,
                       idle_timeout=idle_timeout)


def remote_layout(disk):
    """One disk's NAS root and the Movie category folders; shipped with the program."""
    return {disk: layout().remote_roots[disk]}, layout().category_dirs[MEDIA]


def nas_pair(source, destination, nas_layout):
    roots, category_dirs = nas_layout
    for p in (source, destination):
        require(split_media_path(p, roots, category_dirs) is not None, 'Invalid NAS movie path')
    src, dst = Path(source), Path(destination)
    require(src != dst and src.name == dst.name, 'Invalid rename pair')
    return src, dst


def remote_program(disk):
    # Send fixed Python code as a shell-quoted command; paths and inventories travel
    # separately as JSON on stdin, never as interpolated shell syntax.
    imports = 'import ctypes, hashlib, json, os, stat, sys, fcntl, time\nfrom pathlib import Path, PurePosixPath\nfrom datetime import datetime\n'
    functions = [scan_metadata, metadata_from_inventory, Refused, require, progress, split_media_path, nas_pair,
                 file_metadata, inventory_metadata,
                 canonical_existing, filesystem_ready, inventory,
                 rename_noreplace, sync_parents]
    return imports + 'NAS_LAYOUT = ' + repr(remote_layout(disk)) + '\n' + '\n\n'.join(inspect.getsource(f) for f in functions) + '''
def content_only(items):
    return {k: v if len(v) == 1 else v[:2] for k, v in items.items()}

data = json.load(sys.stdin)
require(data['operation'] in {'check', 'rename'}, 'Unknown operation')
src, dst = nas_pair(data['source'], data['destination'], NAS_LAYOUT)
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


class NasTransport(transport.NasChannel):
    """The NAS program's channel (migratarr/engine/transport.py): SSH, or this host for topology local."""
    runtime = property(lambda self: RUNTIME)

    def call(self, operation, src, dst, before):
        found = [layout().parse_local(posix(p), MEDIA) for p in (src, dst)]
        require(None not in found and found[0][0] == found[1][0], 'Source and destination disks differ')
        disk = found[0][0]
        payload = dict(operation=operation, source=remote_path(src), destination=remote_path(dst),
                       inventory=before)
        result = run_progress(self.command(remote_program(disk)),
                              'NAS ' + operation, input=json.dumps(payload),
                              timeout=None, idle_timeout=NAS_IDLE_TIMEOUT)
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
    return run.main(NS, args, MEDIA, TRANSFER, resume=args.resume_nfs_refusal)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (Refused, OSError) as exc:
        sys.exit('STOPPED: ' + str(exc))
