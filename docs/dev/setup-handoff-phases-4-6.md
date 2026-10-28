# Hand-off: what Phases 1–3 give the Phase 4–6 team

**From:** Phases 1–3 team, 2026-10-26. **Base:** `integration/setup-phases-1-3` (= `main` @ `696d7d4` + `feature/engine-gate2` + the three branches below). Build Phase 4–6 work on that branch, or on `feature/doctor` if you don't need the engine changes.

| Branch | Phase | Contents |
| --- | --- | --- |
| `feature/settings-foundation` | 1 | runtime.json v2 + migration, settings layer, service registry, pasted keys |
| `feature/doctor` | 2 | `migratarr/health.py`, `migratarr_doctor.py`, `/settings` page, UI setup mode |
| `feature/engine-transport` | 3 (first step) | gate 2 merged, `NasChannel` (SSH or local), exit status on failures, mark reconciled |

## Stable interfaces for you

Please call these rather than reading or writing the JSON files yourselves; ask us before changing their signatures.

**Settings (`migratarr.settings`, docs/settings.md)**
- `load()` → `Report`: `.data[name]` (raw JSON per file, `None` if missing), `.problems` (`Problem(file, field, message, severity)`), `.runtime/.targets/.planner` (parsed or `None`), `.missing`, `.needs_migration`, `.topology`, `.as_dict()`.
- `check({name: data})` → `(problems, runtime, targets, planner)`: validate a form's result before showing a diff. `field` values match the JSON paths your forms edit (`urls.radarr`, `nas`, `targets[2] (media03)`, `category_paths.TV`, `streaming.region`, `scoring`).
- `diff(report, changes)` → `{name: unified diff}` for the confirm screen.
- `save(changes, report=report)`: all-or-nothing; raises `SettingsInvalid(problems)`, `SettingsConflict` (file changed since `report`), `SettingsBusy` (job/executor running). Returns `SaveResult(written, backup, backed_up, diff)`. For a first-run wizard with no runtime.json yet, pass `base=` and all three files in `changes`.
- `busy(base)` → reason or `None`: grey out Save buttons with it.
- `migratarr.settings.keys.store_key(service, value, url_base=None)` → the `secrets` entry to put in runtime.json. Never echo `value`; never put it in a page, log or error.
- `redacted_secrets(runtime)`: display strings for credential sources.

**Services (`migratarr.services`)**: `SERVICES[id]` → `ServiceDescriptor` (label, role, `url_setting`, `default_url`, `container_setting`, `default_source`, `sources`, `url_base_from_config`, `credential_label`, `help_url`, `probe`). Build service forms from this table; a new integration is a new descriptor.

**Health (`migratarr.health`)**: `run_checks(report, groups=(...))` → `[Check(id, group, label, status, detail, fix)]`; `worst(checks)`. Individual checks for the wizard's "Test" buttons: `service_check(descriptor, runtime)`, `arr_root_checks(...)`, `disk_checks(...)`, `nas_checks(runtime)`. To test settings that aren't saved yet, build them with `settings.check(...)` and pass the resulting objects.

**UI**: `/settings` and `/api/settings` (read-only view model in `settings_view()`); `App(..., setup=True)` is the mode the UI starts in when settings don't load. Phase 5's wizard can replace `render_settings(..., setup=True)` as the setup-mode landing page. Every POST needs the per-process token (`form()`), as today.

**Topology**: `runtime.topology` is `nas_ssh` or `local`. Under `local`, runtime.json has no `nas` and storage targets have no `remote_path`. Moves run under `local` (Phase 3 transport), but see the caveat below before offering it in the wizard.

## Things to know

- Version 1 runtime.json files still load everywhere; the UI migrates them once at startup. Your forms only ever need to write version 2.
- `config/runtime.example.json` is now version 2; the version 1 example moved to `config/examples/` (parity tests use it).
- Category identities (`Common`, `Current`, `Library`, `Rare`, `Archive`) stay fixed; only folders, placement and weights are editable (plan risk list).
- **Local topology is not yet evidenced end to end** against real Radarr/Sonarr (only the shipped programs are, in tests). Keep it behind an "experimental" label in the wizard until the plan's real-world test is done.
- Settings saves and reconciliation records hold `jobs/runner.lock` and `execution_logs/executor.lock` while writing; a job started at that instant gets "Another job is running".

## Open questions for Josh (affect both teams)

1. Arr root folder check: a placement category folder that isn't an Arr root folder is reported as **failed** (the executors refuse moves into it). OK, or should it warn?
2. When should the version 1 → 2 migration run on Josh's media host: let the UI do it on next start, or run `migratarr_doctor.py --migrate` by hand first?
3. Gate 3 (adapters/strategies/`run()`) is next for the engine. Proceed now, or after the gate 4 live parity check of the transport change on the NAS?
