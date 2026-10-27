# Execution transport, local disks, and flagged moves

**Status:** Phase 3 of `reviews/2026-10-26-public-setup-plan`, first step. Written 2026-10-26 on `feature/engine-gate2` (gates 1–2) merged with the Phase 1 settings foundation.

## Transport (`migratarr/engine/transport.py`)

Every executor builds one standard-library program (pinned byte-for-byte by `tests/golden/nas_programs.json`) and sends it a JSON request on stdin. `NasChannel` decides only how that program is started; each executor's `NasTransport` now subclasses it and keeps its own `call()` (payloads, responses and refusals are unchanged).

| `topology` | Program runs | Command |
| --- | --- | --- |
| `nas_ssh` | on the NAS | `ssh <options> <user>@<host> <nas.python> -c <program>` through one control master per run. Options and commands are exactly the ones the executors used before; `tests/test_local_transport.py` pins the full argv of the master, each call and the exit. |
| `local` | on this host | `<this Python> -I -c <program>`; no SSH. Disk "remote" roots equal the local ones (`runtime_config.storage_from_targets`), so the same program renames (same disk), or stages, publishes and deletes (cross disk) on this host's filesystems. |

Everything else is shared by both: the approval checks, preflight, journals and their events, the NAS-side lock (`/tmp/migratarr-movie-<uid>.lock`, D5), receipts, the NFS visibility check (immediate on local disks), and the Radarr/Sonarr updates. Cross-disk moves still require the two disks to be different filesystems; same-disk moves still require one. `recover_cross_published.py` uses the channel too; the frozen incident scripts (`recover_cross_0102.py`, `recover_cross_0116.py`) still build SSH commands themselves and are NAS-only.

**Evidence:** `tests/test_local_transport.py` runs the real shipped programs on this host through all four executors' transports: same-disk rename (Movie, TV), refusal when the source changed after hashing, and cross-disk check → copy → delete between `/tmp` and `/dev/shm` (Movie, TV), ending with no staging or quarantine folders left. All gate 1 golden journals and the gate 2 program pins are unchanged.

**Not yet evidenced:** a full executor run in `local` topology against real Radarr/Sonarr. The plan's open risk stands: local mode needs a real-world test outside Josh's NAS setup before it is offered in the wizard. `nas_ssh` behaviour is unchanged by construction (same argv, same program text), but the plan's live check-only parity run on Josh's library still applies before release.

## Exit status and signal on failures

`executor_command.run_command` now ends a failure reason with how the command ended, e.g. `NAS copy failed or is uncertain: Copied 3/7 files [exit status 255: an SSH error, or the NAS program ended by a signal]`, or `[killed by SIGKILL on this host]`. Before, only the last stderr lines were kept, so an ending with no output (incident `20261023T212605Z-0031`) left nothing to diagnose. The text goes into the journal's `STOPPED` reason; existing reasons gain only the bracketed suffix.

## Mark reconciled (`reconcile_store.py`)

A move in `NEEDS_RECONCILIATION`, or `UNFINISHED` with nothing running, stays flagged until someone checks it by hand. Recording that check:

```sh
python3 migratarr_reconcile.py 20261023T212605Z-0031 --outcome not_moved \
    --note "NAS copy ended silently; source intact, Radarr unchanged, staging folder removed by hand"
```

or **Mark reconciled** on the move's page in the UI (actions on, no job running). Outcomes: `moved`, `not_moved`, `other`; the note is required.

- Stored in `execution_logs/reconciled/<execution_id>.json` as an append-only history; journals, approvals, media and Arr records are never touched.
- Bound to the journal's SHA-256: any later journal line (a new attempt) makes the record stop counting, and the move is flagged again.
- `read_api` reports a matching record as the new state `RECONCILED` (detail = the note); the UI lists it under "Moved" as "Reconciled by hand". Move jobs never run it (`RUNNABLE_STATES` unchanged); the next plan re-plans the title from what is on disk.
- Refused while a UI job or executor runs; both locks are held while writing.

## Next: gate 3 and beyond

Unchanged from `docs/unified-execution-core.md` §5. Gate 3 (adapters, strategies, `run()`, scripts as thin wrappers) builds on this channel: `run()` takes a `NasChannel` instead of constructing SSH itself. Gate 4's live parity on Josh's library should now also include one check-only run per pairing to confirm the channel's commands on the real NAS.

## Sources

`execute_movie_nas.py`, `execute_tv_nas.py`, `execute_cross_movie.py`, `execute_cross_tv.py`, `executor_command.py`, `read_api.py`, `migratarr/engine/nas_side.py` on `feature/engine-gate2` @ `e1ce07b`; `reviews/2026-10-26-progress-update.md` (incidents `-0004`, `-0031`).
