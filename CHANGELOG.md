# Changelog

All notable changes to Migratarr. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/) (0.x: settings files and commands
may still change between minor versions, always with a migration or a note here).

## [Unreleased] — toward 0.1.0

First public release: set up and run Migratarr on your own stack without
hand-editing JSON. Tracked in `reviews/2026-10-26-public-setup-plan.md`.

### Added
- `pip install migratarr`: an installed copy keeps settings, runs and logs in one data
  folder, `$MIGRATARR_HOME` (default `~/.local/share/migratarr`). A git clone keeps
  using the clone, so existing setups don't change. `migratarr doctor` shows which.
- `migratarr` command: `ui`, `plan`, `status`, `approve`, `doctor`, `reconcile`.
- Settings pages and first-run setup in the web UI; planning is refused while any
  health check fails (warnings are fine).

### Changed
- All code is in the `migratarr` package; an install adds only `migratarr` to
  site-packages. In a clone, `python3 <script>.py` keeps working through small
  wrapper files. Commands that need settings print what's missing instead of a
  traceback.
- Pipeline steps and executors start as `python -m <module>`, so they work from an
  install. Run snapshots record the Migratarr version and a hash of the code; the git
  commit and the clean-tree check apply only to a clone.

### Built, switched on when the settings layer lands
- Settings pages in the web UI: edit a section, review the exact change, save
  with a backup. Saves are refused while a job runs or if the files changed
  since review. Pasted keys are never shown back.
- First-run setup in the web UI: services, where moves run, disks and
  categories, streaming, then review and save; a first plan only after the
  health checks pass.

### Planned before 0.1.0
- One definition per disk (settings v2) with automatic, backed-up migration.
- `migratarr doctor` and the health checks behind the Settings page.
- Moves on this machine's own disks (no NAS) through the unified execution engine.
- Demo mode on a synthetic library; rewritten SETUP and README.

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
