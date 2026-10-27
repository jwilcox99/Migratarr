# Changelog

All notable changes to Migratarr. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/) (0.x: settings files and commands
may still change between minor versions, always with a migration or a note here).

## [Unreleased]

## [0.1.0] — unreleased

First public release: install with pip, set up and run Migratarr on your own
stack in the browser without hand-editing JSON. Every move is still planned,
reviewed and approved before anything runs.

### Added
- `pip install migratarr`: an installed copy keeps settings, runs and logs in one data
  folder, `$MIGRATARR_HOME` (default `~/.local/share/migratarr`). A git clone keeps
  using the clone, so existing setups don't change. `migratarr doctor` shows which.
- `migratarr` command: `ui`, `plan`, `status`, `approve`, `doctor`, `reconcile`, `demo`.
- First-run setup in the web UI: services, where moves run, disks and categories,
  streaming, then review and save. Detect reads Radarr's and Sonarr's keys from
  their containers.
- Settings pages in the web UI: edit a section, review the exact change, save with
  a backup. Saves are refused while a job runs or if the files changed since review.
  Pasted keys are stored owner-only and never shown back.
- Health checks (`migratarr doctor`, and the Settings & health page). Settings may be
  saved with failing checks, but planning is refused until none fail (warnings are fine).
- **Experimental:** moves between disks attached to the machine running Migratarr
  (no NAS), through the same plan, approval and safety checks as NAS moves.
- `migratarr demo`: plans a made-up library with the real pipeline, offline, and
  opens the UI read-only. No Radarr, Sonarr, Jellyfin or TMDB needed.
- `examples/local-test/`: throwaway Radarr, Sonarr and Jellyfin containers for trying
  local-disk moves without touching a real library.

### Changed
- Settings version 2: one definition per disk. Version 1 files are migrated
  automatically, with a backup.
- All code is in the `migratarr` package; an install adds only `migratarr` to
  site-packages. In a clone, `python3 <script>.py` keeps working through small
  wrapper files. Commands that need settings print what's missing instead of a
  traceback.
- Pipeline steps and executors start as `python -m <module>`, so they work from an
  install. Run snapshots record the Migratarr version and a hash of the code; the git
  commit and the clean-tree check apply only to a clone.

### Fixed
- Planning on "this machine's own disks" stopped at the storage consistency check
  ("remote_path disagrees"); local disks have no NAS path.
- Setup wizard: a busy port is reported with the next free port to try instead of a
  traceback; missing NAS fields and a host/user mix-up are reported on the field.

## History before 0.1.0 (personally operated, not released)

- **2026-10** Web UI: plan, confirm and run moves from the browser; one-command
  planning (`migratarr_plan.py`); refuse stale pipeline inputs; cross-disk
  copies keep source folder permissions.
- **2026-09 (late)** Read-only status layer and `migratarr_status.py`; NAS calls
  time out on silence rather than total duration; recovery for a published copy
  whose NAS call timed out; batch approval as its own recorded step; golden
  characterization of the four executors; CI on Python 3.10–3.13.
- **2026-09 (mid)** Everything deployment-specific moved into config:
  runtime host settings, storage targets, media layout, service credentials,
  streaming, override tags and scoring, each with real-data parity evidence.
  Cross-disk TV executor and batch runners for Movies and TV.
- **2026-09 (early)** Placement v1 for Movies and TV, immutable run snapshots,
  same-disk and cross-disk executors validated on a live library, read-only
  validation engine and parity harnesses.
