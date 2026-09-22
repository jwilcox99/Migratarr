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


def fields(value, expected, label):
    require(isinstance(value, dict), label + ' must be an object')
    require(set(value) == set(expected), label + ' requires exactly: ' + ', '.join(expected))


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
        fields(data, ('schema_version', 'base_path', 'containers', 'urls', 'nas', 'storage'), 'root')
        require(type(data['schema_version']) is int and data['schema_version'] == 1, 'unsupported schema_version')
        self.base_path = Path(path(data['base_path'], 'base_path'))
        fields(data['containers'], ('radarr', 'sonarr', 'homepage'), 'containers')
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
                valid = (u.scheme in {'http', 'https'} and u.hostname and not u.username
                         and not u.password and not u.query and not u.fragment and not u.path
                         and not any(c.isspace() for c in value))
            except ValueError:
                valid = False
            require(valid, 'URL must be an HTTP(S) origin without credentials/path: ' + name)
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
            # Disk count is not a Phase One contract; shape and identity below still are.
            require(re.fullmatch(r'[a-z][a-z0-9_-]{0,31}', disk) is not None,
                    'invalid storage disk id: ' + str(disk))
            fields(entry, ('local_path', 'remote_path'), 'storage.' + disk)
            self.storage[disk] = {k: path(v, disk + '.' + k) for k, v in entry.items()}
            local = PurePosixPath(self.storage[disk]['local_path'])
            remote = PurePosixPath(self.storage[disk]['remote_path'])
            # Retain Phase One path shape and disk identity checks, including remote helpers.
            require(len(local.parts) == 4 and local.name == disk,
                    disk + ' local_path must be /<component>/<component>/' + disk)
            require(len(remote.parts) == 3, disk + ' remote_path must have two components')
        parents = {str(PurePosixPath(v['local_path']).parent) for v in self.storage.values()}
        require(len(parents) == 1, 'local disk roots must share a parent')
        require(len(set(self.remote_disks.values())) == len(self.remote_disks),
                'remote disk roots must be distinct')
        self.mount_root = PurePosixPath(parents.pop())

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
                    storage={disk: dict(entry) for disk, entry in self.storage.items()})


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
