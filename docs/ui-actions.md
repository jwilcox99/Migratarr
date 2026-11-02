# Web UI actions: plan, confirm, run

`migratarr_ui.py` (without `--read-only`) adds three actions to the read-only
view described in `ui-data-contract.md`. They reuse the existing pipeline
rather than re-implementing it:

| Step | In the UI | What actually runs |
| --- | --- | --- |
| Plan | "Plan a new run" (Runs page) | A background job runs `migratarr_plan.py`: preflight, dry runs, plan, override audit, snapshot, manifest. Nothing is approved. |
| Confirm | Tick moves → "Review selected" / "Review all" → "Approve and start moving" (approves, then starts the move job for exactly those moves; while a job runs, only approves) or "Approve only" | `approval_store.approve()`: one `APPROVE` history entry per row, bound to the manifest hash, with a shared `batch` record (`source: "ui"`). Executors verify these exactly as they verify `approve_execution.py` approvals. |
| Run | For moves approved earlier: "Run N approved moves" (at the top, and under the approved moves) → "Start moving" | A background job runs each row's existing executor (`execute_movie_nas.py`, `execute_tv_nas.py`, `execute_cross_movie.py`, `execute_cross_tv.py`): check-only, then `--execute`. |
| Done | "Mark done" (Runs page, next to a run with moves still not made) or "Mark run done" (the scheduled-planning note); "Reopen" on the run page | `approval_store.close()`: withdraws the approval of every move that hasn't run (one `REVOKE` each, `reason: "run marked done"`), records `closed` and a `CLOSE` history entry in `approvals/<run_id>.json`. `reopen()` removes `closed` (`REOPEN` entry); withdrawn approvals stay withdrawn. |

## Safety rules

- **Pages never write.** Every GET only reads (tested by snapshotting the base
  path). The review and run-confirmation pages are reads too; only the final
  buttons write.
- **What you reviewed is what's recorded.** The approve and start forms carry
  the manifest hash and the exact execution IDs shown. If the manifest changed,
  or a row stopped being eligible or approved, the action is refused and nothing
  is written.
- **Same eligibility as the CLI.** Approvable rows are `READY_FOR_REVIEW`, no
  blockers, `executed` = `NO`, and not already moved; the UI offers checkboxes
  only for rows in "Not approved" (never for rows that need attention).
- **A run marked done runs nothing.** While `closed` is set, nothing in the run
  can be approved (UI, `migratarr approve`), and `approval_is_current()` is false
  for every row, so no executor or move job runs one even if an approval was
  added by hand. Marking done is refused while a move from the run is unfinished,
  needs reconciliation or has an unreadable journal, and while a job works on
  that run, so a half-done move is never buried. A run marked done doesn't hold
  up scheduled planning.
- **One job at a time.** The job process holds `jobs/runner.lock`; the UI
  refuses to start another while a job is starting, running, or was interrupted
  and not yet dismissed.
- **Move jobs never approve and never interrupt a move.** Before each row, smallest first:
  1. the manifest hash is unchanged (else the job stops);
  2. the row's journal is `NOT_STARTED`, `CHECKED` or `STOPPED_SAFE`, with no
     recovery started (else the job stops before it);
  3. its approval is still current (else it's skipped: revoking from the UI or
     CLI takes effect before the next move);
  4. **every folder of the source has group write** (else it's skipped,
     nothing changed). The NAS-side executors delete and rename as the NAS user
     through the shared group, and Radarr/Sonarr write through it too. Nimrods
     (`20261015T174112Z-0004`) copied fully and then stopped at the delete
     because its folder was `2755`; this check refuses that case up front;
  5. the runtime config is unchanged (else the job stops).

  Then check-only runs. A check-only refusal changed nothing, so that row is
  skipped and the job continues. A live failure stops the job, like
  `batch_cross_*.py`, and the row's journal says what to reconcile. The job
  requires a `SUCCESS` bound to this manifest before moving on.
- **Stop after this move.** The job page's stop button writes
  `jobs/<job_id>.stop`. The job checks it before each move and between
  check-only and live, never during an executor.
- **Interrupted jobs are reported, not cleared.** If the job process is gone
  without finishing, the UI says so and offers "Dismiss" only after you've
  looked; any move it was running shows as in progress or needs attention.
- **Loopback by default.** The server binds 127.0.0.1 and answers only
  `localhost` host names. With `--host` set to another address, every page,
  API call and action needs a signed-in session (`migratarr/auth.py`), and any
  host name is answered. Every form carries a random per-process token;
  cross-site requests (`Origin` / `Sec-Fetch-Site`) and non-form bodies are
  refused. The CSP allows forms to post only to the UI itself.

## Files

`jobs/` under the base path (git-ignored, so planning's clean-tree preflight
still passes):

| File | Contents |
| --- | --- |
| `active.json` | The running job: `job_id`, `kind`, `pid`, `run_id`, `queue`, `current` (`execution_id`, `index`, `total`, `step`), `done`, `skipped`, `message`. Replaced atomically. |
| `<job_id>.log` | Everything the job and its executors printed. The job page shows the tail. |
| `<job_id>.json` | The finished job's record, same fields plus `finished_utc` and final `state` (`FINISHED`, `STOPPED`, `FAILED`, `INTERRUPTED`). |
| `<job_id>.stop` | Stop request, removed when the job ends. |
| `runner.lock` | `flock`ed by the job process for its lifetime. |

## Running it

Start the UI in tmux on the media host with the same environment you'd plan
from (for example `TMDB_TOKEN`), because jobs inherit it. Jobs keep running if
the UI stops; restart the UI and the job page picks them up again.

## Not covered yet

- Moves started from the CLI don't write `jobs/active.json`, so the UI still
  infers them from journals (`ui-data-contract.md` §5, gap 2).
- Recovery (`recover_cross_published.py`) and manual reconciliation stay CLI
  steps.
- The permission check is made from the media host's view of the NFS mounts
  (group write bit), not as the NAS user.

## Live evidence

Production library, run `20261023T212605Z`, started from the UI (`main` @ `cbcfe07`).

| Job | Outcome | Moved |
| --- | --- | --- |
| `20261023T213816Z-run` | `STOPPED` as requested, between moves ("Stopped as requested before Scooby-Doo and the Ghoul School (1988)") | 25 titles, 217.79 GB, smallest first (Scott Pilgrim vs. the World, 0.41 GB, up to It Follows, 23.87 GB) |
| `20261025T204507Z-run` | `FAILED` on *The Hunger Games: Mockingjay – Part 2* (`-0031`); later moves not started | 4 titles, 97.21 GB |

Together: 29 titles, 315.0 GB. Plan, approve, run, stop, and a fresh job resuming the remaining queue all worked.

Five of the moved titles (Kingsman, Trapped in Paradise, Scooby-Doo! and the Witch's Ghost, The Equalizer, The Young Girls of Rochefort) had `2755` source folders, the case that stopped Nimrods (`20261015T174112Z-0004`) at its delete. They moved cleanly after `chmod g+w`.

### Incident: `20261023T212605Z-0031` stopped during the NAS copy

- Journal: `PREFLIGHT_OK` → `COPY_INTENT` → `STOPPED` (`Refused`, "NAS copy failed or is uncertain"), 2026-10-26 07:58–08:08 UTC. The NAS program hashed the 25.10 GiB source, printed "Copying …", and ended about four minutes later with no error output. That is well inside the 1,800 s silence timeout; the cause (dropped SSH session or the NAS ending the process) isn't recorded.
- State checked 2026-10-26: source intact (26,951,161,512 bytes); no published destination; a partial `.migratarr-stage-20261023T212605Z-0031` (owned by the NAS user, `0700`) on media04; 383 GB free there. No Radarr change was attempted.
- Cleanup: the staging folder was removed on the NAS by hand. The row stays `NEEDS_RECONCILIATION`; the next run will plan the move again.

### Follow-ups found

1. **Record a reconciliation.** Two rows (`20261015T174112Z-0004`, `20261023T212605Z-0031`) were reconciled by hand but still show "needs attention". A recorded "mark reconciled" action is needed.
2. **Capture why a NAS step ended.** The copy failure kept only the last stderr lines, not the exit status or signal, so the cause can't be told apart after the fact.
