# Shared executor manifest verification

`executor_manifest.py` is the first deliberately small executor refactor. It
owns the immutable manifest, checksum and execution-approval verification that
was previously copied verbatim across executor scripts — four at the time of
this refactor, now five with the later addition of `execute_cross_tv.py`.

Each executor keeps a local `load_plan(base, execution_id)` wrapper and passes
three explicit values to the shared function:

- its accepted media type,
- its accepted transfer type,
- its existing `require` callback.

Passing `require` preserves the executor's existing `Refused` exception class
and refusal messages. The shared module does not import executors, runtime
configuration, NAS transports, Docker/Arr clients or filesystem mutation code.
It has no command-line entrypoint and performs only the same manifest/approval
file reads as the former copies.

This checkpoint does not change path validation, journal handling, check-only
behavior, inventory hashing, Arr verification, locking, copy/rename/delete
operations or recovery logic. `digest` remains available from each executor for
batch-runner compatibility.

The offline executor suite runs the valid path and refusal cases through every
executor wrapper, including checksum tampering, rehashed manifests, duplicate
entries, invalid metadata/columns/approval records, missing or revoked approval,
wrong media/transfer scope, blocked rows and already-executed rows.

Deploy `executor_manifest.py` together with the updated executors. Installing
only part of this change will make executor imports fail closed. Run:

```sh
python3 -m unittest discover -s tests -v
```
