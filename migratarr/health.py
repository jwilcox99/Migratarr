"""Health checks shared by `migratarr_doctor.py` and the Settings & health page.

Each check returns Check(id, group, label, status, detail, fix):

  config       each settings file loads with no errors (migratarr.settings report)
  services     each registry service: credential readable, reachable, key accepted
  arr_roots    Radarr/Sonarr root folders include every category folder Migratarr moves into
  disks        each disk exists, is mounted (nas_ssh), writable (local), above its reserve
  nas          (nas_ssh) SSH with the configured key works, the NAS Python runs, each
               NAS disk root exists and is writable there
  permissions  category folders and the item folders in them are group-writable

Read-only: nothing here writes a file, changes a service, or prints a
credential. Details carry HTTP status codes and exception type names, never
response bodies or command output that could echo a key. Standard library only.
"""
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path, PurePosixPath
import shlex
import stat
import subprocess
import urllib.error
import urllib.request

from migratarr import services

OK, WARN, FAIL, SKIP = 'ok', 'warn', 'fail', 'skip'
ORDER = {FAIL: 0, WARN: 1, OK: 2, SKIP: 3}
GROUPS = ('config', 'services', 'arr_roots', 'disks', 'nas', 'permissions')
TIMEOUT = 10
GiB = 1024 ** 3


@dataclass(frozen=True)
class Check:
    id: str
    group: str
    label: str
    status: str
    detail: str = ''
    fix: str = ''

    def as_dict(self):
        return asdict(self)


def worst(checks):
    return min((c.status for c in checks), key=ORDER.get, default=OK)


# ---------------------------------------------------------------- HTTP

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # A redirect could forward the key to another server; the executors refuse them too.
    def redirect_request(self, *args, **kwargs):
        return None


def http_get(url, headers, *, local=True, timeout=TIMEOUT):
    """GET url → (status, parsed JSON or None). Raises OSError subclasses on network failure."""
    handlers = [_NoRedirect] + ([urllib.request.ProxyHandler({})] if local else [])
    opener = urllib.request.build_opener(*handlers)
    request = urllib.request.Request(url, headers=dict(headers, Accept='application/json'))
    try:
        with opener.open(request, timeout=timeout) as response:
            body = response.read(1024 * 1024)
            status = response.status
    except urllib.error.HTTPError as exc:
        return exc.code, None
    try:
        return status, json.loads(body)
    except ValueError:
        return status, None


def _network_reason(exc):
    reason = getattr(exc, 'reason', None)
    if isinstance(reason, BaseException):
        return type(reason).__name__ + (': ' + reason.strerror if getattr(reason, 'strerror', None) else '')
    return type(exc).__name__


# ---------------------------------------------------------------- checks

def config_checks(report):
    labels = {'runtime': 'runtime.json', 'storage_targets': 'storage-targets.json', 'planner': 'planner.json'}
    checks = []
    for name in ('runtime', 'storage_targets', 'planner'):
        errors = [p for p in report.problems if p.file == name and p.severity == 'error']
        warnings = [p for p in report.problems if p.file == name and p.severity == 'warning']
        path = str(report.files.path(name))
        if errors:
            detail = '; '.join(f'{p.field}: {p.message}' for p in errors[:5])
            if len(errors) > 5:
                detail += f'; and {len(errors) - 5} more'
            checks.append(Check('config.' + name, 'config', labels[name], FAIL, detail,
                                'Fix ' + path + ' (see docs/settings.md)'))
        elif warnings:
            checks.append(Check('config.' + name, 'config', labels[name], WARN,
                                '; '.join(p.message for p in warnings),
                                'Run: python3 migratarr_doctor.py --migrate'
                                if report.needs_migration and name == 'runtime' else ''))
        else:
            checks.append(Check('config.' + name, 'config', labels[name], OK, path))
    return checks


def _endpoint(descriptor, runtime):
    """(api_root, key) or raise SecretError; TMDB has no configurable URL."""
    from service_keys import read_credential, service_endpoint
    if descriptor.url_setting is None:
        return descriptor.probe.base_url, read_credential(descriptor.id, runtime)[0]
    return service_endpoint(descriptor.id, runtime)


def service_check(descriptor, runtime, http=http_get):
    from service_keys import SecretError, describe
    cid, label = 'service.' + descriptor.id, descriptor.label
    try:
        root, key = _endpoint(descriptor, runtime)
    except SecretError as exc:
        return Check(cid, 'services', label, FAIL, str(exc).replace('Service credentials: ', ''),
                     'Check where the ' + descriptor.credential_label + ' comes from: runtime.json "secrets"'), None
    probe = descriptor.probe
    url = root.rstrip('/') + probe.path
    shown = root  # validated: no credentials, query or fragment
    try:
        status, data = http(url, {probe.header: probe.header_format.format(key=key)},
                            local=probe.base_url is None)
    except (OSError, ValueError) as exc:
        return Check(cid, 'services', label, FAIL, f'cannot reach {shown}: {_network_reason(exc)}',
                     f'Is {label} running, and is its URL right ({descriptor.url_setting or "fixed"})?'), None
    source = describe(descriptor.id, runtime.secrets[descriptor.id])
    if status in (401, 403):
        return Check(cid, 'services', label, FAIL, f'{shown} rejected the key (HTTP {status}); {source}',
                     'Check the ' + descriptor.credential_label), None
    if status != 200 or not isinstance(data, dict) or (probe.expect_field and probe.expect_field not in data):
        return Check(cid, 'services', label, FAIL,
                     f'{shown} answered HTTP {status} without the expected response; is this {label}?',
                     'Check ' + (descriptor.url_setting or 'the service URL')
                     + (', including any URL base' if descriptor.url_base_from_config else '')), None
    version = str(data.get(probe.version_field, '')) if probe.version_field else ''
    detail = f'{shown}' + (f', version {version}' if version else '') + f'; key from {source}'
    if descriptor.id == 'sonarr' and version and not version.startswith('4.'):
        return Check(cid, 'services', label, WARN, detail, 'The TV executors require Sonarr v4'), (root, key)
    return Check(cid, 'services', label, OK, detail), (root, key)


def arr_root_checks(descriptor, endpoint, runtime, targets, http=http_get):
    """Every category folder Migratarr places this media type into must be an accessible root folder."""
    media = 'Movie' if descriptor.role == 'movies' else 'TV'
    cid, label = 'arr_roots.' + descriptor.id, descriptor.label + ' root folders'
    if endpoint is None:
        return Check(cid, 'arr_roots', label, SKIP, descriptor.label + ' is not reachable')
    root, key = endpoint
    try:
        status, data = http(root + '/api/v3/rootfolder', {'X-Api-Key': key})
    except (OSError, ValueError) as exc:
        return Check(cid, 'arr_roots', label, FAIL, 'cannot list root folders: ' + _network_reason(exc))
    if status != 200 or not isinstance(data, list):
        return Check(cid, 'arr_roots', label, FAIL, f'cannot list root folders (HTTP {status})')
    found = {r.get('path'): r.get('accessible') for r in data if isinstance(r, dict)}
    expected = {str(targets.arr_root / targets.category_paths[media][category])
                for category in targets.placement[media]}
    missing = sorted(p for p in expected if p not in found)
    inaccessible = sorted(p for p in expected if p in found and found[p] is not True)
    if missing or inaccessible:
        parts = (['missing: ' + ', '.join(missing)] if missing else []) + \
                (['not accessible: ' + ', '.join(inaccessible)] if inaccessible else [])
        return Check(cid, 'arr_roots', label, FAIL, '; '.join(parts),
                     f'Add them as root folders in {descriptor.label} (Settings > Media Management), or change '
                     'category_paths / arr_root in storage-targets.json. Moves into a missing root are refused.')
    return Check(cid, 'arr_roots', label, OK, ', '.join(sorted(expected)))


def disk_checks(runtime, targets, *, ismount=os.path.ismount, statvfs=os.statvfs, access=os.access):
    checks = []
    for target in targets.targets:
        cid, label = 'disk.' + target.id, f'Disk {target.id} ({target.name})'
        path = str(target.path)
        if not os.path.isdir(path):
            checks.append(Check(cid, 'disks', label, FAIL, path + ' does not exist or is not a folder',
                                'Mount the disk, or fix its path in storage-targets.json'))
            continue
        notes, status, fix = [path], OK, ''
        if runtime.topology == 'nas_ssh' and not ismount(path):
            status, fix = WARN, 'Expected the NAS disk to be mounted here; an empty folder looks like an empty disk'
            notes.append('not a mount point')
        if runtime.topology == 'local' and not access(path, os.W_OK):
            status, fix = FAIL, 'Moves on local disks need write access for the account running Migratarr'
            notes.append('not writable by this account')
        try:
            info = statvfs(path)
            free = info.f_bavail * info.f_frsize
            notes.append(f'{free / GiB:.0f} GiB free, reserve {target.minimum_free_space_gb} GiB')
            if free < target.minimum_free_space_gb * GiB and status == OK:
                status, fix = WARN, 'Below its reserve: the planner will not move anything onto it'
        except OSError as exc:
            notes.append('free space unknown: ' + type(exc).__name__)
        if not target.enabled:
            notes.append('disabled as a destination')
        checks.append(Check(cid, 'disks', label, status, '; '.join(notes), fix))
    return checks


def _destinations(targets):
    """(disk id, media, category, folder) for every placement destination."""
    by_id = {t.id: t for t in targets.targets}
    for media, categories in targets.placement.items():
        for category, ids in categories.items():
            for disk in ids:
                yield disk, media, category, by_id[disk].path / targets.category_paths[media][category]


def permission_checks(targets, *, limit=5, lstat=os.lstat, listdir=os.listdir):
    """Category folders that exist, and the item folders directly in them, need group write.

    The NAS-side executors and Radarr/Sonarr reach media through the shared
    group (migratarr_jobs.permission_problems has the incident). This looks
    one level deep only; the move job checks every folder of an item before it moves.
    """
    problems, checked, missing = [], 0, []
    seen = set()
    for disk, media, category, folder in _destinations(targets):
        if folder in seen:
            continue
        seen.add(folder)
        try:
            mode = lstat(str(folder)).st_mode
        except FileNotFoundError:
            missing.append(str(folder))
            continue
        except OSError as exc:
            problems.append(f'{folder}: {exc.strerror}')
            continue
        checked += 1
        if not mode & stat.S_IWGRP:
            problems.append(f'{folder} is {stat.filemode(mode)}')
        try:
            names = sorted(listdir(str(folder)))
        except OSError:
            continue
        for name in names:
            item = PurePosixPath(str(folder)) / name
            try:
                mode = lstat(str(item)).st_mode
            except OSError:
                continue
            if stat.S_ISDIR(mode):
                checked += 1
                if not mode & stat.S_IWGRP:
                    problems.append(f'{item} is {stat.filemode(mode)}')
    checks = []
    if problems:
        shown = '; '.join(problems[:limit]) + (f'; and {len(problems) - limit} more' if len(problems) > limit else '')
        checks.append(Check('permissions.group_write', 'permissions', 'Group write on media folders', WARN,
                            f'{len(problems)} of {checked} folders lack group write: {shown}',
                            'chmod g+w those folders (a move of an item without it stops before deleting)'))
    else:
        checks.append(Check('permissions.group_write', 'permissions', 'Group write on media folders', OK,
                            f'{checked} folders checked'))
    if missing:
        checks.append(Check('permissions.category_folders', 'permissions', 'Category folders', WARN,
                            'not found: ' + ', '.join(missing[:limit])
                            + (f' and {len(missing) - limit} more' if len(missing) > limit else ''),
                            'Create them, or the planner refuses moves into them (DESTINATION_ROOT_MISSING)'))
    return checks


# The program run on the NAS: standard library only, paths in on stdin, facts out on stdout.
NAS_PROBE = r'''
import json, os, sys
roots = json.load(sys.stdin)
out = {"python": list(sys.version_info[:3]), "roots": {}}
for disk, root in roots.items():
    entry = {"exists": os.path.isdir(root)}
    if entry["exists"]:
        entry["writable"] = os.access(root, os.W_OK)
        try:
            s = os.statvfs(root)
            entry["free"] = s.f_bavail * s.f_frsize
        except OSError:
            entry["free"] = None
    out["roots"][disk] = entry
print(json.dumps(out))
'''


def ssh_command(runtime, remote):
    # Same options as the executors' NasTransport: dedicated key, never prompt, known host only.
    return ['ssh', '-i', runtime.ssh_key, '-o', 'IdentitiesOnly=yes', '-o', 'BatchMode=yes',
            '-o', 'StrictHostKeyChecking=yes', '-o', 'ConnectTimeout=15', runtime.ssh_target, remote]


def nas_checks(runtime, run=subprocess.run):
    if runtime.topology != 'nas_ssh':
        return [Check('nas', 'nas', 'NAS over SSH', SKIP, 'topology local: no NAS')]
    remote = shlex.quote(runtime.remote_python) + ' -c ' + shlex.quote(NAS_PROBE)
    roots = {disk: entry['remote_path'] for disk, entry in runtime.storage.items()}
    try:
        result = run(ssh_command(runtime, remote), input=json.dumps(roots), capture_output=True,
                     text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        return [Check('nas.ssh', 'nas', 'NAS over SSH', FAIL, 'ssh did not run: ' + type(exc).__name__,
                      'Is the OpenSSH client installed?')]
    if result.returncode == 255:
        tail = (result.stderr or '').strip().splitlines()[-1:] or ['no message']
        return [Check('nas.ssh', 'nas', 'NAS over SSH', FAIL, f'{runtime.ssh_target}: {tail[0]}',
                      'Check nas.host, nas.user, nas.ssh_key and that the NAS host key is in known_hosts '
                      '(connect once by hand)')]
    try:
        facts = json.loads(result.stdout)
    except ValueError:
        return [Check('nas.ssh', 'nas', 'NAS over SSH', OK, runtime.ssh_target),
                Check('nas.python', 'nas', 'NAS Python', FAIL,
                      f'{runtime.remote_python} exited with status {result.returncode}',
                      'Set nas.python to a Python 3 on the NAS')]
    checks = [Check('nas.ssh', 'nas', 'NAS over SSH', OK, runtime.ssh_target)]
    version = facts.get('python', [0])
    checks.append(Check('nas.python', 'nas', 'NAS Python', OK if version[0] == 3 and version[1] >= 6 else FAIL,
                        f'{runtime.remote_python} {".".join(map(str, version))}',
                        '' if version[0] == 3 and version[1] >= 6 else 'The NAS program needs Python 3.6 or newer'))
    for disk, entry in sorted(facts.get('roots', {}).items()):
        label, root = f'NAS disk {disk}', roots[disk]
        if not entry.get('exists'):
            checks.append(Check('nas.disk.' + disk, 'nas', label, FAIL, root + ' not found on the NAS',
                                'Fix remote_path for ' + disk + ' in storage-targets.json'))
        elif not entry.get('writable'):
            checks.append(Check('nas.disk.' + disk, 'nas', label, FAIL, root + ' is not writable by '
                                + runtime.ssh_target.split('@')[0], 'Moves rename and copy as this NAS user'))
        else:
            free = entry.get('free')
            checks.append(Check('nas.disk.' + disk, 'nas', label, OK,
                                root + (f'; {free / GiB:.0f} GiB free' if free is not None else '')))
    return checks


# ---------------------------------------------------------------- all together

def run_checks(report=None, *, environ=None, groups=GROUPS, http=http_get, run=subprocess.run, **fs):
    """Run the selected groups and return their checks (config first)."""
    from migratarr import settings
    report = report or settings.load(environ=environ)
    checks = config_checks(report) if 'config' in groups else []
    runtime, targets = report.runtime, report.targets
    if runtime is None or targets is None:
        later = [g for g in groups if g != 'config']
        if later:
            checks.append(Check('skipped', 'config', 'Other checks', SKIP,
                                'skipped until runtime.json and storage-targets.json load: ' + ', '.join(later)))
        return checks
    endpoints = {}
    if 'services' in groups or 'arr_roots' in groups:
        for descriptor in services.SERVICES.values():
            check, endpoint = service_check(descriptor, runtime, http)
            endpoints[descriptor.id] = endpoint
            if 'services' in groups:
                checks.append(check)
    if 'arr_roots' in groups:
        for descriptor in services.SERVICES.values():
            if descriptor.role in ('movies', 'tv'):
                checks.append(arr_root_checks(descriptor, endpoints.get(descriptor.id), runtime, targets, http))
    if 'disks' in groups:
        checks += disk_checks(runtime, targets, **{k: v for k, v in fs.items()
                                                   if k in ('ismount', 'statvfs', 'access')})
    if 'nas' in groups:
        checks += nas_checks(runtime, run)
    if 'permissions' in groups:
        checks += permission_checks(targets, **{k: v for k, v in fs.items() if k in ('lstat', 'listdir')})
    return checks
