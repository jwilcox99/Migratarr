"""Load, check, migrate and save Migratarr's three settings files together.

    runtime.json          host, topology, NAS, containers, service URLs, credential sources
    storage-targets.json  disks (the only place they are declared, from schema 2), category folders
    planner.json          streaming, override tags, scoring

`load()` never raises for what is *in* the files: it returns a Report listing
every problem by file and field, so the settings page, `doctor` and the wizard
can show all of them at once. `migrate()` turns a version 1 runtime.json into
version 2 (disks move out; storage-targets.json already has them), and
`save()` writes changed files; both back up what they replace, write
atomically and refuse while a UI job or an executor is running.

Nothing here reads a credential's value or contacts a service.
"""
from contextlib import ExitStack, contextmanager
import copy
from dataclasses import dataclass, field
from datetime import datetime, timezone
import difflib
import errno
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile

RUNTIME, TARGETS, PLANNER = 'runtime', 'storage_targets', 'planner'
FILES = (RUNTIME, TARGETS, PLANNER)
REPOSITORY = Path(__file__).resolve().parents[2]
BACKUP_FOLDER = 'backups'


class SettingsBusy(RuntimeError):
    """A UI job or an executor is running; settings can't change under it."""


class SettingsConflict(RuntimeError):
    """A file changed on disk since it was loaded (someone else saved first)."""


class SettingsInvalid(ValueError):
    def __init__(self, problems):
        self.problems = problems
        super().__init__('; '.join(p.text() for p in problems if p.severity == 'error'))


@dataclass(frozen=True)
class Problem:
    file: str            # runtime | storage_targets | planner
    field: str           # e.g. "urls.radarr", "targets[1] (media02)", "root"
    message: str
    severity: str = 'error'   # error | warning

    def text(self):
        return f'{self.file}: {self.field}: {self.message}'

    def as_dict(self):
        return dict(file=self.file, field=self.field, message=self.message, severity=self.severity)


@dataclass(frozen=True)
class SettingsFiles:
    runtime: Path
    storage_targets: Path
    planner: Path

    def path(self, name):
        return getattr(self, name)

    def as_dict(self):
        return {name: str(self.path(name)) for name in FILES}


@dataclass
class Report:
    files: SettingsFiles
    data: dict                       # name -> parsed JSON, or None if missing/unreadable
    digests: dict                    # name -> sha256 of the file bytes, or None if missing
    problems: list = field(default_factory=list)
    runtime: object = None           # RuntimeConfig when runtime.json is valid
    targets: object = None           # StorageTargets when storage-targets.json is valid
    planner: object = None           # PlannerSettings when planner.json is valid

    @property
    def ok(self):
        return not self.errors

    @property
    def errors(self):
        return [p for p in self.problems if p.severity == 'error']

    @property
    def missing(self):
        return [name for name in FILES if self.digests.get(name) is None]

    @property
    def needs_migration(self):
        data = self.data.get(RUNTIME)
        return isinstance(data, dict) and data.get('schema_version') == 1

    @property
    def topology(self):
        data = self.data.get(RUNTIME)
        if isinstance(data, dict):
            return data.get('topology', 'nas_ssh' if data.get('schema_version') == 1 else None)
        return None

    def as_dict(self):
        """JSON-safe summary for the UI and `doctor --json`; contains no credential values."""
        return dict(ok=self.ok, files=self.files.as_dict(), missing=self.missing,
                    needs_migration=self.needs_migration, topology=self.topology,
                    problems=[p.as_dict() for p in self.problems])


# ---------------------------------------------------------------- locating

def locate(environ=None, runtime_file=None):
    """The three files, by the same rules every script uses to load them."""
    from runtime_config import read_json, runtime_path, targets_path, ConfigError
    env = os.environ if environ is None else environ
    runtime = Path(runtime_file).expanduser() if runtime_file else runtime_path(env)
    planner = Path(env.get('MIGRATARR_PLANNER_CONFIG')
                   or REPOSITORY / 'config' / 'planner.json').expanduser()
    try:
        data = read_json(runtime)
    except ConfigError:
        data = None
    if isinstance(data, dict) and data.get('schema_version') == 1 and isinstance(data.get('base_path'), str):
        targets = targets_path(data, runtime, env)
    elif isinstance(data, dict) and data.get('schema_version') == 2 and (
            data.get('storage_targets') is None or isinstance(data.get('storage_targets'), str)):
        targets = targets_path(data, runtime, env)
    elif env.get('MIGRATARR_STORAGE_TARGETS'):
        targets = Path(env['MIGRATARR_STORAGE_TARGETS']).expanduser()
    else:
        targets = runtime.parent / 'storage-targets.json'
    return SettingsFiles(runtime, targets, planner)


# ---------------------------------------------------------------- loading and checking

def _read(path):
    """(data, sha256, problem message). data is None when missing or unparsable."""
    from runtime_config import unique_object, ConfigError
    try:
        raw = Path(path).read_bytes()
    except FileNotFoundError:
        return None, None, 'file not found: ' + str(path)
    except OSError as exc:
        return None, None, 'cannot read ' + str(path) + ': ' + (exc.strerror or type(exc).__name__)
    digest = hashlib.sha256(raw).hexdigest()
    try:
        return json.loads(raw.decode('utf-8'), object_pairs_hook=unique_object), digest, None
    except (ValueError, ConfigError) as exc:
        return None, digest, 'not valid JSON: ' + str(exc).replace('Runtime configuration: ', '')


def _strip(message, *prefixes):
    for prefix in prefixes:
        if message.startswith(prefix):
            message = message[len(prefix):]
    return message


def check(data, environ=None):
    """Problems in a set of settings documents {name: data}, without touching disk.

    Used by load() on what is on disk and by save() on what would be written,
    so a save is checked exactly the way the next load will be.
    Returns (problems, runtime, targets, planner).
    """
    from runtime_config import RuntimeConfig, ConfigError
    from storage_targets import parse_targets, target_problems, check_runtime_consistency
    from planner_settings import parse_settings, settings_problems, SettingsError
    problems, runtime, targets, planner = [], None, None, None

    raw_targets = data.get(TARGETS)
    if raw_targets is not None:
        for where, message in target_problems(raw_targets):
            problems.append(Problem(TARGETS, where, _strip(message, 'Storage targets: ')))
        if not any(p.file == TARGETS for p in problems):
            try:
                targets = parse_targets(raw_targets)
            except ValueError as exc:  # target_problems should have caught it; never hide it
                problems.append(Problem(TARGETS, 'root', _strip(str(exc), 'Storage targets: ')))

    raw_runtime = data.get(RUNTIME)
    if raw_runtime is not None:
        version2 = isinstance(raw_runtime, dict) and raw_runtime.get('schema_version') == 2
        found = RuntimeConfig.problems(raw_runtime, targets if version2 else None)
        for where, message in found:
            problems.append(Problem(RUNTIME, where, _strip(message, 'Runtime configuration: ')))
        if not found and (targets is not None or not version2):
            try:
                runtime = RuntimeConfig(raw_runtime, targets if version2 else None)
            except ConfigError as exc:
                problems.append(Problem(RUNTIME, 'root', _strip(str(exc), 'Runtime configuration: ')))
        if runtime is not None and runtime.schema_version == 1:
            problems.append(Problem(RUNTIME, 'schema_version',
                                    'version 1: disks are declared twice (runtime.json storage and '
                                    'storage-targets.json); run the settings migration to move to version 2',
                                    'warning'))
            if targets is not None:
                try:
                    check_runtime_consistency(runtime, targets)
                except ValueError as exc:
                    problems.append(Problem(RUNTIME, 'storage', _strip(str(exc), 'Storage targets: ')))

    raw_planner = data.get(PLANNER)
    if raw_planner is not None:
        for where, message in settings_problems(raw_planner):
            problems.append(Problem(PLANNER, where, _strip(message, 'Planner settings: ')))
        if not any(p.file == PLANNER for p in problems):
            try:
                planner = parse_settings(raw_planner)
            except SettingsError as exc:
                problems.append(Problem(PLANNER, 'root', _strip(str(exc), 'Planner settings: ')))
    return problems, runtime, targets, planner


def load(files=None, environ=None):
    """Read and check all three files. Never raises for their content."""
    files = files or locate(environ)
    data, digests, problems = {}, {}, []
    for name in FILES:
        value, digest, problem = _read(files.path(name))
        data[name], digests[name] = value, digest
        if problem:
            problems.append(Problem(name, 'file', problem))
    found, runtime, targets, planner = check(data, environ)
    if data[RUNTIME] is not None and data[RUNTIME].get('schema_version') == 2 and data[TARGETS] is None:
        found.append(Problem(RUNTIME, 'storage', 'runtime.json version 2 reads its disks from '
                             + str(files.storage_targets) + ', which could not be loaded'))
    report = Report(files, data, digests, problems + found, runtime, targets, planner)
    if runtime is not None:
        runtime.source_path = files.runtime.resolve()
    return report


# ---------------------------------------------------------------- busy checks and locking

def _flock_free(path):
    import fcntl
    with open(path, 'a') as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return True


def busy(base):
    """Why settings can't change right now, or None.

    A UI job holds <base>/jobs/runner.lock for its whole life; each executor
    holds <base>/execution_logs/executor.lock for its whole run (including
    batch_cross_*.py rows started from a terminal).
    """
    base = Path(base)
    checks = (('jobs/runner.lock', 'a UI job is running'),
              ('execution_logs/executor.lock', 'an executor is running'))
    for relative, reason in checks:
        lock = base / relative
        if lock.exists() and not _flock_free(lock):
            return reason
    return None


@contextmanager
def hold_locks(base):
    """Hold both locks while settings change, so no job or executor starts mid-write."""
    import fcntl
    base = Path(base)
    with ExitStack() as stack:
        for relative, reason in (('jobs/runner.lock', 'a UI job is running'),
                                 ('execution_logs/executor.lock', 'an executor is running')):
            lock = base / relative
            if not lock.parent.is_dir():
                continue  # nothing has ever run here; nothing can be running
            handle = stack.enter_context(open(lock, 'a'))
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise SettingsBusy('Settings were not changed: ' + reason + '. Try again when it finishes.') \
                    from None
        yield


# ---------------------------------------------------------------- writing

def render(data):
    return json.dumps(data, indent=2, ensure_ascii=False) + '\n'


def _fsync_dir(folder):
    with __import__('contextlib').suppress(OSError):
        fd = os.open(folder, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def write_atomic(path, text, mode=None):
    """Replace `path` with `text` in one rename; keeps the old file's mode."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if mode is None:
        try:
            mode = path.stat().st_mode & 0o7777
        except FileNotFoundError:
            mode = 0o644
    fd, tmp = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        with __import__('contextlib').suppress(OSError):
            os.unlink(tmp)
        raise
    _fsync_dir(path.parent)


def backup_folder(files, now=None):
    stamp = (now or datetime.now(timezone.utc)).strftime('%Y%m%dT%H%M%S%fZ')
    return files.runtime.parent / BACKUP_FOLDER / stamp


def _backup(paths, folder):
    folder.mkdir(parents=True, exist_ok=False)
    os.chmod(folder, 0o700)
    saved = []
    for path in paths:
        if path.exists():
            # The name says which file it was; two files with one name can't collide
            # because the three settings files have distinct names.
            shutil.copy2(path, folder / path.name)
            saved.append(str(folder / path.name))
    _fsync_dir(folder)
    return saved


def diff(report, changes):
    """Unified diff of each changed file: {name: text}. Shows settings only, never credential values."""
    result = {}
    for name, new in changes.items():
        old = report.data.get(name)
        before = render(old).splitlines(keepends=True) if old is not None else []
        after = render(new).splitlines(keepends=True)
        if before != after:
            path = str(report.files.path(name))
            result[name] = ''.join(difflib.unified_diff(before, after, path + ' (current)', path + ' (new)'))
    return result


@dataclass
class SaveResult:
    written: list
    backup: str | None
    backed_up: list
    diff: dict

    def as_dict(self):
        return dict(written=self.written, backup=self.backup, backed_up=self.backed_up, diff=self.diff)


def save(changes, *, report=None, base=None, environ=None, dry_run=False, now=None):
    """Write the changed settings documents {name: data}, all or nothing.

    - The combined result (changes plus the files left alone) must have no errors.
    - Each file must still be what `report` loaded (no lost updates).
    - Refused while a UI job or executor runs; both locks are held while writing.
    - Every file about to be replaced is first copied to config/backups/<UTC time>/.
    """
    report = report or load(environ=environ)
    unknown = set(changes) - set(FILES)
    if unknown:
        raise ValueError('unknown settings files: ' + ', '.join(sorted(unknown)))
    combined = {name: copy.deepcopy(changes.get(name, report.data.get(name))) for name in FILES}
    problems, runtime, _, _ = check(combined, environ)
    errors = [p for p in problems if p.severity == 'error']
    if errors:
        raise SettingsInvalid(errors)
    changed = diff(report, changes)
    if dry_run or not changed:
        return SaveResult([], None, [], changed)
    if base is None:
        if runtime is None:
            raise ValueError('save() needs base= when runtime.json is not part of the settings')
        base = runtime.base_path
    base = Path(base)
    with hold_locks(base):
        for name in changed:
            _, digest, _ = _read(report.files.path(name))
            if digest != report.digests.get(name):
                raise SettingsConflict(f'{report.files.path(name)} changed since it was loaded; reload and retry')
        paths = [report.files.path(name) for name in changed]
        folder = backup_folder(report.files, now)
        backed_up = _backup(paths, folder)
        written = []
        # Storage targets first: a version 2 runtime.json is only valid once its disks exist.
        for name in sorted(changed, key=lambda n: FILES.index(n) if n != TARGETS else -1):
            write_atomic(report.files.path(name), render(combined[name]))
            written.append(str(report.files.path(name)))
    return SaveResult(written, str(folder), backed_up, changed)


# ---------------------------------------------------------------- version 1 -> 2

def migrated_runtime(report):
    """The version 2 runtime.json for a valid version 1 report, or raise SettingsInvalid.

    Version 1 already has to agree with storage-targets.json (both roots of
    every disk), so the migration only removes `storage`, adds
    `topology: nas_ssh` (the only topology version 1 supported), and records
    where storage-targets.json is when it isn't beside runtime.json.
    """
    data = report.data.get(RUNTIME)
    if not (isinstance(data, dict) and data.get('schema_version') == 1):
        raise ValueError('runtime.json is not version 1; nothing to migrate')
    blocking = [p for p in report.errors if p.file in (RUNTIME, TARGETS)]
    if blocking:
        raise SettingsInvalid(blocking)
    if report.targets is None:
        raise SettingsInvalid([Problem(TARGETS, 'file', 'storage-targets.json is required to migrate: '
                                       'it becomes the only place disks are declared')])
    # The disks storage-targets.json would give, in runtime.json's order.
    by_id = {t.id: t for t in report.targets.targets}
    extra = sorted(set(by_id) - set(data['storage']))
    if extra:
        raise SettingsInvalid([Problem(TARGETS, 'targets', 'declares disks runtime.json does not ('
                                       + ', '.join(extra) + '); after migration they would become '
                                       'move sources and destinations. Remove them or add them to '
                                       'runtime.json storage first, then migrate')])
    new = {'schema_version': 2, 'base_path': data['base_path'], 'topology': 'nas_ssh'}
    for key in ('containers', 'urls', 'nas'):
        new[key] = copy.deepcopy(data[key])
    beside = report.files.runtime.expanduser().resolve().parent / 'storage-targets.json'
    actual = report.files.storage_targets.expanduser().resolve()
    if actual != beside:
        new['storage_targets'] = str(actual)
    for key in ('secrets', 'arr_file_checks'):
        if key in data:
            new[key] = copy.deepcopy(data[key])
    return new


def migrate(*, report=None, environ=None, dry_run=False, now=None):
    """Migrate a version 1 runtime.json to version 2. Returns SaveResult, or None if already version 2.

    Effective settings are unchanged, which is checked before writing: the
    version 2 file must load to the same storage, NAS, URLs, containers,
    credential sources and file checks as the version 1 file.
    """
    report = report or load(environ=environ)
    if not report.needs_migration:
        return None
    new = migrated_runtime(report)
    problems, runtime, _, _ = check({RUNTIME: new, TARGETS: report.data[TARGETS], PLANNER: None}, environ)
    if [p for p in problems if p.severity == 'error'] or runtime is None:
        raise SettingsInvalid([p for p in problems if p.severity == 'error'])
    old, now_ = report.runtime.as_dict(), runtime.as_dict()
    for record in (old, now_):
        for key in ('schema_version', 'topology', 'storage_targets'):
            record.pop(key, None)
    if old != now_:
        raise SettingsInvalid([Problem(RUNTIME, 'root', 'migration would change effective settings; '
                                       'not migrated')])
    return save({RUNTIME: new}, report=report, environ=environ, dry_run=dry_run, now=now)


def migrate_if_needed(*, environ=None, log=print):
    """For entry points (UI start, doctor): migrate once, quietly skip when not possible.

    Returns a short message for the operator, or None when nothing happened.
    Never raises: a migration that can't run leaves version 1 in place, which
    every script still reads.
    """
    try:
        report = load(environ=environ)
        if not report.needs_migration:
            return None
        result = migrate(report=report, environ=environ)
    except SettingsBusy as exc:
        message = 'runtime.json is still version 1; migration postponed: ' + str(exc)
    except (SettingsInvalid, SettingsConflict, ValueError, OSError) as exc:
        message = 'runtime.json is still version 1; not migrated: ' + str(exc)
    else:
        message = ('Migrated runtime.json to version 2 (disks now come only from storage-targets.json); '
                   'previous file saved in ' + str(result.backup))
    if log:
        log(message)
    return message


# ---------------------------------------------------------------- views

def redacted_secrets(runtime):
    """Where each credential comes from, for display. Never the value."""
    from service_keys import describe
    return {service: describe(service, source) for service, source in runtime.secrets.items()}
