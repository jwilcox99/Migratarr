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
2. - [ ] **GHCR package public** (Josh): Packages > migratarr > Package settings > Change
         visibility. Check with `docker logout ghcr.io && docker pull ghcr.io/jwilcox99/migratarr:edge`
3. - [ ] **Container test on the VM** (Josh): `2026-10-30-docker-vm-runbook.md`, steps 1–10:
         health checks OK in the container, a real same-disk and cross-disk move, files owned by
         `PUID`/`PGID`, nothing in `/config` owned by root
4. - [ ] **A real notification** (Josh, optional): runbook step 11, or accept the risk
5. - [ ] **arm64** (Josh, optional): pull and start the image on an arm64 machine, or accept the
         risk (only built in CI, never run)
6. - [ ] **Release candidate** (Josh): tag `v0.2.0rc1`. PyPI marks it pre-release; Docker gets
         only `:0.2.0rc1` (no `:latest`). Check the PyPI page, `pip install --pre migratarr`,
         `docker pull ghcr.io/jwilcox99/migratarr:0.2.0rc1`, `--version`
7. - [ ] **Friend's install** (Josh): from `docs/docker.md` and `examples/docker/compose.yaml`
         only, with `:0.2.0rc1`. Fixes found here go into `main` before the final tag
8. - [ ] **Release** (Claude, then Josh): `__version__ = '0.2.0'`, the CHANGELOG heading gets
         the date; Josh tags `v0.2.0` (PyPI, GitHub release, Docker `:0.2.0`, `:0.2`, `:latest`)
9. - [ ] **After** (Josh, optional): delete merged branches (`feature/docker`,
         `feature/schedule-notify`, `feature/ui-login`, ...); announce
