"""Settings layer: one place that loads, checks, migrates and saves Migratarr's settings.

See docs/settings.md. Public API (used by migratarr_doctor.py, the UI, and the
settings forms and first-run wizard):

    locate(), load() -> Report (.problems, .ok, .runtime, .targets, .planner, .as_dict())
    check({name: data}) -> (problems, runtime, targets, planner)
    diff(report, changes), save(changes, report=...), busy(base)
    migrate(), migrate_if_needed()
    store_key(service, value) -> runtime.json "secrets" entry   (migratarr.settings.keys)
"""
from .store import (FILES, PLANNER, RUNTIME, TARGETS, Problem, Report, SaveResult, SettingsBusy,
                    SettingsConflict, SettingsFiles, SettingsInvalid, busy, check, diff, load, locate,
                    migrate, migrate_if_needed, redacted_secrets, save)
from .keys import KeyStoreError, store_key

__all__ = ['FILES', 'PLANNER', 'RUNTIME', 'TARGETS', 'Problem', 'Report', 'SaveResult', 'SettingsBusy',
           'SettingsConflict', 'SettingsFiles', 'SettingsInvalid', 'busy', 'check', 'diff', 'load',
           'locate', 'migrate', 'migrate_if_needed', 'redacted_secrets', 'save', 'KeyStoreError',
           'store_key']
