# Offline executor safety checkpoint

`tests/test_executor_safety.py` characterizes the existing Phase One executors
before architecture changes. It does not alter executor code or enable live work.

Run the complete suite (also run by the existing GitHub Actions workflow):

```sh
python3 -m unittest discover -s tests -v
```

Run only this checkpoint:

```sh
python3 -m unittest discover -s tests -p test_executor_safety.py -v
```

## Contracts covered

- All four executors' `load_plan` functions accept a valid fixture and refuse
  modified manifest/metadata bytes, duplicate rows/checksum entries, missing or
  revoked approval, approval for another run, empty history, and blocked/executed
  rows. Recomputing checksums does not bypass approval's manifest hash binding.
- Each executor's `check_journal` rejects its existing uncertain-live event set,
  even with a later check-only event. Truncated JSON fails rather than being
  treated as a fresh journal. Clean check-only history remains usable.
- Each executor's `paths` rejects traversal, disk mismatch and changed folder
  names. The same-disk Movie recovery override rejects an unrelated failure.
- Cross-disk Movie `execute` keeps its real approval loader, inventory hashing,
  source metadata checks, destination verification and Arr record verification.
  Check-only does not copy, update Arr or delete. A successful simulated transfer
  journals intent before copy/update/delete and verifies the destination before
  updating Arr and deleting the source.
- Destination collision, lock/conflicting override, uncertain copy response,
  corrupt destination, source changes, revoked approval after copy or Arr update,
  and incorrect Arr path after update stop progress. Failure cases explicitly
  assert source retention and absence of downstream mutations.

## Isolation and limits

Fixtures create manifests, approvals, journals and tiny disposable media under
`TemporaryDirectory`. The example runtime config is injected at import time;
installed runtime config and credentials are unnecessary. Subprocess entrypoints
and socket connection creation are blocked during these tests.

The fake NAS transport copies one temporary file and deletes only that fixture
file/directory. The fake Arr client returns detached records and verifies real
temporary-file hashes. Only deployment path translation is replaced inside the
cross-disk execution flow. No SSH, Docker, HTTP, real media mounts, CLI entrypoints
or NAS helper programs are executed.

This is a focused regression checkpoint, not full executor coverage. It does not
prove NAS-side `renameat2`, fsync/durability, process locking, real NFS visibility,
remote delete receipts, or full same-disk Movie/TV execution sequencing. It does
not generalize incident recovery. Those require additional targeted tests and/or
controlled integration validation before the corresponding code is refactored.
