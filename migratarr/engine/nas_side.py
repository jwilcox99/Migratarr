"""Code that runs on the NAS, shared by the executors.

The executors ship these functions to the NAS as source text
(inspect.getsource), together with their own helpers, as one program run by
the NAS's python3. So this module must stay standard-library only, import
nothing from the project, and never be imported for side effects: names a
function uses but doesn't define here (inventory, nas_pair, NAS_LAYOUT,
scan_metadata, progress, ...) are supplied by the program it is shipped in, or,
when called on the media host, by the executor it is bound into
(migratarr.engine.primitives.bind).

The executors' tests pin every shipped program byte for byte
(tests/test_nas_program_parity.py), so a change here is a change to what runs
on the NAS and needs its own review.
"""
import ctypes
import os
import shutil
import stat
from pathlib import PurePosixPath


class Refused(RuntimeError):
    pass


def require(ok, message):
    if not ok:
        raise Refused(message)


def canonical_existing(p):
    require(p.resolve(strict=True) == p, 'Symlink or noncanonical filesystem path: ' + str(p))


def file_metadata(root):
    return scan_metadata(root, require)


def inventory_metadata(items):
    return metadata_from_inventory(items)


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


def filesystem_ready(src, dst):
    canonical_existing(src)
    canonical_existing(dst.parent)
    require(src.is_dir() and dst.parent.is_dir(), 'Missing source or destination parent')
    require(not os.path.lexists(dst), 'Destination already exists')
    require(src.stat().st_dev == dst.parent.stat().st_dev, 'Different filesystem devices')


def content_only(items):
    return {k: v if len(v) == 1 else v[:2] for k, v in items.items()}


def copy_tree(src, stage, expected):
    """Copy regular files only; exclusive creation, no symlink traversal fallback.

    Files and folders keep the source's permission bits, so the Arrs can still
    write to moved media. Folders stay private (0700) while being filled and get
    their source mode at the end, deepest first.
    """
    stage.mkdir(mode=0o700)
    for name, info in sorted(expected.items(), key=lambda x: (len(PurePosixPath(x[0]).parts), x[0])):
        target = stage / name
        if len(info) == 1:
            target.mkdir(mode=0o700)
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
        s = os.lstat(src / folder.relative_to(stage))
        require(stat.S_ISDIR(s.st_mode), 'Source changed before copy')
        os.chmod(folder, stat.S_IMODE(s.st_mode))
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
    src, dst = nas_pair(data['source'], data['destination'], NAS_LAYOUT)
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
