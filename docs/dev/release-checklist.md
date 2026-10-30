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
         on PyPI (release-prep branch); UI screenshots in the README (PR #7)
3. - [x] **Real-world checks** (Josh, 2026-10-28/29, Ubuntu 26.04, Python 3.14):
         - Fresh machine: install, wizard, plan gate
         - Local disks with `examples/local-test/`: all four move types, real Radarr and Sonarr,
           through the unified engine. Results: `examples/local-test/RESULTS-2026-10-29.md`
         - NAS (gate 4, 2026-10-29): new engine against the old in check-only mode, then one watched
           live move each for Movie same-disk, Movie cross-disk, TV cross-disk
           (`docs/dev/unified-execution-core.md`, "Recorded by gate 4", PR #12)
4. - [x] **Demo mode** (Claude): `migratarr demo` (PR #12)
5. - [x] **Version** (Claude): `0.1.0rc1` in `migratarr/cli.py`; CHANGELOG `[0.1.0]` section;
         release.yml handles pre-release tags (notes from `[0.1.0]`, marked pre-release) and
         stops the notes at the next heading
6. - [x] **Public history** (Josh, 2026-10-27/28): the scrub plan re-run on the final `main`
         (8 commits had the work email, one test line the NAS user); old repo archived and made
         private, a fresh `Migratarr` holds the scrubbed `main`. Clones re-pointed or disconnected
7. - [x] **Repo settings** (Josh, 2026-10-28): `main` ruleset (PR, the six CI checks, no force
         push or deletion), `v*` tag ruleset (no update or deletion), private vulnerability
         reporting, the `pypi` environment (required reviewer, `v*` tags only)
8. - [x] **PyPI** (Josh): pending trusted publisher for `migratarr` (owner `jwilcox99`, repository
         `Migratarr`, workflow `release.yml`, environment `pypi`)
9. - [x] **Release candidate** (Josh): `v0.1.0rc1` published 2026-10-28; PyPI page, `pip install
         --pre`, `--version`, `doctor`, `demo`, wizard and plan gate checked 2026-10-28.
         Pre-release cleanup (incident scripts out of the package, developer notes in
         `docs/dev/`) lands after rc1 and ships in 0.1.0
10. - [x] **Release** (Claude, then Josh): `__version__ = '0.1.0'`, `## [0.1.0] — 2026-10-30`
          (Unreleased folded in); Josh tags `v0.1.0`. Done before it: Jellyfin header fix (PR #8),
          kept keys and disks on one filesystem (PR #11), NAS check of the unified engine (gate 4,
          passed 2026-10-29, PR #12)
