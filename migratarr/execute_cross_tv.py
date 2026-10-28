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
import subprocess  # patched by tests through this module; the Arr client uses it
import sys
import shutil
import time
from datetime import datetime

from migratarr.runtime_config import get_config
from migratarr.planner_settings import get_settings
from migratarr.media_layout import MediaLayout, canonical, get_targets, split_media_path
from migratarr.executor_manifest import digest, load_approved_plan  # digest: batch_cross_*.py use m.digest
from migratarr.executor_command import NAS_IDLE_TIMEOUT, run_command
from migratarr.executor_inventory import scan_metadata, metadata_from_inventory
from migratarr.engine import nas_side, primitives, run, transport

# Shared code, bound into this module so it resolves names (inventory, nas_pair,
# RUNTIME, ...) here, exactly as when it was defined in this file.
primitives.bind(globals(), nas_side.Refused, nas_side.require, nas_side.canonical_existing, nas_side.file_metadata, nas_side.inventory_metadata, nas_side.rename_noreplace, nas_side.sync_parents, nas_side.content_only, nas_side.copy_tree, nas_side.remove_verified_tree, nas_side.nas_operation,
                primitives.layout, primitives.posix, primitives.remote_path, primitives.verify_destination, primitives.wait_source_absent)
RUNTIME = get_config()
OVERRIDES = get_settings().overrides
TARGETS = get_targets()
MEDIA, TRANSFER = 'TV', 'CROSS_DISK_TRANSFER'
# execute(), check_journal(), the Sonarr client, episode_state() and
# verify_episode_contents() come from the engine (migratarr/engine: adapter
# SonarrSeries, strategy CrossDiskCopy, run()).
NS = run.wire(globals(), MEDIA, TRANSFER)


def load_plan(base, execution_id):
    return load_approved_plan(base, execution_id, MEDIA, TRANSFER, require)


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
    return run.main(NS, args, MEDIA, TRANSFER)




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
