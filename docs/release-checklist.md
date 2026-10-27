# 0.1.0 release checklist (plan phase 6)

Plan: `reviews/2026-10-26-public-setup-plan.md`. Tag `v0.1.0` only when every box is ticked.
Owner key: **A** = phases 1–3 team, **B** = phases 4–6 team.

## Install and command

- [x] `pyproject.toml`, standard library only, Python ≥ 3.10 (B)
- [x] `migratarr` command: `ui`, `plan`, `status`, `approve`, `doctor` (B)
- [ ] `migratarr doctor` exists (A, phase 2); `tests/test_cli.py` then expects every command
- [x] Install scope (Josh, 2026-10-26): a regular `pip install migratarr`. Data folder
      `MIGRATARR_HOME`, default `~/.local/share/migratarr`; a clone keeps using itself (2026-10-27).
      Steps run as modules; snapshots record the version and a code hash when installed
- [x] CI builds the wheel and sdist, installs the wheel into a clean environment and runs it with an
      empty data folder (doctor, the wizard, nothing written into site-packages)
- [x] Release workflow (`.github/workflows/release.yml`): tag `vX.Y.Z` -> tests, build, PyPI via
      trusted publishing, GitHub release from the CHANGELOG section
- [ ] PyPI: add a trusted publisher for project `migratarr` (repository `jwilcox99/Migratarr`,
      workflow `release.yml`, environment `pypi`), and create the `pypi` environment on GitHub (Josh)
- [ ] Fresh-machine install test: clone, `pip install -e .`, `migratarr ui`, wizard, first plan (B)

## Settings and onboarding

- [x] Settings pages and wizard built against `settings_contract.py`, tested with the fake (B)
- [ ] Contract accepted or amended by A (`docs/settings-ui-contract.md`)
- [ ] Real backend wired into `migratarr_ui.main` (`settings=`, `wizard=`, reload hook) (A + B)
- [ ] Adding and removing a disk in Settings (rows the backend adds; B, after A's disk model)
- [ ] Wizard on a clean machine for both topologies; `local` only after phase 3's parity (A + B)

## Demo mode

- [ ] `migratarr demo`: plans `tests/synthetic_library.py`'s fake Radarr/Sonarr/Jellyfin/TMDB world
      into a temporary base and opens the UI **read-only**, so nothing can move. Needs the planners
      to accept an injected `urlopen` outside tests (today it is patched in), and the synthetic
      library moved out of `tests/` (B; review with A)

## Docs

- [x] `CHANGELOG.md`, issue templates (bug, feature, integration, private security link) (B)
- [ ] `SETUP.md` rewrite around the wizard; manual JSON as the advanced path (B, after A's v2 files)
- [ ] README: install, first run, both topologies, screenshots of setup and settings (B)
- [ ] `SECURITY.md`: it says pasted keys are never stored; update for the owner-only secrets
      file, and what the UI does and doesn't show (B, wording checked by A)
- [ ] `docs/runtime-configuration.md` and `docs/storage-targets.md` for settings v2 (A)

## Release

- [ ] CI green on 3.10–3.13, including a `pip install -e .` + `migratarr --help` step (B)
- [ ] Version `0.1.0` in `migratarr_cli.__version__`, CHANGELOG dated, tag `v0.1.0` (B)
- [ ] Repository public: history scrub plan done (`reviews/2026-09-24-history-scrub-plan.md`) (Josh)
