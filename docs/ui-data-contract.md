# Read-only UI: data contract

**Status:** Read layer implemented 2026-09-25 as `read_api.py` + `migratarr_status.py` (tests: `tests/test_read_api.py`); the web UI itself is not built yet. Written 2026-09-24.
**Plan reference:** UI milestone 1 (read-only web UI) in `2026-09-24-release-path-plan`, and §4 of `docs/unified-execution-core.md`.
**Goal:** pin down exactly which files the first UI reads, what each field means, and how the UI works out a move's status. The UI can then be built against formats that already exist, without waiting for new storage.

## 1. Ground rules

1. **Read-only, in every sense.** The UI never writes, renames or `chmod`s anything under the base path, and **never takes a lock**. The executors take `execution_logs/executor.lock` with `flock(LOCK_EX | LOCK_NB)`. If the UI held that lock even briefly, an executor starting at the same moment would refuse to run.
2. **Verify, then display.** Before showing a run or manifest, the UI checks its `SHA256SUMS`. On a mismatch it shows an integrity error instead of the data.
3. **The source of truth for state is approvals + journals.** Several stored "status" fields are frozen at creation and never updated (§3), so the UI must ignore them.
4. **Tolerate in-flight writes.**
   - Journals are appended and fsynced line by line, so a crash can leave a truncated last line. The executors refuse such a journal; the UI shows it as *journal unreadable*.
   - `approvals/<run>.json` is rewritten in place (`save_approvals` uses `write_text`), so the UI retries once on a JSON error before reporting one.
5. **Base path** comes from `runtime.json` `base_path` (`RUNTIME.base_path`). Everything below is relative to it.

## 2. Files the UI reads

| Path | Written by | Mutability | UI use |
| --- | --- | --- | --- |
| `runs/<run_id>/` | `snapshot_run.py` | Immutable (`chmod 444/555`) | Run list, full plan (including blocked rows), scoring detail |
| `manifests/<run_id>/` | `build_execution_manifest.py` | Immutable by convention, checksummed; one per run | Executable rows and their `execution_id`s |
| `approvals/<run_id>.json` | `approve_execution.py` | Rewritten on each approve/revoke | Approval state and history |
| `execution_logs/<execution_id>.jsonl` | Executors, recovery scripts | Append-only | Per-move status and timeline |
| `execution_logs/executor.lock` | Executors | Locked while one runs | **Never opened by the UI** (rule 1) |

`run_id` is `YYYYMMDDTHHMMSSZ` (UTC). `execution_id` is `<run_id>-NNNN` (4+ digits), numbered in manifest order.

### 2.1 `runs/<run_id>/`

- **`metadata.json`:**
  - `run_id`, `created_utc`.
  - `git.{commit, branch, working_tree_clean, working_tree_status[]}`.
  - `counts.{movie_scores, tv_scores, planned_moves, status{}, media_type{}, transfer_type{}}`.
  - `checksums{}`.
- **`move_plan.csv`:** every planned category change, **including `BLOCKED` rows**, which never reach the manifest. The columns are exactly `build_move_plan.py` `FIELDS`:
  - `media_type`, `title`, `current`, `scored_recommendation`, `recommended`
  - `override_type`, `override_tag`
  - `source_path`, `target_path`
  - `size_gb`, `destination_free_gb`, `free_after_move_gb`
  - `source_disk`, `target_disk`, `transfer_type` (`SAME_DISK_RENAME` | `CROSS_DISK_TRANSFER`)
  - `final_score`, `replacement`, `replacement_confidence`, `decision_reason`
  - `arr_path_update_required`
  - `status` (`READY_FOR_REVIEW` | `BLOCKED`), `blockers`, `warnings`

  `blockers` and `warnings` are `;`-separated codes. Blocker codes include:
  - `SOURCE_CATEGORY_UNKNOWN`, `CONFLICTING_MANUAL_OVERRIDES`
  - `NO_ELIGIBLE_DESTINATION`, `SOURCE_MISSING`, `DESTINATION_ROOT_MISSING`, `DESTINATION_COLLISION`
  - `INSUFFICIENT_DESTINATION_SPACE`, `INSUFFICIENT_PROJECTED_SPACE`, `CUMULATIVE_DESTINATION_SPACE`
- **`movie_dry_run.csv` / `tv_dry_run.csv`:** per-item scoring detail for "why is this here". This is where the per-component reasons live:
  - Movie: `streaming_reason`, `usage_reason`, `franchise_reason`, `viable_releases`, `threshold_flag`.
  - TV: `replacement_detail`, `streaming_detail`, `usage_detail`, `owned_seasons`.
- **`code/`:** the exact code and config used, including `runtime.json`.
  - The UI must **not display `code/runtime.json` by default**: it names the NAS host and SSH user. It contains no credentials by design.
  - It may show `storage-targets.json`.

### 2.2 `manifests/<run_id>/`

- **`execution_manifest.csv`:** only rows that were `READY_FOR_REVIEW` with `current != recommended`.
  - Columns: `execution_id`, then every `move_plan.csv` column, then `approved`, `executed`, `execution_result`.
  - It can be an empty file when nothing was eligible.
- **`manifest_metadata.json`:**
  - `manifest_version` (1), `run_id`, `created_utc`, `source_snapshot`, `source_git_commit`, `snapshot_verified`.
  - `counts.{source_plan_rows, eligible_rows, blocked_source_rows, transfer_types{}}`.
- **`SHA256SUMS`:** covers both files. The **manifest hash** is the SHA-256 of `execution_manifest.csv`. Approvals and `SUCCESS` events are bound to it.

### 2.3 `approvals/<run_id>.json`

```json
{ "run_id": "…", "approved_execution_ids": ["…"],
  "history": [ { "action": "APPROVE" | "REVOKE", "execution_id": "…", "utc": "…",
                 "manifest_sha256": "…", "batch": { "utc", "media_type", "transfer_type", "size" } } ] }
```

- `batch` is present only on entries written by `--approve-batch`. Entries sharing a `batch.utc` were approved as one decision.
- A missing file means nothing in that run is approved.

### 2.4 `execution_logs/<execution_id>.jsonl`

One JSON object per line: `{utc, execution_id, event, …details}`. A journal can hold several attempts, for example a check-only run followed by a live run. Each attempt starts with `START {live}`.

| Event | Emitted by | Meaning |
| --- | --- | --- |
| `START` (`live`) | all | Attempt began; `live=false` is check-only |
| `PREFLIGHT_OK` | all | Every check passed. Details: `manifest_sha256`, paths, Arr id/version; TV adds `episode_file_count`, `linked_episode_count` |
| `CHECK_ONLY` | all | Check-only attempt ended cleanly |
| `RENAME_INTENT` → `RENAMED` | same-disk | NAS rename about to happen / done |
| `NFS_VISIBILITY_WAIT` / `_READY`, `NFS_RECOVERY_VERIFIED` | same-disk | NFS view catching up; Movie EINVAL resume |
| `COPY_INTENT` → `COPIED` | cross-disk | Staged copy + publish about to happen / done (`receipt`) |
| `RADARR_UPDATE_INTENT` / `SONARR_UPDATE_INTENT` | all | Arr path update about to happen |
| `DELETE_INTENT` → `SOURCE_REMOVED` | cross-disk | Verified source deletion about to happen / done |
| `RECOVERY_STARTED` | `recover_cross_*.py` | Manual incident recovery began |
| `SUCCESS` (`manifest_sha256`) | all | Move complete and verified |
| `STOPPED` (`error_type`, `reason`) | all | Attempt ended with a refusal or error; no automatic retry |

The exact sequences per executor and scenario are pinned in `tests/golden/executor_sequences.json`.

## 3. Fields the UI must ignore

These are written once as `false`/`NO` and never updated, because the files are immutable:

- `runs/*/metadata.json` `execution.{approved, executed}`.
- `manifest_metadata.json` `approval.*`, `execution.*`.
- The manifest CSV's `approved` / `execution_result` columns.

The `executed` column is still load-bearing: executors refuse any row where it isn't `NO`. The UI just must not read it as "not yet executed".

## 4. Derived state

**Approval state** (per `execution_id`). This is the same rule as `executor_manifest.approval_is_current()`:

- **Approved:** the ID is in `approved_execution_ids`, and its latest history entry is `APPROVE` with `manifest_sha256` equal to the current manifest hash.
- **Revoked:** the latest history entry is `REVOKE`.
- **Unapproved:** otherwise.

**Execution state** (per `execution_id`, from its journal). The first matching rule wins:

1. **Succeeded:** the last event is `SUCCESS` and its `manifest_sha256` matches the manifest. Mark it *recovered* if the journal contains `RECOVERY_STARTED`.
2. **Journal unreadable:** any line fails to parse.
3. **Unfinished:** the last event is not terminal (`SUCCESS`, `CHECK_ONLY`, `STOPPED`). The move is either running right now or was interrupted without writing `STOPPED`, and only the media host can tell which.
   - *Changed 2026-09-25 from "running if the journal changed in the last few minutes".* That heuristic was wrong: nothing is written to the journal during a long copy or hash (hours for a large series), so an old file time doesn't mean the process died. The engine's status sidecar (§5, gap 2) will separate the two cases.
4. **Needs reconciliation:** the journal contains any mutation event (`RENAME_INTENT`, `RENAMED`, `COPY_INTENT`, `COPIED`, `*_UPDATE_INTENT`, `DELETE_INTENT`, `SOURCE_REMOVED`, `RECOVERY_STARTED`) and no later `SUCCESS`, or it ends in a `SUCCESS` bound to a different manifest hash.
5. **Stopped safely:** the last event is `STOPPED` and there is no mutation event. The refusal came before anything changed, so fixing the cause and retrying is safe.
6. **Checked:** the last event is `CHECK_ONLY`.
7. **Not started:** no journal, or an empty one.

This matches what `batch_cross_*.py` `pending_*()` computes ad hoc today, minus their Movie/TV and cross-disk filters.

**Row display status:**
- Execution state wins whenever it is *succeeded*, *needs reconciliation* or *unfinished*.
- Otherwise combine the two, e.g. "Approved · Checked" or "Unapproved · Not started".

Blocked plan rows (`runs/*/move_plan.csv` `status=BLOCKED`) show as "Blocked" with their blocker codes; they have no `execution_id`.

## 5. Gaps found while writing this

1. **Plan rows have no Arr item ID.** `move_plan.csv` has no `radarr_id`/`sonarr_id`, so the UI can't reliably join a plan row to its dry-run row. Title is ambiguous; path formats differ (Arr path vs. `/mnt/nas` path). The executors already accept an optional `radarr_id`/`sonarr_id`/`item_id` column.
   - Proposal: have the planner carry it through.
   - This changes `move_plan.csv`, so it needs its own change behind the planner parity tests.
2. **No safe "is it running?" signal.** Proposal: the Step 2 engine writes `execution_logs/active.json`, containing `execution_id`, `pid`, `started_utc` and the last progress event, via atomic replace. The engine removes it when the attempt ends.
3. **Approvals are written non-atomically.** Proposal: write to a temp file, then `os.replace`. This is a small `approve_execution.py` change.
4. **Frozen status fields (§3) look authoritative but aren't.** Proposal: document them in the file formats, or drop the unused ones in a future `manifest_version` bump. `executed` must stay.

## 6. Read API for Step 4 (shared by the CLI and the UI)

| Function | Returns |
| --- | --- |
| `list_runs()` | `run_id`, `created_utc`, counts, integrity OK/failed, manifest present |
| `get_plan(run_id)` | All `move_plan.csv` rows (including blocked), joined to dry-run detail once gap 1 is fixed |
| `get_manifest(run_id)` | Rows, manifest hash, integrity |
| `get_approvals(run_id)` | Per-row approval state, history, batch groups |
| `status(execution_id)` / `run_status(run_id)` | §4 states; per-run counts and GB by state |
| `events(execution_id)` | Parsed journal, with `inventory`/`receipt` details collapsed by default |

All of these are pure reads of the files above, so they can ship before the executor refactor, and the web UI can use them unchanged. **Implemented** in `read_api.py` as `list_runs`, `get_plan`, `get_manifest`, `get_approvals`, `execution_state`, `events` and `run_status`. `migratarr_status.py` is the CLI over them: no arguments lists runs, `--run` shows the per-row status table, `--execution` shows a timeline, and `--json` gives machine-readable output.

## Sources

`snapshot_run.py`, `build_execution_manifest.py`, `build_move_plan.py` (`FIELDS`, blocker codes), `dry_run_movies.py`, `dry_run_tv.py`, `approve_execution.py`, `executor_manifest.py`, `execute_*.py`, `recover_cross_*.py`, `batch_cross_*.py`, `tests/golden/executor_sequences.json`, at `chore/release-housekeeping` @ `711b309`.
