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
6. - [ ] **Release candidate** (Josh): `v0.2.0rc1` published 2026-10-30 (`868c017`). Tag
         **`v0.2.0rc2`** after the Debian/log PR, since the image changed (`/ping` healthcheck,
         status in logs). PyPI marks it pre-release; Docker gets only `:0.2.0rc2` (no `:latest`).
         Check `docker pull ghcr.io/jwilcox99/migratarr:0.2.0rc2`, `--version`, and that the
         container turns healthy
7. - [ ] **Friend's install** (Josh): from `docs/docker.md` and `examples/docker/compose.yaml`
         only, with `:0.2.0rc2`. Fixes found here go into `main` before the final tag
8. - [ ] **Release** (Claude, then Josh): `__version__ = '0.2.0'`, the CHANGELOG heading gets
         the date; Josh tags `v0.2.0` (PyPI, GitHub release, Docker `:0.2.0`, `:0.2`, `:latest`)
9. - [ ] **After** (Josh, optional): delete merged branches (`feature/docker`,
         `feature/schedule-notify`, `feature/ui-login`, ...); announce
