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

- All five executors' `load_plan` functions accept a valid fixture and refuse
  modified manifest/metadata bytes, duplicate rows/checksum entries, missing or
  revoked approval, approval for another run, empty history, and blocked/executed
  rows. Recomputing checksums does not bypass approval's manifest hash binding.
- Each executor's `check_journal` rejects its existing uncertain-live event set,
  even with a later check-only event. Truncated JSON fails rather than being
  treated as a fresh journal. Clean check-only history remains usable.
- Each executor's `paths` rejects traversal, disk mismatch and changed folder
  names. The same-disk Movie recovery override rejects an unrelated failure.
- Cross-disk Movie and TV `execute` each keep their real approval loader,
  inventory hashing, source metadata checks, destination verification and
  Radarr/Sonarr record verification (the TV path additionally verifies every
  episode file, not just one). Check-only does not copy, update Arr or delete.
  A successful simulated transfer journals intent before copy/update/delete
  and verifies the destination before updating Arr and deleting the source.
- Destination collision, lock/conflicting override, uncertain copy response,
  corrupt destination, source changes, revoked approval after copy or Arr update,
  and incorrect Arr path after update stop progress in both cross-disk executors.
  Failure cases explicitly assert source retention and absence of downstream
  mutations.
- Same-disk Movie and TV check-only flows do not rename or update Arr. Successful
  simulations assert `RENAME_INTENT` before rename, destination verification
  before the Arr update intent, and `SUCCESS` only after post-update checks.
  Source changes and rename failures block the rename/update; a post-rename Arr
  failure remains visibly incomplete for manual reconciliation. TV association
  changes after rename block the Sonarr update.
- Cross-disk NAS operation tests exercise the real validation path with inert
  path/filesystem doubles. Delete refuses receipts with the wrong result,
  operation, source identity or inventory. Check refuses an existing destination
  and inadequate reserve space. Invalid operations, IDs and media paths refuse
  before any mutation helper can run.
- Both incident recovery loaders accept only their exact diagnosed journal shape,
  execution ID, manifest/path binding and (for 0116) copy receipt. Wrong final
  failures, later recovery events, invalid receipts and changed paths refuse.

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
prove the operating system's NAS-side `renameat2`, fsync/durability, process
locking, real NFS visibility, or successful remote delete implementation. The
same-disk flows replace NFS visibility polling with a strict temporary-directory
verifier; remote-operation tests stop at safety gates rather than deleting data.
Recovery execution after its loader is not simulated. Controlled integration
validation is still required before refactoring those boundaries.
