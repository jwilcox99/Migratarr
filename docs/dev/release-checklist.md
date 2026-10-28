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

1. - [x] **Package layout** (Claude): all code in the `migratarr` package; `python3 <script>.py`
         keeps working in a clone (PR #11)
2. - [x] **Docs** (Claude): README, `SETUP.md`, `SECURITY.md`, settings version 2 docs (PR #10);
         `migratarr demo` and the logo in the README, absolute links so the README also works
         on PyPI (release-prep branch). Still open: UI screenshots (Josh, from `migratarr demo`)
3. - [ ] **Real-world checks** (Josh, with steps from Claude):
         - Fresh machine or VM: install the built wheel, run the wizard, make a first plan
         - One real move on local disks with `examples/local-test/` (PR #13)
4. - [x] **Demo mode** (Claude): `migratarr demo` (PR #12)
5. - [x] **Version** (Claude): `0.1.0rc1` in `migratarr/cli.py`; CHANGELOG `[0.1.0]` section;
         release.yml handles pre-release tags (notes from `[0.1.0]`, marked pre-release) and
         stops the notes at the next heading
6. - [ ] **Public history** (Josh): the repo is already publicly readable with its full history.
         Decide whether the scrub plan (`reviews/2026-09-24-history-scrub-plan.md`, private)
         still applies, and do it before anything is published to PyPI
7. - [ ] **Repo settings** (Josh): `main` and `v*` tag rulesets, private vulnerability
         reporting, the `pypi` environment
8. - [ ] **PyPI** (Josh): trusted publisher for project `migratarr` — owner `jwilcox99`, repository
         `Migratarr`, workflow `release.yml`, environment `pypi`
9. - [ ] **Release candidate** (Josh): tag `v0.1.0rc1`; check the PyPI page, then
         `pipx install --pip-args=--pre migratarr` on the VM, `migratarr demo`, wizard, first plan
10. - [ ] **Release** (Claude, then Josh): `__version__ = '0.1.0'`, date the `[0.1.0]` section
          (`## [0.1.0] — YYYY-MM-DD`); Josh tags `v0.1.0`
