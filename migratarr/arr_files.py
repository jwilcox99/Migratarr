"""How executors check media the way Radarr/Sonarr see it.

Executors confirm, before and after a move, that the Arr can see each file at
its Arr path (under storage-targets.json arr_root) and that its content hashes
as expected. runtime.json may choose per service, under an optional
"arr_file_checks" object:

  {"mode": "docker"}                   default: `docker exec <container> test`
                                       / `sha256sum` inside the Arr container
                                       (container: containers.radarr/.sonarr)
  {"mode": "host"}                     the Arr runs on this host, so its paths
                                       are host paths; check them directly
  {"mode": "host", "host_root": "..."} the Arr sees host_root as arr_root (a
                                       bind mount); translate and check here

docker is the strongest check: it proves the container's own mounts show the
file. host with host_root proves only that this host does; a container mounted
wrongly would go unnoticed.
"""
import hashlib
import os
from pathlib import PurePosixPath
import re

ARR = ('radarr', 'sonarr')
CHUNK = 1024 * 1024


class ArrFilesError(ValueError):
    pass


def _require(ok, message):
    if not ok:
        raise ArrFilesError('Arr file checks: ' + message)


def parse_arr_file_checks(data, containers):
    """Validate runtime.json "arr_file_checks" (no I/O); return the effective mode per Arr."""
    _require(isinstance(data, dict), 'arr_file_checks must be an object')
    unknown = set(data) - set(ARR)
    _require(not unknown, 'unknown services: ' + ', '.join(sorted(unknown)))
    result = {}
    for service in ARR:
        entry = dict(data.get(service, {'mode': 'docker'}))
        label = 'arr_file_checks.' + service
        mode = entry.pop('mode', None)
        _require(mode in {'docker', 'host'}, label + '.mode must be docker or host')
        allowed = {'docker': {'container'}, 'host': {'host_root'}}[mode]
        _require(set(entry) <= allowed, label + ' (' + mode + ') takes only: ' + ', '.join(sorted(allowed)))
        if mode == 'docker':
            container = entry.get('container') or containers.get(service)
            _require(isinstance(container, str) and re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]*', container),
                     label + ' needs a container (set it here or in runtime.json containers)')
            result[service] = {'mode': 'docker', 'container': container}
        else:
            root = entry.get('host_root')
            _require(root is None or (isinstance(root, str) and root and os.path.isabs(root)
                                      and not any(ord(c) < 32 for c in root)
                                      and os.path.normpath(root) == root.rstrip('/\\')
                                      and not root.endswith(('/', '\\'))),
                     label + '.host_root must be a canonical absolute host path')
            result[service] = {'mode': 'host', 'host_root': root}
    return result


def host_path(check, arr_root, arr_path):
    """The host path to check for an Arr path, or None if it can't be mapped."""
    arr_path = str(arr_path)
    p = PurePosixPath(arr_path)
    if not p.is_absolute() or str(p) != arr_path or '..' in p.parts:
        return None
    if check['host_root'] is None:
        return arr_path
    root = str(arr_root)
    if arr_path == root:
        return check['host_root']
    if not arr_path.startswith(root.rstrip('/') + '/'):
        return None
    return check['host_root'] + arr_path[len(root.rstrip('/')):]


def host_visible(path, kind):
    """`test -f` / `test -d` on this host (both follow symlinks, like test)."""
    if path is None:
        return False
    return os.path.isfile(path) if kind == '-f' else os.path.isdir(path) if kind == '-d' else False


def host_sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(CHUNK), b''):
            digest.update(block)
    return digest.hexdigest()
