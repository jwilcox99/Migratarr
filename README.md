# Migratarr

Migratarr manages *where* your media library physically lives, not what's in
it. It works alongside Radarr and Sonarr (which manage acquisition and
metadata) and Jellyfin (which serves playback) to move movies and TV
between storage tiers — based on how hard a title would be to re-acquire,
where it's streaming, how it's actually being watched, and how much space
your disks have — instead of leaving everything on one volume forever.

> **Status: early-stage, personally operated.** Migratarr currently runs
> against one home media stack. As of the Phase One closeout
> (`docs/phase-one-closeout.md`, 2026-09-19), same-disk Movie/TV moves
> *and* a live-validated cross-disk Movie executor are on `main`. Since
> then, a live-validated cross-disk **TV** executor (`execute_cross_tv.py`)
> and batch orchestrators for both cross-disk Movie and TV moves
> (`batch_cross_movies.py`, `batch_cross_tv.py`) have also landed and been
> exercised against real production data (see [Project status](#project-status)).
> A read-only validation/config layer (`migratarr_validation/`,
> `config/legacy-*.json`) is also on `main` but not yet wired into live
> execution. Read this whole README, and particularly
> [Safety model](#safety-model), before pointing it at a library you care about.

## What it does

Migratarr evaluates replacement difficulty, streaming availability, and
usage to recommend a logical placement category. Movies also receive a
collection bonus:

- **Replacement difficulty** — how many independent releases still exist
  for this title (queried through Radarr/Sonarr's indexers). More viable
  releases suggest easier replacement; this is an estimate, not a
  guarantee that a title can be re-acquired.
- **Streaming availability** — whether the title is on a service you're
  already subscribed to, free-with-ads, rental/purchase-only, or absent
  entirely from US streaming (via TMDB watch-provider data).
- **Usage** — Jellyfin play recency, repeat plays, and how many distinct
  household members have watched it, with a grace period for newly added
  titles that haven't had a chance to be watched yet.
- **Franchise/collection membership** — small bonus for titles in a
  collection where related entries are already protected.

Movies use **Common**, **Library**, **Rare**, and **Archive**; TV uses
**Current**, **Library**, **Rare**, and **Archive**. Logical categories map
to one or more eligible physical disks through the planner. A title
that's hard to replace *and* has no streaming fallback gets protected in
Rare even if nobody's watched it lately; something with abundant
  availability and no recent plays may be an Archive/Common candidate.

Nothing is moved automatically. Migratarr produces a plan, you review and
approve it, and only then does an executor move files and update
Radarr/Sonarr's records to match.

## How it works (pipeline)

The normal workflow follows this order. Stages read saved outputs and/or
live service state and write reports, caches, snapshots, manifests, or
approvals. There is no single command that runs the whole pipeline.

| # | Script | What it does |
|---|--------|---------------|
| 1 | `dry_run_movies.py` / `dry_run_tv.py` | Score every movie/series and write `movie_dry_run.csv` / `tv_dry_run.csv`. Read-only against Radarr/Sonarr/Jellyfin/TMDB. |
| 2 | `build_move_plan.py` | Turn score-based recommendations into concrete source→destination paths, resolve which physical disk to use, check free space, and write `move_plan.csv`. Still read-only. |
| 3 | `audit_overrides.py` | Optional. Lists the override tags configured in `config/planner.json` (default `migratarr-*`) that are currently set in Radarr/Sonarr, so you can see what's being manually pinned before you plan around it. Other `migratarr-*` tags are flagged as unrecognized. |
| 4 | `snapshot_run.py` | Copies `movie_dry_run.csv`, `tv_dry_run.csv`, `move_plan.csv`, and selected current code files into a read-only, checksummed run under `runs/<timestamp>/`. Keep the code unchanged between planning and snapshotting. |
| 5 | `build_execution_manifest.py` | Turns the frozen snapshot's eligible rows into a manifest with one stable `execution_id` per proposed move, hashed and stored under `manifests/<run>/`. |
| 6 | `approve_execution.py` | Human review. Lists manifest rows and lets you `--approve <execution_id>` one at a time. Writes an approval record; **moves no files**. |
| 7 | `execute_movie_nas.py` / `execute_tv_nas.py` (same-disk) / `execute_cross_movie.py` / `execute_cross_tv.py` (cross-disk) | Takes one approved `execution_id`, re-verifies everything (manifest hash, approval hash, live Radarr/Sonarr state, full file-content hash), performs the move, and updates Radarr/Sonarr. Defaults to check-only — pass `--execute` to actually move files. |
| 8 | `batch_cross_movies.py` / `batch_cross_tv.py` (optional) | Lists every pending cross-disk Movie/TV row from a run's manifest and, with `--execute`, approves and runs each through its executor in sequence (smallest first). `--limit N` caps how many it processes in one invocation instead of the full pending set. Without `--execute` it only lists — no approvals, copies, updates, or deletions. |

`movie_placement_v1.py` / `tv_placement_v1.py` are frozen, checksum-pinned
copies of the scoring logic from the point it was first validated
(`movie_placement_v1.sha256` / `tv_placement_v1.sha256`), kept for
reproducing past decisions. They are not part of the pipeline you run.

## Prerequisites

- Linux host with **Python 3.10+** — standard library only, nothing to
  `pip install`. (`migratarr_validation/` uses PEP 604 `X | None` union
  annotations, evaluated at import time; 3.9 will fail to import it.)
- By default, Docker with `docker exec` access to your Radarr and Sonarr
  containers: the executors verify files as Radarr/Sonarr see them from inside
  the container, and API keys are read straight out of each service's config
  (Radarr/Sonarr `config.xml`, a Jellyfin secret in a `homepage` container)
  rather than stored anywhere. Non-Docker installs choose other sources in
  `runtime.json`: [Service credentials](docs/runtime-configuration.md#service-credentials)
  and [Arr file checks](docs/runtime-configuration.md#arr-file-checks).
- A [TMDB](https://www.themoviedb.org/settings/api) API **Read Access
  Token**, by default exported as `TMDB_TOKEN` (see [Usage](#usage)).
- SSH access to your NAS with a dedicated key, if you want the executors
  to perform moves remotely rather than on the media host's local mounts.
- Radarr/Sonarr root folders and Jellyfin libraries already pointed at
  the disks Migratarr will place things on.

## Configuration

Runtime scripts use a required, validated `config/runtime.json` file. Start with:

```sh
cp config/runtime.example.json config/runtime.json
python3 -c 'from runtime_config import get_config; get_config(); print("Runtime config valid")'
```

Review the example's media host/NAS values before use. See
[Runtime configuration](docs/runtime-configuration.md) for all fields, environment
overrides, retained layout restrictions, validation errors and migration steps.
No credentials belong in the config. The deployment file is ignored by Git.

The dry-run planners also require `config/planner.json`: the streaming
services you subscribe to and your TMDB watch-provider region. Start from the
example (Hulu + Peacock, `US`) and edit it for yourself; see
[Planner settings](docs/planner-settings.md).

```sh
cp config/planner.example.json config/planner.json
python3 -c 'from planner_settings import load_settings; load_settings(); print("Planner settings valid")'
```

`config/legacy-storage.json` and `legacy-rules.json` remain separate read-only
validation policy. Scoring, category placement policy and executor semantics are
unchanged. Safe unit tests run in GitHub Actions and locally with
`python3 -m unittest discover -s tests -v`.

See [SETUP.md](SETUP.md) for what still assumes this deployment's shape
before running this against a library you care about.

## Usage

```bash
export TMDB_TOKEN='YOUR_TMDB_READ_ACCESS_TOKEN'

python3 dry_run_movies.py
python3 dry_run_tv.py
python3 build_move_plan.py
python3 audit_overrides.py            # optional, read-only
python3 snapshot_run.py
python3 build_execution_manifest.py
python3 approve_execution.py --list-same-disk
EXECUTION_ID='REPLACE_WITH_SELECTED_MOVIE_EXECUTION_ID'
python3 approve_execution.py --approve "$EXECUTION_ID"

# Dry-run the approved move first (default is check-only):
python3 execute_movie_nas.py "$EXECUTION_ID"
# Then actually move it:
python3 execute_movie_nas.py "$EXECUTION_ID" --execute
```

Set `EXECUTION_ID` to the Movie row you selected before running
the approval and executor commands. The example uses the same-disk NAS
Movie executor; select a row its configured disk mapping supports.

## Safety model

This is the part to actually read before pointing Migratarr at a library
you care about.

- **Nothing moves until you run an executor with `--execute`.** No
  earlier stage touches a source or destination media file — but they do
  write more than just CSVs/JSON: the placement scripts cache external
  API lookups as JSON under `cache/`, and
  `snapshot_run.py` writes a whole `runs/<timestamp>/` directory (copied
  CSVs, copied code, `metadata.json`, `SHA256SUMS`).
- **Snapshots are read-only and checksummed.** `snapshot_run.py` removes
  write permissions after copying the inputs, and manifest creation checks
  their hashes. These safeguards detect accidental changes; an owner or
  privileged process can still change permissions, files, and checksums.
- **Approval is per-item and hash-bound.** Approving execution ID `X`
  records the manifest's hash at that moment; the executor refuses to run
  if the manifest has changed since.
- **The executor re-verifies everything immediately before moving
  anything**: the manifest hash, the approval record, live Radarr/Sonarr
  state for that exact item, and a full SHA-256 hash of the source
  directory's contents.
- **Same-disk renames are collision-safe** (`renameat2` with
  `RENAME_NOREPLACE`, without overwriting an existing destination) and are verified
  by content hash again after the move, on both the local and
  Radarr/Sonarr-visible paths.
- **Every attempt is journaled** to `execution_logs/<execution_id>.jsonl`
  before and after any mutation. A failed live attempt refuses to be
  retried automatically — it requires manual reconciliation (or, for one
  specifically diagnosed failure mode, `--resume-nfs-refusal`).
- **Live execution coverage still isn't full parity with the planner.**
  `execute_movie_nas.py` / `execute_tv_nas.py` perform same-disk renames on
  one disk (see `remote_path()`); `execute_cross_movie.py` and
  `execute_cross_tv.py` (both on `main`, live-validated) cover cross-disk
  **Movie** and **TV** transfers via staged copy → verify → atomic
  publish/Radarr-or-Sonarr update → verified delete. Read a plan's
  `transfer_type` and `status` columns before assuming an executor can act
  on a row.

## Project status

As of the **Phase One closeout** (`docs/phase-one-closeout.md`,
2026-09-19), `main` has:

- A working, validated pipeline for **same-disk Movie and TV moves**.
- A live-validated **cross-disk Movie executor** (`execute_cross_movie.py`
  — staged copy, destination verification, Radarr path update, verified
  source removal, full journal), plus `batch_cross_movies.py` and two
  incident-specific recovery scripts (`recover_cross_0102.py`,
  `recover_cross_0116.py`) from real runs.
- A live-validated **cross-disk TV executor** (`execute_cross_tv.py`),
  mirroring the Movie executor's safety architecture with Sonarr
  multi-episode-file verification, plus `batch_cross_tv.py`. Both have
  moved real series end-to-end on the production host — a single
  `execute_cross_tv.py` run and a multi-series `batch_cross_tv.py --execute`
  run (see `docs/storage-targets.md` for the exact evidence).
- Storage-target configuration (`storage_targets.py`,
  `config/storage-targets.json`) has replaced hardcoded planner/executor
  disk assumptions across four migration gates (`docs/storage-targets.md`):
  planner byte parity, manifest target IDs, `runtime.json`/
  `storage-targets.json` consistency checking, and executor disk
  generalization. `runtime_config.py` now accepts any number of validly-shaped
  disk IDs instead of exactly `media01`-`media04`; the real deployment is
  still four disks, but the four-disk assumption is no longer load-bearing
  in the code.
- A standalone, read-only **validation engine** (`migratarr_validation/`)
  with planner characterization tests, parity checks against real server
  data, capture/audit tooling, and the project's first automated tests
  (`tests/`) — checked for byte-for-byte parity with the legacy planner
  (`docs/validation-engine.md`) but **not yet the live planner/executor
  gate**.
- `config/legacy-storage.json` / `config/legacy-rules.json` — a first cut
  at moving storage layout and override policy into versioned JSON config.
  Still scoped "legacy," not wired into a live run.

The branches these came from (`cross-disk-transfers`,
`feature/validation-engine`) are both merged ancestors of `main` — don't
build against them separately.

`execute_movie.py` also remains in the tree: an earlier, non-NAS-transport
version of the same-disk Movie executor (no SSH remote-execution path).
It predates `execute_movie_nas.py` and isn't part of the documented
pipeline above.

The next checkpoint, per the closeout's Phase Two handoff, is a unified
execution core that removes the Movie/TV and same-disk/cross-disk
duplication, with the validation engine promoted to the live
pre-execution gate.

## Known limitations

- No orchestrating entry point; the pipeline order above is documentation,
  not enforced by any single command. `batch_cross_movies.py` /
  `batch_cross_tv.py` sequence one media type's cross-disk moves, but
  nothing drives the whole dry-run → plan → snapshot → manifest pipeline.
- Significant duplication between `execute_movie_nas.py`,
  `execute_tv_nas.py`, `execute_cross_movie.py`, and `execute_cross_tv.py`
  — changes to the shared safety logic currently have to be ported by hand
  across all four (see the Phase Two handoff in `docs/phase-one-closeout.md`).
- **No CI.** `tests/` exists on `main` but nothing runs it automatically —
  `docs/phase-one-closeout.md` states this explicitly.
- `migratarr_validation/` is read-only and not yet the live gate for
  planning or execution.
- Runtime configuration supports one host/NAS pairing. Disk count, root
  depth and category folder names are configuration (`docs/media-layout.md`),
  but the executor topology itself (one media host, one NAS reached
  over SSH) isn't otherwise generalized.
- Streaming subscriptions, TMDB region, override tags and every scoring
  weight, tier and threshold are settings (`config/planner.json`, see
  `docs/planner-settings.md`).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## Security

See [SECURITY.md](SECURITY.md) — in particular, if you're forking or
publishing your own copy, scrub the NAS hostname/IP, SSH username, and any
container-specific paths committed for this deployment.

## License

[MIT](LICENSE).
