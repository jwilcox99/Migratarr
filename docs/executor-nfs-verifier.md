# Shared same-disk NFS visibility verifier

`executor_nfs.py` contains the post-rename visibility loop shared by the NAS
Movie and TV executors. Those two implementations previously differed only in
whether their docstring named Radarr or Sonarr.

The executor-local `verify_nfs_after_rename` signatures remain unchanged. Each
wrapper supplies its existing refusal, canonical-path, inventory and metadata
functions to the shared helper. This preserves executor-specific `Refused`
exceptions and keeps the shared module independent of runtime configuration,
media type, Arr clients, NAS transports and mutation code.

The verifier retains the Phase One contract:

- a still-visible source path is retried until the fixed deadline;
- only missing/stale filesystem errors (`ENOENT`/`ESTALE`) are retried;
- other operating-system errors propagate immediately;
- a readable but different destination inventory refuses immediately;
- the first retry logs `NFS_VISIBILITY_WAIT`, and eventual visibility logs
  `NFS_VISIBILITY_READY`;
- metadata-only verification does not rehash file content;
- the verifier never repeats the rename or an Arr update.

Direct offline tests cover both executor wrappers, immediate success, one retry,
timeout/refusal type, readable mismatch, metadata-only operation and the exact
retryable error set. The same-disk sequence tests continue to cover placement of
the verifier between rename and Arr update.

Deploy `executor_nfs.py` together with the two updated NAS executors. A partial
install fails closed during import. Run the full safe suite with:

```sh
python3 -m unittest discover -s tests -v
```
