# 0.1.0 release checklist

First public release: `pip install migratarr`, set up in the browser, every move approved first.
Tag `v0.1.0` only when every box is ticked. Owner: **Josh**, or **Claude** (work handed over as
branches to push).

## Decisions (Josh)

| Date | Decision |
| --- | --- |
| 2026-10-26 | A regular `pip install`, published on PyPI as `migratarr` (name confirmed free 2026-10-27) |
| 2026-10-26 | Setup and Settings may save with failing health checks; planning waits until none fail (warnings allowed) |
| 2026-10-27 | Data folder: `MIGRATARR_HOME`, default `~/.local/share/migratarr`; a git clone keeps using itself |
| 2026-10-27 | Moving files on this machine's own disks ships in 0.1.0, labelled experimental |
| 2026-10-27 | Demo mode is in 0.1.0, built last so it can't hold up the release |
| 2026-10-27 | Public repo at `github.com/jwilcox99/Migratarr`, by the scrub plan: the current repo becomes `Migratarr-archive` (private), a fresh repo gets the scrubbed `main` |

## Done

- [x] `pyproject.toml` (standard library only, Python ≥ 3.10); `migratarr` command: `ui`, `plan`,
      `status`, `approve`, `doctor`, `reconcile`
- [x] Settings layer, health checks, `migratarr doctor` (phases 1–2)
- [x] Settings pages and setup wizard on the real settings layer; add and remove disks; plan gate
      in the UI and `migratarr plan` (phases 4–5, PR #6)
- [x] Wizard tested on the media host, NAS and local topologies (2026-10-27); fixes in PR #7
- [x] Runs as an installed program: data folder, steps as modules, version and code hash in
      snapshots (phase 7, PR #8)
- [x] CI: tests on 3.10–3.13; wheel built, installed into a clean environment and run with an
      empty data folder
- [x] Release workflow: tag `vX.Y.Z` → tests, build, PyPI (trusted publishing), GitHub release
      from CHANGELOG
- [x] `CHANGELOG.md`, issue templates, private security reporting link

## To do, in order

1. - [ ] **Package layout** (Claude): move the top-level modules into the `migratarr` package so an
         install doesn't add ~50 generic module names (`read_api`, `storage_targets`, ...) to
         site-packages. Keep `python3 <script>.py` working in a clone for existing setups
2. - [ ] **Docs** (Claude):
         - README: what it is, `pip install migratarr`, first run, both topologies, screenshots
         - `SETUP.md`: the wizard first; hand-edited JSON as the advanced path
         - `SECURITY.md`: pasted keys go to an owner-only folder; what the UI shows and never shows
         - `docs/runtime-configuration.md`, `docs/storage-targets.md`: settings version 2
3. - [ ] **Real-world checks** (Josh, with steps from Claude):
         - Fresh machine or VM: install the built wheel, run the wizard, make a first plan
         - One real move on local disks against throwaway Radarr and Sonarr containers
4. - [ ] **Demo mode** (Claude): `migratarr demo` plans the synthetic library into a temporary
         folder and opens the UI read-only, so nothing can move. Needs the planners to take an
         injected HTTP opener outside tests, and the synthetic library moved out of `tests/`
5. - [ ] **Version** (Claude): `0.1.0` in `migratarr_cli.__version__`, CHANGELOG section dated
6. - [ ] **Go public** (Josh): rerun the scrub on the final `main`, then the scrub plan's remaining
         steps (`reviews/2026-09-24-history-scrub-plan.md`)
7. - [ ] **New repo settings** (Josh): `main` and `v*` tag rulesets, private vulnerability
         reporting, the `pypi` environment
8. - [ ] **PyPI** (Josh): trusted publisher for project `migratarr` — owner `jwilcox99`, repository
         `Migratarr`, workflow `release.yml`, environment `pypi`
9. - [ ] **Tag `v0.1.0`** (Josh) on the public repo; the release workflow publishes
