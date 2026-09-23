# Contributing to Migratarr

Thanks for looking at this. Migratarr moves real media files with no
automatic rollback, so the bar here is "prove it's safe," not just "prove it
works." A few conventions the existing code already follows — please keep
following them.

## Getting set up

- **Python 3.10+**, Linux. `migratarr_validation/` uses PEP 604 union
  annotations (`int | None`, e.g. `migratarr_validation/engine.py`,
  `impact.py`) without `from __future__ import annotations`, so they're
  evaluated at import time and require 3.10+ — Python 3.9 will raise a
  `TypeError` on import. The project intentionally has no third-party
  dependencies — if you're about to add one, that's worth a discussion
  first, not just an import.
- You won't have live access to the original operator's media stack
  (Radarr/Sonarr/Jellyfin containers, NAS SSH). Anything that requires that
  access should be called out explicitly in your PR description as
  "not run live" versus "run live, here's the result," rather than implied.
- Run the test suite before opening a PR: `python -m unittest discover -s tests -v`.
  `tests/` is on `main` as of the Phase One closeout
  (`docs/phase-one-closeout.md`) — keep it green, and add to it if you're
  touching `migratarr_validation/`.

## Branch and PR workflow

- Work in a branch off `main`, and **open a PR even if you're the only
  reviewer for now.** A PR gives a fixed diff to review against and a
  place to record what was and wasn't validated — a raw pushed branch
  doesn't.
- Rebase or merge `main` into your branch before asking for review if
  it's drifted; don't let a branch sit unmerged and diverging for weeks.
- Small, reviewable PRs beat one big one, especially for anything touching
  `execute_*.py`.

## The parity-testing pattern

If you're changing logic in `build_move_plan.py`, `dry_run_movies.py`, or
`dry_run_tv.py` — the scoring/planning code — follow the precedent set in
`docs/validation-engine.md` (on `main` as of the Phase One closeout):
characterize the existing behavior first (tests that pin down what the current code
actually does, with citations to the exact function/lines you're
reproducing), *then* change it, and report a parity comparison against
real saved data before claiming the new version is equivalent. "I ported
the logic and it looks right" is not sufficient for code that decides
where someone's media library lives.

## Code conventions already in use

- **Fail loud, never guess.** The codebase's dominant pattern is a
  `require(condition, message)` helper (or an equivalent explicit check)
  that raises rather than falling back to a default when state is
  ambiguous — e.g. an item found on two disks blocks with
  `SOURCE_MISSING`-style errors instead of picking one. New code should
  match this: prefer refusing over guessing.
- **Read-only until explicitly told otherwise.** No stage before the
  executors touches a source or destination media file. They do write more
  than just their named CSV/JSON output, though: the placement scripts
  cache external API lookups as JSON under
  `cache/`, and `snapshot_run.py` writes a whole `runs/<timestamp>/`
  directory — copies of the score/plan CSVs, selected current code files,
  `metadata.json`, and `SHA256SUMS`. Keep code unchanged between planning
  and snapshotting. Executors default to check-only
  and require an explicit `--execute` flag.
- **Journal before you mutate.** If you're adding a new code path that
  changes filesystem or Arr state, write a journal entry before the
  mutation and another after, following the pattern in
  `execute_movie_nas.py`.
- **Verify by content hash, not just by name/size.** Renames and moves
  are confirmed by hashing file contents before and after, on both the
  local and Radarr/Sonarr-visible paths.

## Areas that could use help

See the README's [Known limitations](README.md#known-limitations) section
and `docs/phase-one-closeout.md`'s Phase Two handoff. In particular:

- De-duplicating `execute_movie_nas.py`, `execute_tv_nas.py`,
  `execute_cross_movie.py`, and `execute_cross_tv.py` into a shared module —
  right now a safety fix has to be manually ported across near-identical
  files (`executor_command.py`, `executor_inventory.py`, and
  `executor_manifest.py` already extract some shared pieces; the four
  execution flows themselves are still separate).
- Promoting `migratarr_validation/` from a parity-checked, read-only
  layer into the actual pre-execution gate.
- Expanding runtime layout support beyond the conservative path-shape contracts
  documented in [runtime configuration](docs/runtime-configuration.md).

## Reporting bugs

Open an issue with: what you ran, the exact command/execution ID if
relevant, and — if a file operation was involved — the contents of the
matching `execution_logs/<execution_id>.jsonl` journal entry. Do not paste
your Radarr/Sonarr API keys or NAS credentials into an issue.

See [SECURITY.md](SECURITY.md) for anything that should be reported
privately instead of as a public issue.
