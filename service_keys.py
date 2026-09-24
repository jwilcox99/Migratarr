"""Where Migratarr reads service credentials from. Values are never logged.

runtime.json may name a source per service under an optional "secrets"
object; any service left out keeps the behavior every script had before this
module existed:

  radarr, sonarr  docker_config_xml: ApiKey from /config/config.xml in the
                  service's container (containers.radarr / containers.sonarr)
  jellyfin        docker_file: first nonempty of /run/secrets/jellyfin_api_key,
                  /run/secrets/jellyfin_key in containers.homepage
  tmdb            env: $TMDB_TOKEN

Other sources: config_xml (a host-side config.xml, for non-Docker installs),
file (a user-provisioned, owner-only file outside the repository) and env.
Errors name the source, never the value.
"""
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET

SERVICES = ('radarr', 'sonarr', 'jellyfin', 'tmdb')
ARR = ('radarr', 'sonarr')
REPOSITORY = Path(__file__).resolve().parent
DEFAULT_SOURCES = {
    'radarr': {'source': 'docker_config_xml'},
    'sonarr': {'source': 'docker_config_xml'},
    'jellyfin': {'source': 'docker_file',
                 'paths': ['/run/secrets/jellyfin_api_key', '/run/secrets/jellyfin_key']},
    'tmdb': {'source': 'env', 'variable': 'TMDB_TOKEN'},
}
# Which runtime.json container a docker source uses when it names none.
DEFAULT_CONTAINER = {'radarr': 'radarr', 'sonarr': 'sonarr', 'jellyfin': 'homepage'}
FIELDS = {  # source: (required, optional)
    'docker_config_xml': ((), ('container', 'path')),
    'docker_file': (('paths',), ('container',)),
    'config_xml': (('path',), ()),
    'file': (('path',), ('url_base',)),
    'env': (('variable',), ('url_base',)),
}


class SecretError(ValueError):
    pass


def _require(ok, message):
    if not ok:
        raise SecretError('Service credentials: ' + message)


def _absolute(value, label):
    _require(isinstance(value, str) and value and not any(ord(c) < 32 for c in value)
             and PurePosixPath(value).is_absolute() and str(PurePosixPath(value)) == value
             and '..' not in PurePosixPath(value).parts, label + ' must be a canonical absolute path')


def parse_secrets(data, containers):
    """Validate runtime.json "secrets" (no I/O); return the effective source per service."""
    _require(isinstance(data, dict), 'secrets must be an object')
    unknown = set(data) - set(SERVICES)
    _require(not unknown, 'unknown services: ' + ', '.join(sorted(unknown)))
    result = {}
    for service in SERVICES:
        source = dict(data.get(service, DEFAULT_SOURCES[service]))
        label = 'secrets.' + service
        kind = source.pop('source', None)
        _require(kind in FIELDS, label + '.source must be one of: ' + ', '.join(FIELDS))
        required, optional = FIELDS[kind]
        _require(set(required) <= set(source) <= set(required) | set(optional),
                 label + ' (' + kind + ') takes ' + ', '.join(required + optional or ('no fields',)))
        _require(service in ARR or kind not in {'docker_config_xml', 'config_xml'},
                 label + ': ' + kind + ' is only for radarr and sonarr')
        _require(service in ARR or 'url_base' not in source, label + ': url_base is only for radarr and sonarr')
        if kind.startswith('docker'):
            container = source.get('container') or containers.get(DEFAULT_CONTAINER.get(service, ''))
            _require(isinstance(container, str) and re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]*', container),
                     label + ' needs a container (set it here or in runtime.json containers)')
            source['container'] = container
        if kind == 'docker_config_xml':
            source.setdefault('path', '/config/config.xml')
        paths = source.get('paths', [source['path']] if 'path' in source else [])
        _require(kind == 'env' or (isinstance(paths, list) and paths), label + ' needs at least one path')
        for path in paths:
            if kind == 'file':  # a host path; ~ allowed
                _require(isinstance(path, str) and path and not any(ord(c) < 32 for c in path)
                         and os.path.isabs(os.path.expanduser(path)), label + '.path must be absolute')
            else:
                _absolute(path, label + ' path')
        if kind == 'env':
            _require(isinstance(source['variable'], str) and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', source['variable']),
                     label + '.variable must be an environment variable name')
        if 'url_base' in source:
            base = source['url_base']
            _require(base == '' or (isinstance(base, str) and base.startswith('/')
                                    and not any(c.isspace() or c in '?#' for c in base)),
                     label + '.url_base must be empty or a path like /radarr')
        result[service] = dict(source, source=kind)
    return result


def describe(service, source):
    kind = source['source']
    if kind.startswith('docker'):
        where = ', '.join(source.get('paths', [source.get('path', '')]))
        return f'{service} credential ({kind} {where} in container {source["container"]})'
    if kind == 'env':
        return f'{service} credential (environment variable {source["variable"]})'
    return f'{service} credential ({kind} {source["path"]})'


def _docker_cat(container, path):
    # No shell: container and path are validated and passed as separate arguments.
    return subprocess.check_output(['docker', 'exec', container, 'cat', path], timeout=30)


def _owner_only_file(raw):
    path = Path(raw).expanduser()
    _require(path.is_absolute(), f'{raw} must be absolute')
    resolved = path.resolve()
    _require(resolved != REPOSITORY and REPOSITORY not in resolved.parents,
             f'{raw} is inside the repository; keep credential files outside it')
    info = path.stat()
    _require(stat.S_ISREG(info.st_mode), f'{raw} is not a regular file')
    if os.name == 'posix':  # Windows has no comparable mode bits.
        _require(not info.st_mode & 0o077, f'{raw} must be readable by its owner only (chmod 600)')
    return path.read_text(encoding='utf-8')


def _from_xml(data, service, source):
    try:
        config = ET.fromstring(data)
    except ET.ParseError:
        raise SecretError('Service credentials: ' + describe(service, source) + ' is not valid XML') from None
    return (config.findtext('ApiKey') or '').strip(), (config.findtext('UrlBase') or '').strip()


def read_credential(service, runtime=None):
    """Return (value, url_base) for a service; url_base is '' where none applies."""
    if runtime is None:
        from runtime_config import get_config
        runtime = get_config()
    source = runtime.secrets[service]
    kind = source['source']
    url_base = source.get('url_base', '')
    value = ''
    try:
        if kind == 'docker_config_xml':
            value, url_base = _from_xml(_docker_cat(source['container'], source['path']), service, source)
        elif kind == 'config_xml':
            value, url_base = _from_xml(Path(source['path']).read_bytes(), service, source)
        elif kind == 'docker_file':
            for path in source['paths']:
                try:
                    value = _docker_cat(source['container'], path).decode('utf-8').strip()
                except (OSError, subprocess.SubprocessError, UnicodeDecodeError):
                    continue
                if value:
                    break
        elif kind == 'file':
            value = _owner_only_file(source['path']).strip()
        else:
            value = os.environ.get(source['variable'], '').strip()
    except SecretError:
        raise
    except (OSError, subprocess.SubprocessError, UnicodeDecodeError) as exc:
        # The exception type only: messages from cat/docker could echo file content.
        raise SecretError('Service credentials: cannot read ' + describe(service, source)
                          + ': ' + type(exc).__name__) from None
    _require(value, describe(service, source) + ' is missing or empty')
    return value, url_base.rstrip('/')


def read_key(service, runtime=None):
    return read_credential(service, runtime)[0]


def service_endpoint(service, runtime=None):
    """Return (api_root, key) for radarr, sonarr or jellyfin; API paths go after api_root.

    api_root is runtime.json urls[service]. When that URL has no base path of
    its own, Radarr/Sonarr append their UrlBase (config.xml, or url_base on an
    env/file source); a path written in urls wins.
    """
    if runtime is None:
        from runtime_config import get_config
        runtime = get_config()
    _require(service in runtime.urls, service + ' has no URL in runtime.json urls')
    key, url_base = read_credential(service, runtime)
    url = runtime.urls[service]
    return (url if urlsplit(url).path else url + url_base), key
