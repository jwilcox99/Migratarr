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


class RuntimeConfig:
    def __init__(self, data):
        fields(data, ('schema_version', 'base_path', 'containers', 'urls', 'nas', 'storage'), 'root',
               optional=('secrets', 'arr_file_checks'))
        require(type(data['schema_version']) is int and data['schema_version'] == 1, 'unsupported schema_version')
        self.base_path = Path(path(data['base_path'], 'base_path'))
        # Each container is needed only by a source or check that runs `docker exec` in it:
        # service_keys.py (credentials) and arr_files.py (file checks) say which.
        fields(data['containers'], (), 'containers', optional=('radarr', 'sonarr', 'homepage'))
        for name, value in data['containers'].items():
            require(isinstance(value, str) and re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]*', value),
                    'invalid container: ' + name)
        self.containers = dict(data['containers'])
        fields(data['urls'], ('radarr', 'sonarr', 'jellyfin'), 'urls')
        for name, value in data['urls'].items():
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
        self.urls = dict(data['urls'])
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
        require(isinstance(data['storage'], dict) and data['storage'], 'storage must be a nonempty object')
        self.storage = {}
        for disk, entry in data['storage'].items():
            # Disk count and root depth are configuration; canonical absolute roots are not.
            require(re.fullmatch(r'[a-z][a-z0-9_-]{0,31}', disk) is not None,
                    'invalid storage disk id: ' + str(disk))
            fields(entry, ('local_path', 'remote_path'), 'storage.' + disk)
            self.storage[disk] = {k: path(v, disk + '.' + k) for k, v in entry.items()}
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
        from service_keys import SecretError, parse_secrets
        self.secrets_config = data.get('secrets', {})
        try:
            self.secrets = parse_secrets(self.secrets_config, self.containers)
        except SecretError as exc:
            raise ConfigError('Runtime configuration: ' + str(exc)) from None
        from arr_files import ArrFilesError, parse_arr_file_checks
        self.arr_file_checks_config = data.get('arr_file_checks', {})
        try:
            self.arr_file_checks = parse_arr_file_checks(self.arr_file_checks_config, self.containers)
        except ArrFilesError as exc:
            raise ConfigError('Runtime configuration: ' + str(exc)) from None

    @property
    def remote_disks(self):
        return {k: v['remote_path'] for k, v in self.storage.items()}

    def local(self, disk):
        require(disk in self.storage, 'unknown disk reference: ' + str(disk))
        return Path(self.storage[disk]['local_path'])

    def as_dict(self):
        user, host = self.ssh_target.split('@')
        return dict(schema_version=1, base_path=self.base_path.as_posix(),
                    containers=dict(self.containers), urls=dict(self.urls),
                    nas=dict(host=host, user=user, ssh_key=self.ssh_key, python=self.remote_python),
                    storage={disk: dict(entry) for disk, entry in self.storage.items()},
                    **({'secrets': dict(self.secrets_config)} if self.secrets_config else {}),
                    **({'arr_file_checks': dict(self.arr_file_checks_config)}
                       if self.arr_file_checks_config else {}))


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, 'duplicate field: ' + key)
        result[key] = value
    return result


def load_config(filename=None, environ=None):
    env = os.environ if environ is None else environ
    filename = filename or env.get('MIGRATARR_CONFIG') or Path(__file__).parent / 'config' / 'runtime.json'
    try:
        data = json.loads(Path(filename).expanduser().read_text(encoding='utf-8'), object_pairs_hook=unique_object)
    except (OSError, ValueError) as exc:
        raise ConfigError('Runtime configuration: cannot load ' + str(filename) + ': ' + str(exc)) from exc
    # Validate the file before applying overrides, so overrides cannot hide malformed settings.
    RuntimeConfig(data)
    overrides = {'MIGRATARR_BASE_PATH': ('base_path',),
                 'MIGRATARR_NAS_HOST': ('nas', 'host'), 'MIGRATARR_NAS_USER': ('nas', 'user'),
                 'MIGRATARR_NAS_SSH_KEY': ('nas', 'ssh_key')}
    for service in ('radarr', 'sonarr', 'homepage'):
        overrides['MIGRATARR_' + service.upper() + '_CONTAINER'] = ('containers', service)
    for service in ('radarr', 'sonarr', 'jellyfin'):
        overrides['MIGRATARR_' + service.upper() + '_URL'] = ('urls', service)
    for key, location in overrides.items():
        if key in env:
            target = data
            for part in location[:-1]:
                target = target[part]
            target[location[-1]] = env[key]
    config = RuntimeConfig(data)
    config.source_path = Path(filename).expanduser().resolve()
    return config


@lru_cache(maxsize=1)
def get_config():
    """Freeze effective settings for the lifetime of this process."""
    return load_config()
