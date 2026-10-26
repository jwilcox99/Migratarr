# Settings layer

**Status:** Phase 1 of `reviews/2026-10-26-public-setup-plan`. Written 2026-10-26 against `main` @ `696d7d4`.

Migratarr is configured by three JSON files. This doc covers what changed in
runtime.json version 2, the settings layer every new entry point uses to read
and write them, and the service registry. Field-by-field references stay in
[Runtime configuration](runtime-configuration.md), [Storage targets](storage-targets.md)
and [Planner settings](planner-settings.md).

| File | Holds | Located by |
| --- | --- | --- |
| `runtime.json` | base path, **topology**, NAS (SSH), containers, service URLs, where credentials come from, Arr file checks | `$MIGRATARR_CONFIG`, else `config/runtime.json` beside `runtime_config.py` |
| `storage-targets.json` | **disks** (the only place they are declared from version 2), category folders, Arr root, reserves, placement | `$MIGRATARR_STORAGE_TARGETS`; else version 2: `storage_targets` in runtime.json (relative to its folder), default `storage-targets.json` beside it; version 1: `<base_path>/config/storage-targets.json` |
| `planner.json` | streaming, override tags, scoring | `$MIGRATARR_PLANNER_CONFIG`, else `config/planner.json` |

## runtime.json version 2

Version 1 declared every disk twice, in runtime.json `storage` and in
storage-targets.json `targets`, and refused to run when they disagreed.
Version 2 declares disks once, in storage-targets.json:

```json
{
  "schema_version": 2,
  "base_path": "/opt/media-stack/migratarr",
  "topology": "nas_ssh",
  "containers": {"radarr": "radarr", "sonarr": "sonarr", "homepage": "homepage"},
  "urls": {"radarr": "http://localhost:7878", "sonarr": "http://localhost:8989", "jellyfin": "http://localhost:8096"},
  "nas": {"host": "nas.example", "user": "migratarr", "ssh_key": "~/.ssh/migratarr_nas", "python": "/usr/bin/python3"}
}
```

- `storage` is gone (refused if present).
- `topology` is required:
  - `nas_ssh`: moves run on a NAS over SSH, exactly as version 1 always did. `nas` is required, and every storage target needs `remote_path` (its root as the NAS sees it).
  - `local`: moves run on this host's own disks. `nas` is not allowed, and no storage target may have `remote_path`. Execution in this mode arrives with Phase 3 (local transport); until then the executors stop at their NAS step, before changing anything.
- `storage_targets` (optional): path to storage-targets.json, absolute or relative to runtime.json's folder. Default: `storage-targets.json` beside runtime.json.
- `secrets`, `arr_file_checks`, `containers`, `urls`, `nas` are unchanged.

Scripts don't need to know which version a deployment uses: `runtime_config.load_config()` returns the same `storage` mapping (`{disk: {local_path, remote_path}}`) for both. For version 2 it is built from storage-targets.json; under `local`, `remote_path` equals `local_path`. `RuntimeConfig.as_dict()` for version 2 includes that derived `storage`, so the "did the config change during a job" checks in `migratarr_jobs.py` and `batch_cross_*.py` still notice a disk change.

Examples: `config/runtime.example.json` (version 2, NAS), `config/examples/runtime.local.example.json` + `storage-targets.local.example.json` (version 2, local disks), `config/examples/runtime.v1.example.json` (version 1, kept for the parity tests and as the migration reference).

## Migration from version 1

`migratarr.settings.migrate()` rewrites a version 1 runtime.json as version 2. It runs automatically, once, when the web UI starts (`migrate_if_needed`), and on request with `python3 migratarr_doctor.py --migrate`. It:

1. Requires both files to be valid and to agree (the existing `check_runtime_consistency`), and storage-targets.json to declare no disk that runtime.json doesn't. An extra target would silently become a move source and destination after migration, so it is refused with the disk named.
2. Removes `storage`, adds `topology: nas_ssh`, and adds `storage_targets` only when storage-targets.json is not beside runtime.json.
3. Loads the result and checks the effective settings are identical to version 1 (disks, NAS, URLs, containers, credential sources, file checks). If not, nothing is written.
4. Saves through `save()` below: backup first, atomic write, refused while a job or executor runs.

If it can't run (busy, invalid, inconsistent), version 1 stays in place and every script keeps reading it; the UI prints why on startup. To undo, copy `config/backups/<time>/runtime.json` back.

## The settings layer (`migratarr/settings/`)

For the UI, `doctor`, the settings forms (Phase 4) and the wizard (Phase 5). Scripts still use `runtime_config`, `storage_targets` and `planner_settings` directly.

| Function | Does |
| --- | --- |
| `locate()` | The three file paths, by the rules above. |
| `load()` → `Report` | Reads and checks all three. Never raises for content. `report.problems` is a list of `Problem(file, field, message, severity)`; `field` is as specific as the validators allow (`urls.radarr`, `targets[1] (media02)`, `streaming.region`). `report.runtime`, `.targets`, `.planner` are the parsed objects when valid. `report.as_dict()` is JSON-safe. |
| `check({name: data})` | The same checks on documents in memory (what a form is about to save). |
| `diff(report, changes)` | Unified diff per changed file. |
| `save(changes, report=...)` | Writes changed files all-or-nothing: the combined result must have no errors; each file must be unchanged since `report` loaded it (`SettingsConflict` otherwise); refused with `SettingsBusy` while `jobs/runner.lock` or `execution_logs/executor.lock` is held, and holds both while writing so nothing starts mid-write; copies each file it replaces to `config/backups/<UTC time>/` (folder `0700`); writes via temp file + `fsync` + rename, keeping the file's mode. |
| `busy(base)` | Why settings can't change now, or `None`. |
| `migrate()`, `migrate_if_needed()` | Above. |
| `redacted_secrets(runtime)` | Where each credential comes from, for display (`tmdb credential (environment variable TMDB_TOKEN)`). |
| `store_key(service, value)` | `migratarr/settings/keys.py`: below. |

Strict loaders and the reporter share one set of rules. `RuntimeConfig`, `parse_targets` and `parse_settings` are split into section validators; the strict path runs them in order and stops at the first error (messages unchanged), and `RuntimeConfig.problems`, `storage_targets.target_problems` and `planner_settings.settings_problems` run each one separately. Tests check that a mutation makes the strict loader fail exactly when the reporter reports it. Follow-on problems are suppressed (a bad `containers` hides the credential-source errors it causes; placement naming a broken target is not reported twice).

## Pasted API keys

Decision 2026-10-26: keys typed into onboarding or settings go to an owner-only file outside git and are read back through the existing `file` credential source. `store_key(service, value)` writes `~/.config/migratarr/secrets/<service>` (folder `0700`, file `0600`; `$MIGRATARR_SECRETS_DIR` overrides the folder), refuses a folder inside the repository and anything that isn't one line of printable text, and returns the `secrets` entry to save in runtime.json, e.g. `{"source": "file", "path": "/home/me/.config/migratarr/secrets/radarr"}`. It never returns, logs or echoes the value, including in errors. `service_keys._owner_only_file` already refuses group- or world-readable files when reading. Auto-detecting Radarr/Sonarr keys from `config.xml` stays the default.

## Service registry (`migratarr/services.py`)

One `ServiceDescriptor` per service: id, label, role (`movies`, `tv`, `media_server`, `metadata`), which runtime.json field holds its URL and default URL, which container its Docker credential source uses, its default credential source, which sources it accepts, whether Radarr/Sonarr-style `UrlBase` applies, and a `HealthProbe` (one authenticated GET, used by `doctor`). `service_keys.py` now takes `SERVICES`, `ARR`, `DEFAULT_SOURCES` and `DEFAULT_CONTAINER` from it; `tests/test_settings.py` pins them to the values hard-coded before, and checks each descriptor's accepted sources against `parse_secrets`.

A future integration is a new descriptor here plus an adapter for what Migratarr does with it. Nothing else needs to list services.

## Not in Phase 1

- Editing forms (Phase 4) and the wizard (Phase 5) build on `check`, `diff`, `save` and `store_key`.
- Executing moves under `topology: local` (Phase 3).
- `doctor` and the Settings & health page (Phase 2).

## Sources

`runtime_config.py`, `storage_targets.py`, `planner_settings.py`, `service_keys.py`, `media_layout.py`, `migratarr_jobs.py`, `config/*.example.json` at `main` @ `696d7d4`; `reviews/2026-10-26-public-setup-plan.md` (decisions table).
