"""Host-side helpers shared by the executors, and the binding step that installs them.

bind() gives an executor its own copy of each shared function, resolving names
in the executor's globals exactly as if the function's text were still pasted
into that file. That matters because the shared functions call per-executor
helpers that still differ (inventory, nas_pair, progress, RUNTIME, ...): on the
NAS they meet those helpers in one shipped program, and on the media host
(and in the safety tests, which patch executor globals) they must see the
executor's versions, not a single module-wide copy.
"""
import errno
import os
import time
import types
from pathlib import Path, PurePath, PurePosixPath


def bind(namespace, *objects):
    """Install `objects` into `namespace`; functions are rebound to its globals."""
    for obj in objects:
        if isinstance(obj, types.FunctionType):
            bound = types.FunctionType(obj.__code__, namespace, obj.__name__, obj.__defaults__, obj.__closure__)
            bound.__kwdefaults__ = obj.__kwdefaults__
            bound.__qualname__ = obj.__qualname__
            bound.__doc__ = obj.__doc__
            bound.__dict__.update(obj.__dict__)
            namespace[obj.__name__] = bound
        else:  # classes (Refused) are shared as they are
            namespace[obj.__name__] = obj


def layout():
    # Built per call from RUNTIME, so a patched or reloaded runtime is honored.
    return MediaLayout(RUNTIME, TARGETS)


def posix(path):
    return path.as_posix() if isinstance(path, PurePath) else str(path)


def remote_path(path):
    raw = posix(path)
    require(layout().parse_local(raw, MEDIA) is not None, 'Invalid NAS mapping')
    return layout().to_remote(raw, MEDIA)


def verify_nfs_after_rename(src, dst, expected, log, timeout=60, interval=2, metadata_only=False):
    return verify_after_rename(
        src, dst, expected, log, require=require, canonical_existing=canonical_existing,
        file_metadata=file_metadata, inventory=inventory, inventory_metadata=inventory_metadata,
        timeout=timeout, interval=interval, metadata_only=metadata_only)


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
