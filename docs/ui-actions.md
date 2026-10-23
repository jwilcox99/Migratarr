# Web UI actions: plan, confirm, run

`migratarr_ui.py` (without `--read-only`) adds three actions to the read-only
view described in `ui-data-contract.md`. They reuse the existing pipeline
rather than re-implementing it:

| Step | In the UI | What actually runs |
| --- | --- | --- |
| Plan | "Plan a new run" (Runs page) | A background job runs `migratarr_plan.py`: preflight, dry runs, plan, override audit, snapshot, manifest. Nothing is approved. |
| Confirm | Tick moves → "Review selected" / "Review all" → "Approve N moves" | `approval_store.approve()`: one `APPROVE` history entry per row, bound to the manifest hash, with a shared `batch` record (`source: "ui"`). Executors verify these exactly as they verify `approve_execution.py` approvals. |
| Run | "Run N approved moves" → "Start moving" | A background job runs each row's existing executor (`execute_movie_nas.py`, `execute_tv_nas.py`, `execute_cross_movie.py`, `execute_cross_tv.py`): check-only, then `--execute`. |

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
- **Loopback only.** The server binds 127.0.0.1 and answers only `localhost`
  host names. Every form carries a random per-process token; cross-site
  requests (`Origin` / `Sec-Fetch-Site`) and non-form bodies are refused. The
  CSP allows forms to post only to the UI itself.

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
