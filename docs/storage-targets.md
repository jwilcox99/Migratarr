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

Gate 2 (complete): `build_execution_manifest.py`'s operator-facing cross-disk
flow report enumerates `TARGETS.targets` instead of a hardcoded four-disk
list, so a fifth target's flow is visible in the report instead of silently
omitted. This did not touch `execution_manifest.csv` or `manifest_metadata.json`
content, the manifest CSV schema, or execution semantics; those are still
gated on gate 4 below before a fifth target may be executed.
`migratarr_validation.manifest_parity` proved this with a real-data byte-parity
run against a frozen run snapshot, the same way gate 1 used
`migratarr_validation.planner_parity`.

Current gate: `runtime.json`'s `storage.media0N` block and
`storage-targets.json`'s corresponding four targets are still two
independently-maintained files describing the same physical roots; nothing
previously caught them drifting apart at runtime, only a CI test comparing
the two *example* files (`tests/test_storage_targets.py`). This does not make
either file derive from the other, and it does not touch `runtime_config.py`'s
disk shape contract that the executors still depend on (see
`docs/runtime-configuration.md`) — it only turns silent drift between the two
real deployed files into an explicit refusal.

`storage_targets.check_runtime_consistency(runtime, targets)` checks, for
every disk `runtime.json` declares, that the storage-targets entry with the
same ID has the identical `local_path` and `remote_path`. `build_move_plan.py`,
`build_execution_manifest.py` and `snapshot_run.py` all already load both
`runtime_config` and `storage_targets`; each now calls this check immediately
after loading `TARGETS`, so a deployment where the two files disagree fails
before any Arr call, plan write or run snapshot rather than only being caught
if someone happens to compare them by eye.

```sh
python3 -c '
from runtime_config import get_config
from storage_targets import load_targets, check_runtime_consistency
import os
runtime = get_config()
targets = load_targets(os.environ.get("MIGRATARR_STORAGE_TARGETS") or runtime.base_path / "config/storage-targets.json")
check_runtime_consistency(runtime, targets)
print("runtime.json and storage-targets.json agree")
'
```

Gate 3 (complete): `storage_targets.check_runtime_consistency` verified against
the real deployed `runtime.json` and `storage-targets.json` on the media host; they agree.

Current gate: this is a proof-of-concept generalization, not a live 5th-disk
deployment — no disk was added to the real deployment. `runtime_config.py`'s
`storage` schema now accepts any number of validly-shaped disk IDs instead of
exactly `media01`-`media04` (a strict superset; the real 4-disk `runtime.json`
is unchanged and still valid). `execute_movie_nas.py` and `execute_tv_nas.py`
(the same-disk NAS rename executors) resolve the disk from each approved
manifest row via `RUNTIME.remote_disks`, instead of hardcoding `media04`; the
generated remote SSH program bakes in the resolved disk's remote root for that
call rather than always `media04`'s. `execute_cross_movie.py` already read
`RUNTIME.remote_disks` generically and needed no changes.

A genuine 5th disk (`media05`, not a relabeling of an existing one) is proven
end-to-end offline in `tests/test_runtime_config.py` (`test_fifth_disk_is_accepted`,
`test_fifth_disk_works_end_to_end_through_same_disk_executors`), since there
is no real 5th disk to test live execution against.

Real-data evidence: both existing local runs (`20260915T192453Z` and
`20260915T192959Z`) predate this generalization and were exercised extensively
during earlier sessions, so every row in both is now stale relative to the
live deployment — media already moved, or an item now carries a
`migratarr-lock` tag. `execute_movie_nas.py`, `execute_tv_nas.py`, and
`execute_cross_movie.py`'s default check-only mode (no `--execute`) was run
against one freshly-approved row of each type from the older, unexecuted
`20260915T192453Z` manifest. All three reached the real preflight code
path — live SSH to the NAS, live Radarr/Sonarr lookups — using the
generalized disk lookup this gate adds, and correctly refused: a source path
that no longer exists because that title already moved, and a movie now
carrying `migratarr-lock`. None reached a full `CHECK_ONLY` result, because
no untouched manifest data remains from prior sessions; getting that would
require a fresh dry-run → plan → snapshot → manifest → approve cycle, treated
as future work rather than blocking this proof-of-concept. The refusals
themselves are correct preflight behavior, not gate 4 regressions, and confirm
the generalized code exercises the same real NAS/Radarr/Sonarr paths as before.

```sh
python3 execute_movie_nas.py <execution_id>
python3 execute_tv_nas.py <execution_id>
python3 execute_cross_movie.py <execution_id>
```

Each defaults to check-only; none of these commands pass `--execute`, so no
file is moved, renamed, or deleted, and no Radarr/Sonarr record is updated —
only the preflight validation runs.

Remaining gates:

1. ~~Obtain real-data byte parity for this planner integration.~~ Complete.
2. ~~Derive manifest target IDs and prove unchanged manifest bytes.~~ Complete.
3. ~~Migrate runtime storage settings, keeping compatibility shape validation.~~ Complete.
4. ~~Generalize executor disk sets and the media04 pin.~~ Proof-of-concept
   complete: offline genericity proof plus real preflight evidence against
   stale-but-real manifests. No real 5th disk has been deployed or executed
   against live.

Adding a fifth entry is supported by the candidate planner and now visible in
the manifest report, and the executors no longer refuse it structurally — but
no fifth disk has been approved for live execution. Recovery incident scripts and frozen
placement rules are unchanged.
