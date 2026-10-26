# Settings UI contract (plan phases 1–2 ↔ 4–5)

Status: **proposed** by the phase 4–6 team, 2026-10-26, for the phase 1–3 team to accept or amend.
Plan: `reviews/2026-10-26-public-setup-plan.md`. Code: `settings_contract.py`.

Phases 4 (edit settings in the UI) and 5 (first-run wizard) depend on phase 1's
settings layer and phase 2's health checks. So both teams can work at once, the
pages are written against a small interface instead of the settings files. Phase 1
implements `SettingsBackend`, phase 2 implements `HealthBackend`; until then the
pages run against an in-memory fake (`tests/fake_settings.py`) that follows the same rules.

## What the pages need

| Call | Used by | Returns |
| --- | --- | --- |
| `status()` | both | `configured` (False → setup mode), `revision` (changes when any settings file changes), `job_running`, current `problems` |
| `capabilities()` | wizard | `topologies` (`nas_ssh` now; `local` once phase 3 ships the local transport), `secret_file` (shown to the user) |
| `sections()` | settings | `SectionSpec(id, title, summary)` in display order |
| `fields(section, values)` | both | `Field(path, label, kind, help, required, choices, group, read_only)`; may depend on values (one group per disk) |
| `current(section)` | settings | `{path: value}`; a stored secret is `SECRET_KEPT`, never its value |
| `defaults(section)` | wizard | starting values for a fresh install |
| `validate(section, values, draft=None)` | both | `[Problem(path, message, severity)]`; `fail` blocks saving, `warn` doesn't. `draft` lets a step check against earlier steps (disks against topology) |
| `diff(section, values)` | settings | `[Change(path, label, before, after, secret)]`; secrets use `SECRET_KEPT` / `SECRET_NEW` / `None` |
| `save(section, values, revision)` | settings | `SaveResult(files, backups)`; raises `SaveRefused` |
| `discover(section, draft)` | wizard | `Discovery(values, notes)`: suggestions the wizard puts only into empty fields |
| `create(draft)` | wizard | writes every file for a new install; raises `SaveRefused` if already configured |
| `run_checks(draft=None)` | both | `[CheckResult(id, label, status, detail, paths)]`; with `draft`, check the unsaved setup |

Section ids: `services`, `topology`, `disks`, `streaming`, `scoring`, `overrides`.
Field kinds: `text url path integer number choice multi bool lines secret`.
Paths are dotted and stable (`services.radarr.url`, `disks.media01.path`), so
problems and checks can point at the field they are about.

## Rules both sides keep

1. **Secrets stay in the backend.** `current`, `diff`, problems, checks and
   exceptions never contain a secret value. A blank secret field means "keep";
   the pages pass `SECRET_KEPT` through to `save`.
2. **The pages type, the backend judges.** The pages turn form text into `int`,
   `float`, `bool`, `str` or `list[str]` and report unparseable input themselves;
   everything about meaning (paths exist, URLs, weights, disk/category rules) is
   `validate`.
3. **`validate` and `diff` never write.** `save` and `create` re-validate, refuse
   while a job runs or when `revision` changed, back up every replaced file, and
   write atomically. Secrets go only to the owner-only secrets file.
4. **One change at a time.** The pages hold a reviewed change in memory under a
   one-time id that expires after 15 minutes. Saving re-checks `revision`, so a
   change reviewed against older files is refused, not merged.

## How the pages use it

- `GET /settings`: sections with problem counts, then the health checks. With
  `--read-only` this is phase 2's read-only "Settings & health" page: forms are
  shown disabled, nothing posts.
- `GET /settings/<section>` → `POST …/review` (diff) → `POST …/save`.
- Setup mode: when `configured` is False, every page redirects to `/setup` and
  every other action is refused. Steps: services → topology → disks →
  streaming → review. Nothing is written until review's Save calls `create`;
  then `/setup/done` shows the checks and offers a first plan only if no check fails.

## Requests to the phase 1–3 team

1. **Implement the interface in phase 1**, or tell us what to change, before the
   settings layer's API freezes. We will adjust the pages, not the other way round.
2. **One settings page, not two.** Phase 2's read-only page is `/settings` rendered by
   `settings_ui.py` with `run_checks()`. Phase 2 supplies the checks and
   `migratarr_doctor.py`; it does not need its own page.
3. **`run_checks(draft)`**: checks must be able to run against an unsaved setup
   (the wizard checks services and disks before anything is written).
4. **`discover`** for services (Radarr/Sonarr `config.xml`: key presence and
   `UrlBase`; never return the key) and disks (mounts, free space, Radarr/Sonarr
   root folders, a suggested category → folder mapping).
5. **A reload hook.** After `create`, the running UI needs the new base path, disks and
   reserves without a restart. Today `migratarr_ui.load_context` reads them once at
   start and fails without a config; phase 1 changes that loader anyway.
6. **Config location.** `runtime_config`, `planner_settings` and `migratarr_jobs`
   find `config/` next to the code. That is fine for a clone (`pip install -e .`)
   but not for a regular install; a single `MIGRATARR_HOME` (default: the clone)
   would fix it. Also `migratarr_plan` refuses to plan from a dirty git tree, and
   `snapshot_run` records the commit; an install without git needs a version instead.
   We need a decision before 0.1.0 (see `docs/release-checklist.md`).
7. **Topology choice.** `capabilities().topologies` gains `local` when the local
   transport passes its tests; the wizard refuses `local` until then.
