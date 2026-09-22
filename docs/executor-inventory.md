# Shared inventory metadata

`executor_inventory.py` supplies the read-only metadata scan and inventory
conversion used by the NAS Movie, TV and cross-disk Movie executors.
Executor-local `file_metadata(root)` and `inventory_metadata(items)` signatures
are unchanged. Scans pass the executor's existing `require` callback, retaining
its refusal type and messages.

The scan retains sorted traversal, root-device enforcement, rejection of links
and nonregular files, POSIX relative names, directory markers, and the original
size/inode/nanosecond-mtime tuples. It never reads file contents. Conversion drops
only the hash field from a content inventory, preserving the identity fields.

NAS helper builders embed the two shared function definitions before their
local wrappers. They do not import this module on the NAS. This is also used by
the 0102 recovery program, which derives its helper from the cross-disk executor.

The hashing loops intentionally remain local. The older local Movie executor
has different verification behavior, and the NAS Movie and TV/cross-disk loops
have distinct refusal text. This checkpoint does not generalize them or alter
copy, deletion, rename, journal, transport, locking or recovery operations.

Eight offline tests cover actual temporary-file metadata, absence of content
reads, hash/conversion parity, links/nested devices/special files (stat doubles),
filesystem errors, empty trees, changes during hashing, and standalone embedded
NAS definitions. The entire generated program is compiled; only read-only
function definitions and inert configuration are executed. NAS locking and
mutation dispatch remain excluded. These tests do not prove real NAS/NFS behavior.

Install `executor_inventory.py` with all three updated NAS executors. The NAS
itself needs no new installed module. Preserve runtime config, approvals and
journals; stop scheduling jobs during an update. Validate with:

```sh
python3 -m unittest discover -s tests -v
```
