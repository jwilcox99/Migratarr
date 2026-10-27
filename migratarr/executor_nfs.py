"""Shared post-rename NFS visibility verification for same-disk executors."""
import errno
import time


def verify_after_rename(src, dst, expected, log, *, require, canonical_existing,
                        file_metadata, inventory, inventory_metadata,
                        timeout=60, interval=2, metadata_only=False):
    """Retry only missing/stale visibility; never repeat a rename or Arr update."""
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
        require(remaining > 0, 'NFS visibility verification timed out: ' + reason)
        if attempts == 1:
            log('NFS_VISIBILITY_WAIT', reason=reason, retry_window_seconds=timeout)
        time.sleep(min(interval, remaining))
