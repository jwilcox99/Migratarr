# 0.2.0 release checklist

Docker image, UI login on the network, scheduled plans and notifications, in one release.
Tag `v0.2.0` only when every box is ticked. Owner: **Josh**, or **Claude** (work handed over as
pull requests). The 0.1.0 checklist is [release-checklist.md](release-checklist.md).

## Decisions (Josh)

| Date | Decision |
| --- | --- |
| 2026-10-28 | Docker image to run Migratarr like Radarr and Sonarr; UI reachable on the LAN |
| 2026-10-29 | Radarr-style username and password login |
| 2026-10-29 | amd64 + arm64, GHCR only, SSH client in the image, `:edge` from `main` |
| 2026-10-29 | Audience: Radarr/Sonarr in Docker or installed directly; local disks first |
| 2026-10-29 | Scheduled runs plan a batch and notify; approving and moving stay manual in the UI |
| 2026-10-29 | Notifications: Discord, generic webhook (JSON), email |
| 2026-10-30 | Skip + one reminder while the last batch waits; simple day/time picker; notify on batch ready, run finished, failures; links via a "UI address" setting |
| 2026-10-30 | Docker install and schedule + notifications ship together as **0.2.0** (not split into 0.2.0 / 0.3.0) |
| 2026-11-02 | "Mark run done" (PR #26) ships in 0.2.0, checked with `:edge` on a VM instead of an rc3 |
| 2026-11-03 | Release without the friend's install first: tested on several VMs (Ubuntu 26.04, Debian 13) instead. Release date 2026-11-03 |

## Done

- [x] UI login, `--host`/`--port`, `migratarr password` (PR #14)
- [x] Docker image, container-aware setup, `docs/docker.md`, `examples/docker/` (PR #15)
- [x] Scheduler and notifications, `docs/notifications.md` (PR #16)
- [x] All of it merged to `main` (PR #17, `d66df6d`); `:edge` published from `main`

## To do, in order

1. - [x] **Version** (Claude): `0.2.0rc1` in `migratarr/cli.py`; CHANGELOG `[0.2.0]` section;
         README status and local-disk wording
2. - [x] **GHCR package public** (Josh, 2026-10-30): Packages > migratarr > Package settings > Change
         visibility. Check with `docker logout ghcr.io && docker pull ghcr.io/jwilcox99/migratarr:edge`
3. - [x] **Container test on the VM** (Josh, 2026-10-30, `:edge` @ `d66df6d`): setup through the
         container's wizard (login, pasted keys, container URLs), health checks OK, a real same-disk
         move (Night of the Living Dead) and cross-disk move (Nosferatu): `./kit.py status` ok for
         every title, nothing left behind, no errors in the container log. Found and fixed: a pasted
         TMDB token was dropped (PR #19); fewer clicks to approve and run, newest output first
         (PR #20). Not recorded: the moved files' owner on disk (VM wiped first); covered by the
         smoke test (UI and its jobs run as `PUID`, nothing in `/config` owned by root) and by the
         "check your first move" step in `docs/docker.md`.
         **Again on Debian 13** (Josh, 2026-10-30, `:0.2.0rc1`, `DOCKER-WALKTHROUGH.md` as a new
         tester): same two moves, `./kit.py status` ok for all six titles, nothing left behind, disks
         on separate filesystems; moved folders `jwilc:jwilc 2775`, files 664 (uid/gid 1000), nothing
         in `/config` owned by root; container log clean. Found: Debian has no `docker-compose-v2`
         (walkthrough step 1); the compose example had no Jellyfin; the log had no status codes and a
         healthcheck line every 30 s. All fixed for rc2
4. - [ ] **A real notification** (Josh, optional): runbook step 11, or accept the risk
5. - [ ] **arm64** (Josh, optional): pull and start the image on an arm64 machine, or accept the
         risk (only built in CI, never run)
6. - [x] **Release candidate** (Josh): `v0.2.0rc1` published 2026-10-30 (`868c017`). Tag
         **`v0.2.0rc2`** after the Debian/log PR, since the image changed (`/ping` healthcheck,
         status in logs). PyPI marks it pre-release; Docker gets only `:0.2.0rc2` (no `:latest`).
         Check `docker pull ghcr.io/jwilcox99/migratarr:0.2.0rc2`, `--version`, and that the
         container turns healthy
7. - [x] **Friend's install** (Josh): skipped (decision 2026-11-03); covered by Josh's runs of
         `DOCKER-WALKTHROUGH.md` on fresh Ubuntu and Debian VMs. `v0.2.0rc2` published 2026-10-30
         (`89b2e2d`, includes #24 and #25)
   - [x] **Mark run done** (Josh, 2026-11-03, `:edge` @ `a217f6a`, Ubuntu VM): mark done from the
         Runs page, CLI approve refused, Reopen; same run: Night of the Living Dead (same disk) and
         Nosferatu (cross disk) moved, all six titles `ok`, owner 1000:1000, files 664, log clean with
         status codes and no healthcheck lines. First try ran an older cached `:edge` (pull before
         `docker run`); found on the way: kit init traceback on root-owned folders (now explained),
         titles showed 0.0 GB (now 0.1-0.2 GB), TMDB default (#28, after 0.2.0)
8. - [x] **Release** (Claude, then Josh, 2026-11-03): `__version__ = '0.2.0'`, CHANGELOG
         `[0.2.0] — 2026-11-03` (PR #27); `v0.2.0` tagged on `297be14`; Release, docker and test
         workflows green; GitHub release (not pre-release), PyPI 0.2.0, Docker `:0.2.0`, `:0.2`,
         `:latest`. Checked on a fresh VM: plain `pipx install migratarr` installs 0.2.0;
         `:latest` reports `migratarr 0.2.0`. PyPI trusted publisher constrained to the `pypi`
         environment (2026-11-03)
9. - [ ] **After** (Josh): delete merged branches and turn on "Automatically delete head branches";
         repo topics and a description that mentions Docker; announce (r/selfhosted on New Project
         Friday, 2026-11-06: `migratarr/marketing/launch-0.2.0.md` in the project)

## Next (0.2.1)

- #28: TMDB key source defaults to "environment variable" when none is set (pipx/venv installs)
- #29: "Add disk" button in the Add a disk box (draft PR)
