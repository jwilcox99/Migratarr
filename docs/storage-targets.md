# Storage target migration

The `storage_targets.py` model now supplies the planner's source roots,
destination paths, physical disk IDs and per-target reserves. The planner requires
`config/storage-targets.json` beneath its runtime base directory, or an explicit
`MIGRATARR_STORAGE_TARGETS` path. Missing or invalid configuration fails before
Arr calls and plan writes. The example is never an implicit fallback.

This branch is pending real-data parity evidence before merge/deployment.
Runtime configuration and executors still retain their existing storage settings;
do not remove those yet. Keep the existing four target paths consistent between
the runtime and target configurations during this intermediate migration.

Load an explicit file with `load_targets(filename)` or validate an object with
`parse_targets(data)`. `config/storage-targets.example.json` reproduces the four
current target IDs, local and remote roots, category directories, fallback order,
and 50 GiB reserves. Tests compare its derived values to both existing configs.

Targets contain declared configuration only. `TargetState` holds observed
capacity and free space in bytes; a future planner probe must supply those values.
This module performs no storage probes and writes no snapshots or media files.

Disabled targets remain source roots but cannot appear in placement lists.
Placement references must be enabled and support the requested media type.
Missing `remote_path` means plan-only; no transport authorization is inferred from
this model. If any remote path exists, the entire configuration retains the
`phase1-fixed-depth` local shape and shared-parent checks, plus the two-component
remote shape. Target IDs remain independent of paths in configurations with no
remote paths. Local roots cannot overlap; remote roots must be distinct.

Reserve values are nonnegative integer GiB, with a default and optional per-target
override. Priority defaults to 100 and accepts integers (including negative
values); priority, storage class and tags have no placement effect. Optional
remote paths must be omitted rather than null. An empty media-type list is valid
for a source-only target, but cannot satisfy any placement reference.

Gate 1 (complete): real-data planner byte parity was obtained on the live
deployment host; both `--capture-facts` and `--replay-facts` runs reported
`byte_identical: true` against the frozen movie/TV dry-run CSVs and a live
Arr overrides snapshot.

`python -m migratarr_validation.planner_parity` compares the planner at `c31afc6`
with this candidate, using saved movie/TV CSVs and overrides. It runs only selected
AST definitions, candidate loops and cumulative capacity checks; neither planner's
CSV writes or Arr calls run. CSV serialization includes headers and row order.
Filesystem exists/resolve/size/free results are memoized across both runs and can
be saved with `--capture-facts` and replayed with `--replay-facts`. Capture performs
read-only filesystem probes (including potentially slow directory-size scans).
Observations are cached, not an atomic filesystem snapshot; pause other activity.

```sh
python3 -m migratarr_validation.planner_parity \
  --baseline tests/fixtures/planner_before_storage_targets.py \
  --movie-csv /path/to/frozen/movie_dry_run.csv \
  --tv-csv /path/to/frozen/tv_dry_run.csv \
  --overrides-json /path/to/frozen/overrides.json \
  --runtime-config /opt/media-stack/migratarr/config/runtime.json \
  --targets config/storage-targets.example.json \
  --capture-facts /path/to/new/planner-facts.json
```

The baseline is a checksum-pinned, test-only copy of the pre-migration planner,
not a second active implementation. Exit 0 requires identical CSV bytes; exit 1
means differences and exit 2 means incomplete verification. Report includes code,
input, observation and CSV hashes. Replaying the captured observations must also
succeed. Existing live media may have moved since older CSVs were created; use
saved observations from the same dataset when available, and do not claim an old
historical move_plan was reproduced from today's filesystem observations.

After the gate passes, install the reviewed target configuration before updating
scheduled planning. Keep code and configuration unchanged between planning and
snapshotting. `snapshot_run.py` includes the target config and loader in its
checksummed code snapshot. Executors are not generalized by this change;
fifth-target plans must not be approved for execution yet.

Current gate: `build_execution_manifest.py`'s operator-facing cross-disk flow
report now enumerates `TARGETS.targets` instead of a hardcoded four-disk list,
so a fifth target's flow is visible in the report instead of silently omitted.
This does not touch `execution_manifest.csv` or `manifest_metadata.json`
content, the manifest CSV schema, or execution semantics; those are unchanged
and still gated on gate 4 below before a fifth target may be executed.

`python -m migratarr_validation.manifest_parity` runs the manifest builder at
its pre-target-id revision and the current candidate against copies of the
same frozen run snapshot, each under an isolated scratch base path, and
confirms `execution_manifest.csv` and `manifest_metadata.json` are
byte-identical (aside from the manifest's own wall-clock `created_utc` stamp
and its scratch-path-derived `source_snapshot` field, neither of which either
run can hold fixed). No Arr calls; the manifest builder only reads a frozen
snapshot and writes under the scratch base path, never the real deployment's
`runs/` or `manifests/` directories.

```sh
python3 -m migratarr_validation.manifest_parity \
  --baseline tests/fixtures/manifest_before_target_ids.py \
  --run-dir /opt/media-stack/migratarr/runs/<run_id> \
  --runtime-config /opt/media-stack/migratarr/config/runtime.json \
  --targets /opt/media-stack/migratarr/config/storage-targets.json
```

Exit 0 requires identical manifest bytes; exit 1 means differences and exit 2
means incomplete verification. Use a `<run_id>` you already trust the plan
for, the same way the planner gate uses saved CSVs rather than a fresh dry run.

Remaining gates:

1. ~~Obtain real-data byte parity for this planner integration.~~ Complete.
2. Derive manifest target IDs and prove unchanged manifest bytes. (current gate)
3. Migrate runtime storage settings, keeping compatibility shape validation.
4. Generalize executor disk sets and the media04 pin in a separate safety review
   with real approved-manifest check-only evidence.

Adding a fifth entry is supported by the candidate planner and now visible in
the manifest report, but not executable. Recovery incident scripts and frozen
placement rules are unchanged.
