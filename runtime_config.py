"""Validated, dependency-free host configuration; never contacts services."""
import json
import os
from functools import lru_cache
from pathlib import Path, PurePosixPath
import re
from urllib.parse import urlsplit


class ConfigError(ValueError):
    pass


def require(ok, message):
    if not ok:
        raise ConfigError('Runtime configuration: ' + message)


def fields(value, expected, label, optional=()):
    require(isinstance(value, dict), label + ' must be an object')
    require(set(expected) <= set(value) <= set(expected) | set(optional),
            label + ' requires exactly: ' + ', '.join(expected)
            + (' (optional: ' + ', '.join(optional) + ')' if optional else ''))


def path(value, label):
    require(isinstance(value, str) and bool(value) and not any(ord(c) < 32 for c in value),
            label + ' must be a nonempty path')
    value = os.path.expanduser(value)
    p = PurePosixPath(value)
    require(p.is_absolute() and str(p) == value and '..' not in p.parts and value != '/' and '\\' not in value,
            label + ' must be a canonical absolute POSIX path')
    return value

def check_url(name, value):
    require(isinstance(value, str), 'invalid URL: ' + name)
    try:
        u = urlsplit(value)
        port = u.port
        # An optional base path (e.g. /radarr behind a reverse proxy) must be canonical.
        segments = u.path.split('/')[1:] if u.path else []
        valid = (u.scheme in {'http', 'https'} and u.hostname and not u.username
                 and not u.password and not u.query and not u.fragment
                 and (not u.path or (u.path.startswith('/') and all(
                     s not in {'', '.', '..'} for s in segments)))
                 and not any(c.isspace() for c in value) and not value.endswith('/'))
    except ValueError:
        valid = False
    require(valid, 'URL must be HTTP(S) with an optional base path, no credentials, query, '
            'fragment or trailing slash: ' + name)


def storage_from_targets(targets, topology):
    """runtime.storage, derived from storage-targets.json (schema_version 2).

    nas_ssh: every target needs remote_path (its root as the NAS sees it).
    local: no target may have one; the "remote" root is the local root, because
    the program the executors run on the NAS runs on this host instead.
    """
    storage = {}
    for target in targets.targets:
        if topology == 'nas_ssh':
            require(target.remote_path is not None,
                    'storage target ' + target.id + ' needs remote_path (topology nas_ssh)')
            remote = str(target.remote_path)
        else:
            require(target.remote_path is None,
                    'storage target ' + target.id + ' has remote_path, which topology local does not use')
            remote = str(target.path)
        storage[target.id] = {'local_path': path(str(target.path), target.id + '.path'),
                              'remote_path': path(remote, target.id + '.remote_path')}
    require(storage, 'storage targets declare no disks')
    return storage


TOPOLOGIES = ('nas_ssh', 'local')
SCHEMA_VERSIONS = (1, 2)
DEFAULT_TARGETS_NAME = 'storage-targets.json'


class RuntimeConfig:
    """Validated runtime.json.

    Version 1 (every deployment before 2026-10) declares disks in a `storage`
    block that must agree with storage-targets.json. Version 2 drops `storage`:
    disks are declared once, in storage-targets.json, and passed in as
    `targets`; `topology` says how moves run (`nas_ssh`: a NAS over SSH, as
    version 1 always did; `local`: on this host's own disks, no NAS). Both
    expose the same `storage` mapping, so callers don't care which version a
    deployment uses. migratarr/settings migrates version 1 files to version 2.
    """

    # Each section validator sets attributes and raises ConfigError on the first
    # problem. The constructor runs them in order; migratarr.settings runs each
    # one separately to report every problem at once (see problems()).
    def __init__(self, data, targets=None):
        self._version(data)
        for _, check in self._sections(data, targets):
            check()

    def _sections(self, data, targets):
        """(field, validator) pairs, in the order the constructor runs them."""
        sections = [('base_path', lambda: self._base_path(data)),
                    ('containers', lambda: self._containers(data)),
                    ('urls', lambda: self._urls(data))]
        if self.schema_version == 1 or 'nas' in data or data.get('topology') == 'nas_ssh':
            sections.append(('nas', lambda: self._nas(data)))
        sections.append(('storage', lambda: self._storage(data, targets)))
        sections += [('secrets', lambda: self._secrets(data)),
                     ('arr_file_checks', lambda: self._arr_file_checks(data))]
        return sections

    def _version(self, data):
        require(isinstance(data, dict), 'root must be an object')
        version = data.get('schema_version')
        require(type(version) is int and version in SCHEMA_VERSIONS, 'unsupported schema_version')
        self.schema_version = version
        # Defaults so a reporter can run later sections after an earlier one failed.
        self.containers, self.urls, self.storage = {}, {}, {}
        self.topology, self.targets_path = 'nas_ssh', None
        self.ssh_target = self.ssh_key = self.remote_python = None
        if version == 1:
            fields(data, ('schema_version', 'base_path', 'containers', 'urls', 'nas', 'storage'), 'root',
                   optional=('secrets', 'arr_file_checks'))
            return
        fields(data, ('schema_version', 'base_path', 'topology', 'containers', 'urls'), 'root',
               optional=('nas', 'storage_targets', 'secrets', 'arr_file_checks'))
        require(data['topology'] in TOPOLOGIES, 'topology must be one of: ' + ', '.join(TOPOLOGIES))
        self.topology = data['topology']
        require((self.topology == 'nas_ssh') == ('nas' in data),
                'nas is required for topology nas_ssh and not allowed for topology local')
        if 'storage_targets' in data:
            value = data['storage_targets']
            # Absolute, or relative to runtime.json's folder; ~ expands.
            require(isinstance(value, str) and value.strip() == value and value
                    and not any(ord(c) < 32 for c in value),
                    'storage_targets must be a path to storage-targets.json')
        self.storage_targets_setting = data.get('storage_targets')

    def _base_path(self, data):
        self.base_path = Path(path(data['base_path'], 'base_path'))

    def _containers(self, data):
        # Each container is needed only by a source or check that runs `docker exec` in it:
        # service_keys.py (credentials) and arr_files.py (file checks) say which.
        fields(data['containers'], (), 'containers', optional=('radarr', 'sonarr', 'homepage'))
        for name, value in data['containers'].items():
            require(isinstance(value, str) and re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]*', value),
                    'invalid container: ' + name)
        self.containers = dict(data['containers'])

    def _urls(self, data):
        fields(data['urls'], ('radarr', 'sonarr', 'jellyfin'), 'urls')
        for name, value in data['urls'].items():
            check_url(name, value)
        self.urls = dict(data['urls'])

    def _nas(self, data):
        fields(data['nas'], ('host', 'user', 'ssh_key', 'python'), 'nas')
        nas = data['nas']
        for name in ('host', 'user'):
            require(isinstance(nas[name], str) and re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]*', nas[name]),
                    'invalid NAS ' + name)
        self.ssh_target = nas['user'] + '@' + nas['host']
        key = nas['ssh_key']
        require(isinstance(key, str) and bool(key) and not any(ord(c) < 32 for c in key), 'invalid nas.ssh_key')
        try:
            expanded = Path(key).expanduser()
        except RuntimeError as exc:
            raise ConfigError('Runtime configuration: cannot expand nas.ssh_key') from exc
        require((expanded.is_absolute() or PurePosixPath(str(expanded)).is_absolute())
                and '..' not in expanded.parts, 'nas.ssh_key must be an absolute path after expansion')
        self.ssh_key = str(expanded)
        self.remote_python = path(nas['python'], 'nas.python')

    def _storage(self, data, targets):
        if self.schema_version == 1:
            require(isinstance(data['storage'], dict) and data['storage'], 'storage must be a nonempty object')
            storage = {}
            for disk, entry in data['storage'].items():
                # Disk count and root depth are configuration; canonical absolute roots are not.
                require(re.fullmatch(r'[a-z][a-z0-9_-]{0,31}', disk) is not None,
                        'invalid storage disk id: ' + str(disk))
                fields(entry, ('local_path', 'remote_path'), 'storage.' + disk)
                storage[disk] = {k: path(v, disk + '.' + k) for k, v in entry.items()}
        else:
            require(targets is not None, 'schema_version 2 reads disks from storage-targets.json; '
                    'none was loaded')
            storage = storage_from_targets(targets, self.topology)
        self.storage = storage
        # Executors identify an item's disk by which root contains it (media_layout.py),
        # so no root may equal or contain another on either side.
        for side in ('local_path', 'remote_path'):
            roots = [PurePosixPath(v[side]) for v in self.storage.values()]
            require(not any(a == b or a in b.parents for i, a in enumerate(roots) for b in roots[i + 1:])
                    and not any(b in a.parents for i, a in enumerate(roots) for b in roots[i + 1:]),
                    side.replace('_path', '') + ' disk roots must be distinct and must not contain each other')
        parents = {str(PurePosixPath(v['local_path']).parent) for v in self.storage.values()}
        # Only the pinned pre-storage-targets planner baseline still reads this.
        self.mount_root = PurePosixPath(parents.pop()) if len(parents) == 1 else None

    def _secrets(self, data):
        from service_keys import SecretError, parse_secrets
        self.secrets_config = data.get('secrets', {})
        try:
            self.secrets = parse_secrets(self.secrets_config, self.containers)
        except SecretError as exc:
            raise ConfigError('Runtime configuration: ' + str(exc)) from None

    def _arr_file_checks(self, data):
        from arr_files import ArrFilesError, parse_arr_file_checks
        self.arr_file_checks_config = data.get('arr_file_checks', {})
        try:
            self.arr_file_checks = parse_arr_file_checks(self.arr_file_checks_config, self.containers)
        except ArrFilesError as exc:
            raise ConfigError('Runtime configuration: ' + str(exc)) from None

    @classmethod
    def problems(cls, data, targets=None):
        """Every section's problem as (field, message), instead of stopping at the first."""
        self = cls.__new__(cls)
        found = []
        try:
            self._version(data)
        except ConfigError as exc:
            found.append(('root', str(exc)))
            if not hasattr(self, 'schema_version'):
                return found
        for field, check in self._sections(data, targets):
            if field in ('secrets', 'arr_file_checks') and any(f == 'containers' for f, _ in found):
                continue  # they name containers; the containers problem is the one to fix
            if field == 'storage' and self.schema_version == 2 and targets is None:
                continue  # the storage-targets.json problem is reported with that file
            if field == 'urls' and isinstance(data.get('urls'), dict):
                for name, value in data['urls'].items():
                    try:
                        check_url(name, value)
                    except ConfigError as exc:
                        found.append(('urls.' + str(name), str(exc)))
            try:
                check()
            except ConfigError as exc:
                if field == 'urls' and any(f.startswith('urls.') for f, _ in found):
                    continue
                found.append((field, str(exc)))
            except (KeyError, TypeError, AttributeError):
                found.append((field, 'Runtime configuration: ' + field + ' is missing or malformed'))
        return found

    @property
    def remote_disks(self):
        return {k: v['remote_path'] for k, v in self.storage.items()}

    def local(self, disk):
        require(disk in self.storage, 'unknown disk reference: ' + str(disk))
        return Path(self.storage[disk]['local_path'])

    def as_dict(self):
        """Effective settings, for snapshots and "did the config change" checks.

        Version 2 includes the disks it read from storage-targets.json as
        `storage`, so a disk change is noticed the same way it was in version 1.
        That dict is a record, not a valid runtime.json version 2 file.
        """
        nas = {}
        if self.ssh_target is not None:
            user, host = self.ssh_target.split('@')
            nas = dict(nas=dict(host=host, user=user, ssh_key=self.ssh_key, python=self.remote_python))
        head = dict(schema_version=self.schema_version, base_path=self.base_path.as_posix())
        if self.schema_version == 2:
            head['topology'] = self.topology
        return dict(**head,
                    containers=dict(self.containers), urls=dict(self.urls), **nas,
                    storage={disk: dict(entry) for disk, entry in self.storage.items()},
                    **({'storage_targets': str(self.targets_path)} if self.targets_path else {}),
                    **({'secrets': dict(self.secrets_config)} if self.secrets_config else {}),
                    **({'arr_file_checks': dict(self.arr_file_checks_config)}
                       if self.arr_file_checks_config else {}))

def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, 'duplicate field: ' + key)
        result[key] = value
    return result


def runtime_path(environ=None):
    env = os.environ if environ is None else environ
    return Path(env.get('MIGRATARR_CONFIG') or Path(__file__).parent / 'config' / 'runtime.json').expanduser()


def targets_path(data, runtime_file, environ=None):
    """Where storage-targets.json is for this runtime.json (both versions).

    $MIGRATARR_STORAGE_TARGETS wins, as it always has. Otherwise version 1 uses
    <base_path>/config/storage-targets.json (unchanged) and version 2 uses its
    `storage_targets` field, relative to runtime.json's folder, defaulting to
    storage-targets.json beside runtime.json.
    """
    env = os.environ if environ is None else environ
    if env.get('MIGRATARR_STORAGE_TARGETS'):
        return Path(env['MIGRATARR_STORAGE_TARGETS']).expanduser()
    if data.get('schema_version') == 1:
        # Same as the scripts: base_path after the $MIGRATARR_BASE_PATH override.
        base = env.get('MIGRATARR_BASE_PATH') or data['base_path']
        return Path(os.path.expanduser(base)) / 'config' / DEFAULT_TARGETS_NAME
    folder = Path(runtime_file).expanduser().resolve().parent
    return folder / Path(data.get('storage_targets') or DEFAULT_TARGETS_NAME).expanduser()


def read_json(filename, label='Runtime configuration'):
    try:
        return json.loads(Path(filename).expanduser().read_text(encoding='utf-8'), object_pairs_hook=unique_object)
    except (OSError, ValueError) as exc:
        if isinstance(exc, ConfigError):
            raise
        raise ConfigError(label + ': cannot load ' + str(filename) + ': ' + str(exc)) from exc


def _load_targets(data, filename, env):
    if not (isinstance(data, dict) and data.get('schema_version') == 2):
        return None, None
    location = targets_path(data, filename, env)
    from storage_targets import load_targets
    try:
        return load_targets(location), location
    except (OSError, ValueError) as exc:
        raise ConfigError('Runtime configuration: cannot load storage targets ' + str(location)
                          + ': ' + str(exc)) from None


def load_config(filename=None, environ=None):
    env = os.environ if environ is None else environ
    filename = filename or runtime_path(env)
    data = read_json(filename)
    targets, location = _load_targets(data, filename, env)
    # Validate the file before applying overrides, so overrides cannot hide malformed settings.
    RuntimeConfig(data, targets)
    overrides = {'MIGRATARR_BASE_PATH': ('base_path',),
                 'MIGRATARR_NAS_HOST': ('nas', 'host'), 'MIGRATARR_NAS_USER': ('nas', 'user'),
                 'MIGRATARR_NAS_SSH_KEY': ('nas', 'ssh_key')}
    for service in ('radarr', 'sonarr', 'homepage'):
        overrides['MIGRATARR_' + service.upper() + '_CONTAINER'] = ('containers', service)
    for service in ('radarr', 'sonarr', 'jellyfin'):
        overrides['MIGRATARR_' + service.upper() + '_URL'] = ('urls', service)
    for key, location_ in overrides.items():
        if key in env:
            if location_[0] == 'nas' and 'nas' not in data:
                continue  # topology local has no NAS to override
            target = data
            for part in location_[:-1]:
                target = target[part]
            target[location_[-1]] = env[key]
    config = RuntimeConfig(data, targets)
    config.source_path = Path(filename).expanduser().resolve()
    config.targets_path = location.resolve() if location is not None else None
    return config

@lru_cache(maxsize=1)
def get_config():
    """Freeze effective settings for the lifetime of this process."""
    return load_config()
