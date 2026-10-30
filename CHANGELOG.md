# Changelog

All notable changes to Migratarr. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/) (0.x: settings files and commands
may still change between minor versions, always with a migration or a note here).

## [Unreleased]

## [0.2.0] — release date set when tagged

Run Migratarr like Radarr and Sonarr: a Docker image, the web UI on your
network behind a login, and scheduled plans that tell you when a batch is
ready. Every move is still reviewed and approved in the UI before it runs.

### Added
- The web UI can be used from your network, like Radarr and Sonarr:
  `migratarr ui --host 0.0.0.0` (or `MIGRATARR_HOST`; `MIGRATARR_PORT` for the
  port). Then every page needs a sign-in, and the first visit creates the login.
- One UI login (username and password, hashed in `config/auth.json`), required
  whenever it is set. `migratarr password` sets or changes it and ends every
  session. Repeated wrong passwords are slowed down. In a git clone `config/auth.json` is
  git-ignored, so planning's clean-tree check still passes.
- Docker image `ghcr.io/jwilcox99/migratarr` (amd64, arm64): the UI on port 8765,
  settings and history in `/config`, `PUID`/`PGID`/`UMASK`/`TZ` like linuxserver
  images, an SSH client for NAS setups, no Docker socket needed. `:edge` follows
  `main`. Guide and compose example: `docs/docker.md`, `examples/docker/`.
- README: install with `pipx` first (a plain `pip install` is refused on current
  Debian and Ubuntu), then Docker.
- In the container, setup offers what works there: Radarr, Sonarr and Jellyfin at
  their container names, pasted keys, and file checks through Migratarr's own media
  mount (no Docker socket).
- Schedule & notifications (Settings & health): plan automatically on chosen days
  at a time; messages to Discord, a webhook (JSON) or email when a batch is ready,
  moves finish or something fails, with links to the UI. A scheduled plan waits
  while the last batch is still waiting (one reminder; "Done with this batch" on the
  run page) or a health check fails. Never approves or moves anything.
  `docs/notifications.md`.

### Fixed
- Setup and Settings > Services: a key typed into the paste box is now used even
  when the dropdown still said another source (TMDB defaulted to "environment
  variable", so a pasted token was silently dropped). In the Docker image, TMDB
  now defaults to "paste" unless `TMDB_TOKEN` is set.

## [0.1.0] — 2026-10-30

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
- Setup and Settings > Services ask whether Radarr and Sonarr run in Docker
  containers (the default) or are installed directly on this machine. The second
  sets host-side Arr file checks, hides the container fields, and Detect looks
  for config.xml on this machine.
- `migratarr doctor` and Settings & health check that the Arr file checks can
  run (new `arr_files` group): the container sees the media root, or, for
  Radarr and Sonarr installed directly, the folder exists here.
- README screenshots of a plan and the Settings & health page.
- Tested on Python 3.14.

### Changed
- Settings version 2: one definition per disk. Version 1 files are migrated
  automatically, with a backup.
- The two one-off recovery scripts for September 2026 incidents (`recover_cross_0102`,
  `recover_cross_0116`) are no longer installed; they live in the clone's `incidents/`.
  `recover_cross_published` is the general tool and stays.
- Developer notes moved to `docs/dev/`; `docs/` holds the user documentation.
- All code is in the `migratarr` package; an install adds only `migratarr` to
  site-packages. In a clone, `python3 <script>.py` keeps working through small
  wrapper files. Commands that need settings print what's missing instead of a
  traceback.
- Pipeline steps and executors start as `python -m <module>`, so they work from an
  install. Run snapshots record the Migratarr version and a hash of the code; the git
  commit and the clean-tree check apply only to a clone.
- The four `execute_*.py` executors now run on one shared execution engine (`migratarr/engine`); journals, refusals and commands are unchanged.

### Fixed
- Planning on "this machine's own disks" stopped at the storage consistency check
  ("remote_path disagrees"); local disks have no NAS path.
- Setup wizard: a busy port is reported with the next free port to try instead of a
  traceback; missing NAS fields and a host/user mix-up are reported on the field.
- Docker's own error messages (for example when Docker isn't installed or
  running) no longer appear in the terminal. Health checks and Detect say what
  went wrong instead.
- `migratarr approve --help` works before setup, like every other command.
- A rejected key says which key to use and links where to get it; for TMDB,
  the API Read Access Token rather than the shorter API Key.
- Hints and help text in an installed copy: settings-file fixes link the docs on
  GitHub, and commands read `migratarr doctor` (and so on) instead of the
  clone-only `python3 migratarr_*.py` scripts.
- The run page no longer shows an empty "code ," label in an installed copy.
- `migratarr demo` no longer prints dozens of "TMDB unavailable – retrying"
  lines, and `--dir` accepts a relative folder.
- Local-disk test kit (`examples/local-test/`): disk2 is now a loop-mounted ext4
  image, a separate filesystem from disk1, so the cross-disk test moves are no longer
  refused ("NAS filesystems are not distinct"). `kit.py init` and `up` print each
  `sudo` command before running it; `--no-loop` keeps plain folders. `down --delete`
  unmounts it and removes the read-only run snapshots without `sudo`.
- Pasted keys are no longer lost when a setup step or a Settings form comes back
  with a problem. The key stays in memory only (never in the page) and the field
  says it is kept; leave it blank to keep it.
- Health checks flag two disks on the same filesystem: a failure when a category
  placement moves items between them (those moves can't run), otherwise a
  warning. Local disks are compared here, NAS disks on the NAS.
- A cross-disk move refused because both disks are on one filesystem shows one
  plain line on the move and job pages and in the terminal, not a Python
  traceback. Other refusals by the NAS-side program drop the traceback too.
- Jellyfin: the key is sent as `Authorization: MediaBrowser Token="..."`. Current
  Jellyfin (12.4) refuses the old `X-Emby-Token` header, so the health check
  failed with HTTP 401 and planning could not read watch history.

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
