# Unified execution core: Step 2 design spec

**Status:** Proposal, not implemented. Written 2026-09-24 against `main` @ `7a1d084`.
**Plan reference:** Step 2 of `2026-09-24-release-path-plan` and the Phase Two handoff in `docs/phase-one-closeout.md` ("a unified execution core that preserves the Phase One safety invariants while removing duplicated Movie/TV and same-disk/cross-disk code").
**Goal:** one execution engine replaces the four live executors. Every journal, refusal and Arr call stays exactly as it is today. The engine reports what it does as structured data, so the CLI now and a web UI later are both clients of the same code.

## 1. Baseline

| Script | Lines | Scope | Status |
| --- | --- | --- | --- |
| `execute_movie_nas.py` | 455 | Same-disk Movie, NAS rename over SSH, `--resume-nfs-refusal` | Live pipeline |
| `execute_tv_nas.py` | 472 | Same-disk TV, NAS rename over SSH | Live pipeline |
| `execute_cross_movie.py` | 541 | Cross-disk Movie, NAS staged copy + verified delete | Live pipeline |
| `execute_cross_tv.py` | 580 | Cross-disk TV, same mechanism | Live pipeline |
| `execute_movie.py` | 276 | Same-disk Movie, local rename, no NAS transport | Not in documented pipeline (README) |
| `batch_cross_movies.py`, `batch_cross_tv.py` | 111, 117 | Sequence cross-disk rows: approve, check-only, `--execute`, via subprocess | Live |
| `recover_cross_0102.py`, `recover_cross_0116.py` | 227, 157 | Incident-specific recovery | Frozen evidence |

Shared modules already exist: `executor_manifest.py` (`load_approved_plan`), `executor_command.py` (`run_command`), `executor_inventory.py` (`scan_metadata`), `executor_nfs.py` (`verify_after_rename`). They take `require` (and other helpers) as parameters instead of importing them. That matters because each executor ships NAS-side code by `inspect.getsource()` (see §2, D4).

### What is still duplicated

I compared every top-level function across the five executors by exact source text (`ast.get_source_segment`):

- **Byte-identical in every file that has it:** `Refused`, `require`, `canonical_existing`, `rename_noreplace`, `sync_parents`, `layout`, `posix` (all 5); `remote_path`, `file_metadata`, `inventory_metadata` (4 NAS executors); `content_only`, `copy_tree`, `remove_verified_tree`, `nas_operation`, `verify_destination`, `wait_source_absent` (both cross-disk); `Sonarr`, `episode_state`, `verify_episode_contents` (both TV); `paths` (3 same-disk).
- **Differ only in text, constants or formatting:** `inventory` across the 4 NAS executors (refusal wording "Directory contents changed while hashing" vs "Files changed while hashing", line wrapping); `progress` (wrapping); `load_plan` (media and transfer-type constants); `nas_pair`, `remote_layout` (the word Movie/TV in a message or docstring); `check_journal` in the cross-disk pair (`RADARR_` vs `SONARR_UPDATE_INTENT`); `run_progress` (timeout 1800 s same-disk, 7200 s cross-disk); `NasTransport` (a comment, plus the call signature per transfer type).
- **Real differences (domain logic, kept as separate implementations):**
  - *Transfer mechanism.* Same-disk: `RENAME_INTENT` → NAS `renameat2` → `RENAMED` → NFS visibility check → Arr update. Cross-disk: `COPY_INTENT` → NAS staged copy + atomic publish → `COPIED` → destination verify → Arr update → `DELETE_INTENT` → quarantine + verified delete → `SOURCE_REMOVED`.
  - *Arr item model.* Movie verifies one `movieFile`. TV verifies every episode file and the episode↔file association table (`episode_state`) before and after every mutation, and pins Sonarr to v4.
  - *Post-update settings check.* Cross-disk Movie compares `monitored`, `qualityProfileId`, `tags`. Both TV executors compare `seriesType`, `seasonFolder`, `monitored`, `qualityProfileId`, `tags`. Same-disk Movie has no settings comparison.
  - *Recovery.* Only same-disk Movie has `--resume-nfs-refusal`, a narrowly scoped resume for one diagnosed NFS `EINVAL` failure.

So the four live executors are really a 2 × 2 grid: {same-disk rename, cross-disk copy} × {Movie/Radarr, TV/Sonarr}. They are four copies of two mechanisms combined with two Arr models, plus about 20 helpers copied between them.

## 2. Design decisions

**D1. Mechanism × media composition.** The engine combines one `TransferStrategy` (`SameDiskRename`, `CrossDiskCopy`) with one `ArrAdapter` (`RadarrMovie`, `SonarrSeries`).
- The strategy owns the ordered steps, the intent/confirmation events and when to re-verify.
- The adapter owns: finding the Arr item for a logical path; a comparable snapshot of its state (Movie: the record and `movieFile`; TV: the record plus `episode_state`); content verification through the Arr's view; the path update; the post-update checks, including which settings fields must not change.

Each of the four current executors is exactly one pairing.

**D2. Journals stay byte-compatible.** Event names, detail fields and order are unchanged, including the adapter-specific names (`RADARR_UPDATE_INTENT`, `SONARR_UPDATE_INTENT`), `NFS_VISIBILITY_*`, `NFS_RECOVERY_VERIFIED`, and the `START`/`STOPPED` wrapper. Reasons:
- `batch_cross_*.py` and both recovery scripts read journals.
- Approval-bound `SUCCESS` events carry `manifest_sha256`.
- Old journals from real runs have to stay interpretable.

Each (strategy, media) pairing keeps its own "uncertain live attempt" event set in `check_journal`, exactly as today.

**D3. Structured output (the UI requirement).** The engine never prints. It emits two kinds of event to one or more sinks:
- `journal` events: exactly what is written to `execution_logs/<id>.jsonl` today.
- `progress` events: numeric fields instead of formatted strings, e.g. `phase="hash"`, `path`, `bytes_done`, `bytes_total`, `elapsed_s`, or `phase="arr_verify"`, `file_index`, `file_count`.

Sinks at launch:
- `JournalSink`: today's fsync-per-line JSONL writer, unchanged.
- `ConsoleSink`: reproduces today's `[HH:MM:SS] message` stderr lines closely enough for an operator. Stderr is not a contract; the journal is.

A run returns an `ExecutionResult(status, execution_id, last_event, error_type, reason, journal_path)`, where `status` is `SUCCESS`, `CHECK_ONLY` or `STOPPED`. Refusals stay `Refused` exceptions internally. The existing `STOPPED` handling turns them into a result at the boundary.

**D4. NAS-side code stays self-contained.** Each executor builds a Python program at run time from `inspect.getsource()` of chosen functions and pipes it to the NAS's `python3` over SSH. So NAS-side code must stay standard-library-only, have no project imports, and take its helpers as parameters or define them in the same text. All NAS-side functions move into one module, `nas_side.py`, with that constraint enforced by a test: parse the module and check that it imports nothing outside the standard library. The program builder ships that module plus a constant `ALLOWED_OPERATIONS`:
- same-disk: `{check, rename}`
- cross-disk: `{check, copy, delete}`

This keeps today's property that the same-disk program cannot copy or delete. The layout constants (`NAS_LAYOUT`) are embedded the same way they are now.

**D5. One NAS-side lock, renamed only after cutover.** All four NAS programs take the same advisory lock, `/tmp/migratarr-movie-<uid>.lock`. TV uses the literal word "movie" too, so in effect this is already a single global NAS lock. Keep that behavior. Rename it to `migratarr-nas-<uid>.lock` only in a later change, after every entry point uses the engine. A partial rollout where old and new code take different lock names would let them run concurrently on the NAS. The host-side `execution_logs/executor.lock` is unchanged.

**D6. Entry points stay the same until Step 4.** `execute_movie_nas.py`, `execute_tv_nas.py`, `execute_cross_movie.py` and `execute_cross_tv.py` remain, as thin wrappers with the same arguments, exit codes and stdout lines (`SUCCESS: <id>`, `CHECK_ONLY: <id>`, `STOPPED: …`). The batch scripts and operator muscle memory keep working, and the Step 4 CLI replaces them later.

**D7. Retire, don't port, `execute_movie.py`.** It has no NAS transport and is outside the documented pipeline. It gets deleted, not moved into the engine. Five test modules reference it (`test_executor_safety`, `test_media_layout`, `test_override_tags`, `test_runtime_config`, `test_service_urls`) and get updated to match. `recover_cross_0102.py` / `recover_cross_0116.py` move unchanged to `incidents/`, with `IncidentRecoveryTests` repointed.

**D8. Behavior-neutral first; fixes come later.** These differences are carried into the engine as per-pairing parameters, not unified. Each gets its own reviewed change afterwards:
- `run_progress` timeout: 1800 s same-disk vs 7200 s cross-disk.
- Refusal wording in `inventory()`.
- Same-disk Movie's missing post-update settings comparison.
- `--resume-nfs-refusal` is same-disk Movie only (not extended to TV).
- A stale comment, "Prompt once through the terminal; no credentials are stored by Python." It sits above an `ssh -o BatchMode=yes` call in `execute_tv_nas.py`, `execute_cross_movie.py` and `execute_cross_tv.py`. `BatchMode=yes` means no prompt is possible; `execute_movie_nas.py` has the correct comment.

## 3. Proposed layout

```
migratarr/                    # package; Step 4 adds cli.py and api.py alongside
  engine/
    primitives.py   # Refused/require, canonical_existing, rename_noreplace, sync_parents,
                    #   inventory (progress via callback), absorbs executor_{inventory,command,nfs,manifest}
    nas_side.py     # everything shipped to the NAS; stdlib-only (D4)
    transport.py    # NasTransport: ssh control master, program builder, receipts
    arr.py          # HTTP client (NoRedirect, service_endpoint), Arr file checks
    adapters.py     # RadarrMovie, SonarrSeries (D1)
    strategies.py   # SameDiskRename, CrossDiskCopy: ordered steps + re-verification points
    journal.py      # locking, JournalSink, per-pairing uncertain-event sets, resume detection
    events.py       # sink protocol, ConsoleSink, ExecutionResult (D3)
    run.py          # run(execution_id, *, base, live, sinks, options) -> ExecutionResult
```

The existing top-level `executor_*.py` modules stay importable (re-exporting from `migratarr.engine.primitives`) until nothing references them.

## 4. What the UI gets

Two new read functions that need no new storage, because they are computed from files that already exist:

- `status(execution_id)` is derived from the journal:
  - `NOT_STARTED`: no journal.
  - `CHECKED`: last attempt ended in `CHECK_ONLY`.
  - `SUCCEEDED`: `SUCCESS` with a matching `manifest_sha256`.
  - `NEEDS_RECONCILIATION`: any uncertain-live event without a later `SUCCESS`, or a `RECOVERY_STARTED`.
  - `IN_PROGRESS`: the host `executor.lock` is held and this journal's last event is `START` or an intent event with nothing after it.

  `batch_cross_*.py` `pending_series()` / `pending_movies()` already compute this ad hoc, and they also re-implement manifest verification instead of calling `load_approved_plan`. The engine's version replaces both.
- `events(execution_id)`: the parsed journal, for a timeline view.

Live progress for a running execution comes from `progress` events (D3). The UI does not need it until UI milestone 3 (start and monitor execution from the UI).

## 5. Migration and parity gates

| Step | Change | Gate before merging |
| --- | --- | --- |
| 0 | Merge `codex/host-verification` first. It edits all five executors (about 16 changed lines each, plus new `arr_files.py`); merging it after the refactor means porting it by hand. | Its own tests, and CI on 3.10–3.13 |
| 1 | Characterization only: golden-journal tests for all four pairings, recording the exact event sequence and detail keys for check-only, success and each existing failure case in `test_executor_safety*.py`; stdout/exit-code tests for each wrapper; a test that the generated NAS programs compile and reject operations outside their set. | New tests pass against **unchanged** executors |
| 2 | `migratarr/engine/primitives.py` + `nas_side.py`; executors import from them, with no behavior change. | Step 1 golden tests byte-identical; `NasReceiptTests` pass against the new NAS program |
| 3 | Adapters, strategies, `run()`; the four scripts become wrappers (D6). | All existing safety tests and Step 1 golden tests pass unmodified |
| 4 | **Live parity:** for one approved row of each pairing, run old and new in check-only mode and compare the journals, which must be identical except for `utc`. This is the same bar as `docs/executor-refactor.md` / `docs/validation-engine.md`. Then one watched `--execute` per pairing on the smallest eligible item. | Evidence recorded in this doc, as `docs/storage-targets.md` does |
| 5 | One `batch_cross.py --media {Movie,TV}` replaces both batch scripts; retire `execute_movie.py`; move recovery scripts (D7). | Existing batch tests ported; `--limit` behavior unchanged |
| 6 | Separate reviewed changes for each D8 item, then the lock rename (D5). | One PR each |

## 6. Decisions needed from Josh

1. **Package location:** `migratarr/engine/` (recommended, because Step 4 builds on it) vs. keeping flat top-level modules.
2. **Batch approval semantics.** `batch_cross_*.py --execute` runs `approve_execution.py --approve <id>` itself, for each pending row, just before executing it. So one batch command both approves and executes, and the human's decision is made per batch, not per item. That's reasonable operationally, but the public messaging ("Why every move needs approval") and the Step 6 UI approval flow should say the same thing. Options:
   - (a) Keep it, and document batch approval as a batch-level decision.
   - (b) Recommended: the engine and batch never approve. Batch approval becomes an explicit step first ("approve these N execution IDs", recorded in the approvals history like any other approval), and the batch executes only rows that are already approved.
3. Whether the D8 items should be unified during Step 6, and in which direction (for example, should same-disk Movie gain the settings comparison both TV executors have).

## 7. Out of scope

- Making the validation engine the live pre-execution gate (Step 3).
- The `migratarr` CLI and packaging (Step 4).
- New transfer types, multi-NAS or non-SSH topologies (Phase 15).
- Any scoring or planner change.
- Changes to the frozen `*_placement_v1.py` files or to the recovery scripts' logic.

## Sources

`execute_movie.py`, `execute_movie_nas.py`, `execute_tv_nas.py`, `execute_cross_movie.py`, `execute_cross_tv.py`, `batch_cross_tv.py`, `executor_*.py`, `recover_cross_0102.py`, `recover_cross_0116.py`, `tests/test_executor_safety.py`, `tests/test_executor_safety_phase2.py`, `docs/executor-safety-tests.md`, `docs/phase-one-closeout.md`, `README.md` — all at `main` @ `7a1d084`; `codex/host-verification` @ `ebd357c`.
