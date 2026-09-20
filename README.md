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
> *and* a live-validated cross-disk Movie executor are on `main`; a
> read-only validation/config layer (`migratarr_validation/`,
> `config/legacy-*.json`) is also on `main` but not yet wired into live
> execution (see [Project status](#project-status)). Read this whole
> README, and particularly [Safety model](#safety-model), before pointing
> it at a library you care about.

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
| 3 | `audit_overrides.py` | Optional. Lists any manual `migratarr-*` tags currently set in Radarr/Sonarr, so you can see what's being manually pinned before you plan around it. |
| 4 | `snapshot_run.py` | Copies `movie_dry_run.csv`, `tv_dry_run.csv`, `move_plan.csv`, and selected current code files into a read-only, checksummed run under `runs/<timestamp>/`. Keep the code unchanged between planning and snapshotting. |
| 5 | `build_execution_manifest.py` | Turns the frozen snapshot's eligible rows into a manifest with one stable `execution_id` per proposed move, hashed and stored under `manifests/<run>/`. |
| 6 | `approve_execution.py` | Human review. Lists manifest rows and lets you `--approve <execution_id>` one at a time. Writes an approval record; **moves no files**. |
| 7 | `execute_movie_nas.py` / `execute_tv_nas.py` (same-disk) / `execute_cross_movie.py` (cross-disk Movie) | Takes one approved `execution_id`, re-verifies everything (manifest hash, approval hash, live Radarr/Sonarr state, full file-content hash), performs the move, and updates Radarr/Sonarr. Defaults to check-only — pass `--execute` to actually move files. |

`movie_placement_v1.py` / `tv_placement_v1.py` are frozen, checksum-pinned
copies of the scoring logic from the point it was first validated
(`movie_placement_v1.sha256` / `tv_placement_v1.sha256`), kept for
reproducing past decisions. They are not part of the pipeline you run.

## Prerequisites

- Linux host with **Python 3.10+** — standard library only, nothing to
  `pip install`. (`migratarr_validation/` uses PEP 604 `X | None` union
  annotations, evaluated at import time; 3.9 will fail to import it.)
- Docker, with `docker exec` access to your Radarr, Sonarr, and (for the
  Jellyfin API key) a container that holds a Jellyfin secret — the scripts
  read API keys directly out of each container rather than storing them
  anywhere.
- A [TMDB](https://www.themoviedb.org/settings/api) API **Read Access
  Token**, exported as `TMDB_TOKEN` (see [Usage](#usage)).
- SSH access to your NAS with a dedicated key, if you want the executors
  to perform moves remotely rather than on the media host's local mounts.
- Radarr/Sonarr root folders and Jellyfin libraries already pointed at
  the disks Migratarr will place things on.

## Configuration

The live pipeline still uses Python constants for storage layout,
service URLs, and container names. The standalone validation tools have
JSON configuration, but it does not configure live execution.
Before running this against your own stack, change:

| Constant | Where | What it is |
|---|---|---|
| `BASE` | `snapshot_run.py`, `build_move_plan.py`, `build_execution_manifest.py`, `approve_execution.py` | Working directory for CSVs, `runs/`, `manifests/`, `approvals/`, `execution_logs/` |
| `DESTINATION_ROOTS` | `build_move_plan.py` | Which disk(s) each media type/tier is allowed to live on |
| `MIN_FREE_AFTER_GB` | `build_move_plan.py` | Minimum free space to preserve on any disk after a move |
| `RADARR_URL` / `SONARR_URL` / `JELLYFIN_URL` | multiple scripts | Base URLs for each service |
| `SUBSCRIBED` / `USER_FREE_ACCESS` | `dry_run_movies.py`, `dry_run_tv.py` | Which streaming services count as "already accessible" when scoring |
| `docker exec radarr` / `sonarr` | `execute_movie_nas.py`, `execute_tv_nas.py`, `audit_overrides.py` | Container names Migratarr expects |
| NAS host and SSH key path | `execute_movie_nas.py`, `execute_tv_nas.py` (`NasTransport`) | Remote host/user and `~/.ssh/<key>` used for NAS-side moves |
| `remote_path()` disk mapping | `execute_movie_nas.py`, `execute_tv_nas.py` | Only one disk is currently wired for live remote execution |

A configurable, validated version of the storage/override-policy half of
this (`config/legacy-storage.json`, `config/legacy-rules.json`) is on
`main`, but it's explicitly scoped "legacy" and not wired into a live
run yet — see [Project status](#project-status).

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
  `build_move_plan.py` reasons about all four disks. `execute_movie_nas.py`
  / `execute_tv_nas.py` perform same-disk renames on one disk (see
  `remote_path()`); `execute_cross_movie.py` (on `main`, live-validated)
  covers cross-disk **Movie** transfers via staged copy → verify → atomic
  publish → Radarr update → verified delete. Cross-disk **TV** has no
  validated executor yet. Read a plan's `transfer_type` and `status`
  columns before assuming an executor can act on a row.

## Project status

As of the **Phase One closeout** (`docs/phase-one-closeout.md`,
2026-09-19), `main` has:

- A working, validated pipeline for **same-disk Movie and TV moves**.
- A live-validated **cross-disk Movie executor** (`execute_cross_movie.py`
  — staged copy, destination verification, Radarr path update, verified
  source removal, full journal), plus `batch_cross_movies.py` and two
  incident-specific recovery scripts (`recover_cross_0102.py`,
  `recover_cross_0116.py`) from real runs.
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
  not enforced by any single command.
- Significant duplication between `execute_movie_nas.py`,
  `execute_tv_nas.py`, and `execute_cross_movie.py` — changes to the
  shared safety logic currently have to be ported by hand.
- **No CI.** `tests/` exists on `main` but nothing runs it automatically —
  `docs/phase-one-closeout.md` states this explicitly.
- `migratarr_validation/` is read-only and not yet the live gate for
  planning or execution.
- Single-host, single-NAS assumptions baked into constants rather than
  configuration (see [Configuration](#configuration)); `config/legacy-*`
  is a first step, not a finished replacement.
- User-facing preferences (subscriptions, scoring weights, tier
  thresholds) are Python constants, not something a user sets.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## Security

See [SECURITY.md](SECURITY.md) — in particular, if you're forking or
publishing your own copy, scrub the NAS hostname/IP, SSH username, and any
container-specific paths committed for this deployment.

## License

[MIT](LICENSE).
