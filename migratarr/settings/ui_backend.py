"""The settings pages' backend: settings_contract over the Phase 1 settings layer.

settings_ui.py (Settings) and setup_wizard.py (first run) edit settings
through settings_contract.SettingsBackend and HealthBackend. This module
implements both on top of migratarr.settings (load / check / save /
store_key), migratarr.services (one descriptor per service; a new
integration is a new descriptor, not a new form) and migratarr.health.

Field paths are the pages' dotted names; each section reads from and writes
to one settings file:

    services.<id>.url|source|key|container|paths|variable|url_base   runtime.json
    services.arr_install (docker | host | custom)                       runtime.json arr_file_checks
    topology.mode|base_path|nas.host|nas.user|nas.ssh_key|nas.python  runtime.json
    disks.<disk>.name|path|remote_path|enabled|media_types|min_free_gb|remove,
    disks._new.*, disks._defaults.min_free_gb, disks._arr_root,
    disks._folders.<Movie|TV>.<category>, disks._placement.<Movie|TV>.<category>
                                                                     storage-targets.json
    streaming.*, scoring.weights.*, overrides.*                      planner.json

Disk ids can't start with "_", so the "_" names never collide with a disk.

Rules kept from the contract: a credential value never leaves this module
(current() says SECRET_KEPT, pasted keys go straight to keys.store_key and
only the returned path is written); validate() and diff() never write;
save() and create() re-check, refuse while a job runs, back up and write
atomically (migratarr.settings.save). Standard library only.
"""
import copy
import hashlib
import json
import os
import re
from pathlib import Path, PurePosixPath
import subprocess

from migratarr import settings_contract as sc
from migratarr import health, services
from migratarr.settings import keys, store

RUNTIME, TARGETS, PLANNER = store.RUNTIME, store.TARGETS, store.PLANNER
SECTION_FILE = {sc.SERVICES: RUNTIME, sc.TOPOLOGY: RUNTIME, sc.DISKS: TARGETS,
                sc.STREAMING: PLANNER, sc.SCORING: PLANNER, sc.OVERRIDES: PLANNER}
PASTE = 'paste'  # a pseudo-source: the "file" source, at keys.store_key's owner-only path
SOURCE_LABELS = {
    'docker_config_xml': "Read automatically from the Docker container's config.xml",
    'config_xml': 'Read from config.xml on this machine',
    'docker_file': 'Read a file inside a Docker container',
    'file': 'Read from a file on this machine (owner-only)',
    'env': 'Read from an environment variable',
    PASTE: 'Paste the key here (saved to an owner-only file)',
}
# How Radarr and Sonarr are installed: picks runtime.json arr_file_checks (migratarr/arr_files.py) and which
# key sources and fields the Services form offers them. "custom" is shown only for an arr_file_checks
# written by hand that is neither (a host_root, or one Arr each way); the form leaves it as it is.
ARR_INSTALL = 'services.arr_install'
ARR_SERVICES = tuple(d.id for d in services.SERVICES.values() if d.url_base_from_config)
ARR_INSTALL_CHOICES = (('docker', 'Radarr and Sonarr run in Docker containers'),
                       ('host', 'Radarr and Sonarr are installed directly on this machine'))
ARR_INSTALL_CUSTOM = ('custom', 'Set by hand in runtime.json (arr_file_checks); keep as it is')
# Migratarr in its Docker image (MIGRATARR_IN_CONTAINER=1, set by the Dockerfile): other containers' files and
# the Docker CLI are out of reach, so Radarr and Sonarr are checked through Migratarr's own media mount (host
# mode, same path in every container) and keys are pasted. Fresh installs point at the usual container names.
CONTAINER_INSTALL = ('host', 'Same media path as Migratarr')
CONTAINER_URLS = {'radarr': 'http://radarr:7878', 'sonarr': 'http://sonarr:8989', 'jellyfin': 'http://jellyfin:8096'}


def in_container(environ=None):
    return (os.environ if environ is None else environ).get('MIGRATARR_IN_CONTAINER') == '1'


def container_services(values):
    """A Services form as it can work inside the container: host-mode checks, no key read from a container."""
    values = dict(values)
    if values.get(ARR_INSTALL) in (None, '', 'docker'):
        values[ARR_INSTALL] = 'host'
    for d in services.SERVICES.values():
        p = 'services.' + d.id
        if (values.get(p + '.source') or '').startswith('docker'):
            values[p + '.source'] = PASTE
        if p + '.container' in values:
            values[p + '.container'] = None
    return values
# Where Radarr's and Sonarr's own installers keep config.xml on Linux (Detect looks only here).
HOST_CONFIG_XML = {'radarr': ('/var/lib/radarr/config.xml', '{home}/.config/Radarr/config.xml'),
                   'sonarr': ('/var/lib/sonarr/config.xml', '{home}/.config/Sonarr/config.xml')}
TOPOLOGY_CHOICES = (('nas_ssh', 'On a NAS, over SSH'), ('local', "On this machine's own disks (experimental)"))
MEDIA = (('Movie', 'Movies'), ('TV', 'TV'))
CATEGORY_FOLDERS = {  # a fresh install's folders; category identities are fixed (plan risk list)
    'Movie': {'Common': 'Movies/Common', 'Rare': 'Movies/Rare', 'Library': 'Movies/Library',
              'Archive': 'Movies/Archive'},
    'TV': {'Current': 'TV/Current', 'Rare': 'TV/Rare', 'Library': 'TV/Library', 'Archive': 'TV/Archive'},
}
OVERRIDE_CATEGORIES = ('Common', 'Current', 'Library', 'Rare', 'Archive')
WEIGHTS = ('replacement', 'streaming', 'usage')
SEVERITY = {'error': sc.FAIL, 'warning': sc.WARN}
NAS_REQUIRED = {
    'host': 'Enter the NAS address: its IP address or host name.',
    'user': 'Enter the account Migratarr logs in to the NAS as.',
    'ssh_key': 'Enter the full path to the SSH private key Migratarr uses for the NAS, '
               'e.g. /home/you/.ssh/migratarr_nas.',
    'python': 'Enter the path to Python 3 on the NAS, e.g. /usr/bin/python3.',
}
IPV4 = re.compile(r'\d{1,3}(\.\d{1,3}){3}')
DISCOVERS = (sc.SERVICES, sc.DISKS)
CHECK_STATUS = {health.OK: sc.OK, health.WARN: sc.WARN, health.FAIL: sc.FAIL, health.SKIP: sc.SKIPPED}


def _dict(value):
    return value if isinstance(value, dict) else {}


def _list(value):
    return list(value) if isinstance(value, list) else []


def _blank(value):
    return value is None or value == '' or value == []


def default_base(environ=None):
    """Where runs, manifests and logs go for a new install: the data folder (migratarr.paths)."""
    from migratarr.paths import home
    return str(home(environ))


def templates(base=None, container=False):
    """The three documents of a fresh install, before the wizard's values are applied."""
    urls = {d.url_setting.split('.', 1)[1]: d.default_url for d in services.SERVICES.values() if d.url_setting}
    runtime = {'schema_version': 2, 'base_path': base or default_base(), 'topology': 'nas_ssh',
               'containers': {'radarr': 'radarr', 'sonarr': 'sonarr', 'homepage': 'homepage'},
               'urls': {k: CONTAINER_URLS.get(k, v) for k, v in urls.items()} if container else urls,
               'nas': {'host': '', 'user': '', 'ssh_key': '', 'python': '/usr/bin/python3'}}
    if container:
        runtime['arr_file_checks'] = {a: {'mode': 'host'} for a in ARR_SERVICES}
    targets = {'schema_version': 1, 'defaults': {'minimum_free_space_gb': 50},
               'targets': [{'id': 'media01', 'name': 'Disk 1', 'path': '', 'remote_path': '', 'enabled': True,
                            'media_types': ['Movie', 'TV']}],
               'category_paths': copy.deepcopy(CATEGORY_FOLDERS),
               'placement': {m: {c: ['media01'] for c in cats} for m, cats in CATEGORY_FOLDERS.items()}}
    planner = {'schema_version': 1, 'streaming': {'region': '', 'subscribed': [], 'user_free_access': []}}
    return {RUNTIME: runtime, TARGETS: targets, PLANNER: planner}


# ---------------------------------------------------------------- fields

def arr_install_field(value, container=False):
    custom = (ARR_INSTALL_CUSTOM,) if value == ARR_INSTALL_CUSTOM[0] else ()
    if container:
        return sc.Field(ARR_INSTALL, 'Radarr and Sonarr', sc.CHOICE,
                        'Migratarr runs in a container, so it checks moved files through its own media mount. '
                        'Mount your media at the same path in Migratarr, Radarr and Sonarr (for example /data), '
                        'and paste each key (Settings > General > API Key).',
                        True, (CONTAINER_INSTALL,) + custom, 'How Radarr and Sonarr run')
    return sc.Field(ARR_INSTALL, 'Radarr and Sonarr', sc.CHOICE,
                    'Docker: Migratarr reads their keys and checks media inside their containers (docker exec). '
                    'Installed directly: it reads config.xml and checks media on this machine.',
                    True, ARR_INSTALL_CHOICES + custom, 'How Radarr and Sonarr run')


def on_host(d, install):
    """Radarr/Sonarr installed directly on this machine: no container to name or read from."""
    return d.url_base_from_config and install == 'host'


def service_fields(d, install='docker', container=False):
    example = CONTAINER_URLS.get(d.id, d.default_url) if container else d.default_url
    p, g = 'services.' + d.id, d.label
    host = on_host(d, install)
    sources = tuple(s for s in d.sources if not ((host or container) and s.startswith('docker')))
    choices = tuple((s, SOURCE_LABELS[s]) for s in sources) + (
        ((PASTE, SOURCE_LABELS[PASTE]),) if 'file' in d.sources else ())
    out = []
    if d.url_setting:
        out.append(sc.Field(p + '.url', 'URL', sc.URL, f'For example {example}', True, group=g))
    out.append(sc.Field(p + '.source', d.credential_label, sc.CHOICE, 'Where Migratarr reads the '
                        + d.credential_label + ' from.' + (f' Find it at {d.help_url}' if d.help_url else ''),
                        True, choices, g))
    if 'file' in d.sources:
        out.append(sc.Field(p + '.key', 'Pasted ' + d.credential_label, sc.SECRET,
                            'Only with "Paste the key". Leave blank to keep a saved key.', group=g))
    if any(s.startswith('docker') for s in sources):
        default = d.container_setting.split('.', 1)[1] if d.container_setting else None
        out.append(sc.Field(p + '.container', 'Docker container', sc.TEXT,
                            'Only when the key is read from a container'
                            + (f' (default name: {default}).' if default else '.'), group=g))
    if host:
        out.append(sc.Field(p + '.paths', 'File path', sc.LINES,
                            'Only when the key is read from a file: one path, for example '
                            f'/var/lib/{d.id}/config.xml.', group=g))
    elif any(s in d.sources for s in ('config_xml', 'file', 'docker_file', 'docker_config_xml')):
        out.append(sc.Field(p + '.paths', 'File path', sc.LINES,
                            'Only when the key is read from a file. One path; a file inside a container may '
                            'list several, tried in order. Blank with the container\'s config.xml means '
                            '/config/config.xml.', group=g))
    if 'env' in d.sources:
        out.append(sc.Field(p + '.variable', 'Environment variable', sc.TEXT,
                            'Only with "environment variable", e.g. TMDB_TOKEN.', group=g))
    if d.url_base_from_config:
        out.append(sc.Field(p + '.url_base', 'URL base', sc.TEXT,
                            'Only with a file, a pasted key or a variable (config.xml supplies its own). '
                            'For example /radarr; blank for none.', group=g))
    return out


def disk_ids(values):
    seen = []
    for path in values:
        parts = path.split('.')
        if len(parts) >= 3 and parts[0] == 'disks' and not parts[1].startswith('_') and parts[1] not in seen:
            seen.append(parts[1])
    return seen


def disk_fields(values, nas):
    ids = disk_ids(values)
    out = [sc.Field('disks._defaults.min_free_gb', 'Default free-space reserve (GB)', sc.INTEGER,
                    'Migratarr never fills a disk past this; each disk can set its own.', True, group='All disks'),
           sc.Field('disks._arr_root', 'Media root as Radarr and Sonarr see it', sc.PATH,
                    'Items live at <this>/<category folder>/<title> inside Radarr and Sonarr, e.g. /media.',
                    group='All disks')]
    for d in ids:
        g = 'Disk ' + d
        out += [sc.Field(f'disks.{d}.name', 'Name', sc.TEXT, required=True, group=g),
                sc.Field(f'disks.{d}.path', 'Mounted at (this machine)', sc.PATH, required=True, group=g)]
        if nas:
            out.append(sc.Field(f'disks.{d}.remote_path', 'Path on the NAS', sc.PATH,
                                'The same disk as the NAS sees it, e.g. /volume1/media01.', True, group=g))
        out += [sc.Field(f'disks.{d}.enabled', 'Move media onto this disk', sc.BOOL, group=g),
                sc.Field(f'disks.{d}.media_types', 'Holds', sc.MULTI, choices=MEDIA, group=g),
                sc.Field(f'disks.{d}.min_free_gb', 'Keep free (GB)', sc.INTEGER, 'Blank: the default.', group=g),
                sc.Field(f'disks.{d}.remove', 'Remove this disk', sc.BOOL,
                         'Migratarr stops managing media on it; nothing on the disk is touched.', group=g)]
    g = 'Add a disk'
    out += [sc.Field('disks._new.id', 'Short id', sc.TEXT, 'Lowercase, e.g. media05. Leave blank to add none.',
                     group=g),
            sc.Field('disks._new.name', 'Name', sc.TEXT, group=g),
            sc.Field('disks._new.path', 'Mounted at (this machine)', sc.PATH, group=g)]
    if nas:
        out.append(sc.Field('disks._new.remote_path', 'Path on the NAS', sc.PATH, group=g))
    choices = tuple((d, d) for d in ids)
    for media, label in MEDIA:
        for cat in CATEGORY_FOLDERS[media]:
            out.append(sc.Field(f'disks._folders.{media}.{cat}', cat, sc.TEXT, required=True,
                                group=label + ' category folders (relative to each disk)'))
        for cat in CATEGORY_FOLDERS[media]:
            out.append(sc.Field(f'disks._placement.{media}.{cat}', cat, sc.MULTI, choices=choices,
                                group=label + ': which disks hold each category'))
    return out


# ---------------------------------------------------------------- documents <-> values (pure)

def secrets_folder_path(environ):
    return keys.secrets_folder(environ).expanduser()


def read_section(section, docs, environ=None):
    """{path: value} for one section, from the three documents. Secrets: SECRET_KEPT or None."""
    runtime, targets, planner = _dict(docs.get(RUNTIME)), _dict(docs.get(TARGETS)), _dict(docs.get(PLANNER))
    v = {}
    if section == sc.SERVICES:
        secrets = _dict(runtime.get('secrets'))
        containers, urls = _dict(runtime.get('containers')), _dict(runtime.get('urls'))
        folder = secrets_folder_path(environ)
        v[ARR_INSTALL] = arr_install_of(runtime)
        for d in services.SERVICES.values():
            p = 'services.' + d.id
            entry = _dict(secrets.get(d.id)) or services.default_sources()[d.id]
            kind = entry.get('source')
            pasted = (kind == 'file' and isinstance(entry.get('path'), str)
                      and Path(entry['path']).expanduser() == folder / d.id)
            if d.url_setting:
                v[p + '.url'] = urls.get(d.url_setting.split('.', 1)[1])
            v[p + '.source'] = PASTE if pasted else kind
            if 'file' in d.sources:
                v[p + '.key'] = sc.SECRET_KEPT if pasted else None
            if any(s.startswith('docker') for s in d.sources):
                key = d.container_setting.split('.', 1)[1] if d.container_setting else None
                v[p + '.container'] = entry.get('container') or (containers.get(key) if key else None)
            if any(s in d.sources for s in ('config_xml', 'file', 'docker_file', 'docker_config_xml')):
                paths = entry.get('paths') if isinstance(entry.get('paths'), list) else (
                    [entry['path']] if isinstance(entry.get('path'), str) and not pasted else [])
                v[p + '.paths'] = [str(x) for x in paths]
            if 'env' in d.sources:
                v[p + '.variable'] = entry.get('variable') if kind == 'env' else None
            if d.url_base_from_config:
                v[p + '.url_base'] = entry.get('url_base')
    elif section == sc.TOPOLOGY:
        nas = _dict(runtime.get('nas'))
        mode = runtime.get('topology') or ('nas_ssh' if runtime.get('schema_version') == 1 else None)
        v = {'topology.mode': mode, 'topology.base_path': runtime.get('base_path')}
        for k in ('host', 'user', 'ssh_key', 'python'):
            v['topology.nas.' + k] = nas.get(k)
    elif section == sc.DISKS:
        v['disks._defaults.min_free_gb'] = _dict(targets.get('defaults')).get('minimum_free_space_gb')
        v['disks._arr_root'] = targets.get('arr_root', '/media')
        for t in _list(targets.get('targets')):
            t = _dict(t)
            d = t.get('id')
            if not isinstance(d, str) or not d or d.startswith('_') or '.' in d:
                continue
            v[f'disks.{d}.name'] = t.get('name')
            v[f'disks.{d}.path'] = t.get('path')
            v[f'disks.{d}.remote_path'] = t.get('remote_path')
            v[f'disks.{d}.enabled'] = bool(t.get('enabled', True))
            v[f'disks.{d}.media_types'] = [m for m in _list(t.get('media_types')) if m in ('Movie', 'TV')]
            v[f'disks.{d}.min_free_gb'] = t.get('minimum_free_space_gb')
            v[f'disks.{d}.remove'] = False
        for k in ('id', 'name', 'path', 'remote_path'):
            v['disks._new.' + k] = None
        folders, placement = _dict(targets.get('category_paths')), _dict(targets.get('placement'))
        for media in CATEGORY_FOLDERS:
            for cat in CATEGORY_FOLDERS[media]:
                v[f'disks._folders.{media}.{cat}'] = _dict(folders.get(media)).get(cat)
                v[f'disks._placement.{media}.{cat}'] = [str(x) for x in _list(_dict(placement.get(media)).get(cat))]
    elif section == sc.STREAMING:
        s = _dict(planner.get('streaming'))
        v = {'streaming.region': s.get('region'), 'streaming.subscribed': _list(s.get('subscribed')),
             'streaming.user_free_access': _list(s.get('user_free_access'))}
    elif section == sc.SCORING:
        from migratarr.planner_settings import DEFAULT_SCORING
        weights = _dict(_dict(planner.get('scoring')).get('weights'))
        v = {'scoring.weights.' + k: weights.get(k, DEFAULT_SCORING['weights'][k]) for k in WEIGHTS}
    elif section == sc.OVERRIDES:
        from migratarr.planner_settings import DEFAULT_OVERRIDES
        o = _dict(planner.get('overrides')) or DEFAULT_OVERRIDES
        by_category = {cat: tag for tag, cat in _dict(o.get('category_tags')).items()}
        v = {'overrides.lock_tag': o.get('lock_tag')}
        for cat in OVERRIDE_CATEGORIES:
            v['overrides.category_tags.' + cat] = by_category.get(cat)
    else:
        raise sc.SettingsError('There is no settings section called ' + str(section) + '.')
    return v


def arr_install_of(runtime):
    """docker, host or custom, from runtime.json arr_file_checks (absent: docker, the default)."""
    checks = runtime.get('arr_file_checks')
    if checks is None:
        return 'docker'
    checks = _dict(checks)
    entries = [_dict(checks.get(a, {'mode': 'docker'})) for a in ARR_SERVICES]
    if all(e.get('mode') == 'docker' for e in entries):
        return 'docker'
    if all(e == {'mode': 'host'} for e in entries):
        return 'host'
    return ARR_INSTALL_CUSTOM[0]


def write_arr_install(runtime, install):
    """Apply the choice to runtime.json arr_file_checks; custom (or anything else) leaves it alone."""
    if install == 'host':
        checks = dict(_dict(runtime.get('arr_file_checks')))
        checks.update({a: {'mode': 'host'} for a in ARR_SERVICES})
        runtime['arr_file_checks'] = checks
    elif install == 'docker':
        checks = {a: e for a, e in _dict(runtime.get('arr_file_checks')).items()
                  if not (a in ARR_SERVICES and _dict(e).get('mode') == 'host')}
        if checks:
            runtime['arr_file_checks'] = checks
        else:
            runtime.pop('arr_file_checks', None)


def normalize_services(values):
    """Keep key sources in step with how Radarr and Sonarr are installed. Returns (values, notes).

    Installed directly: a key read from a container becomes config.xml on this machine, and the form is
    shown again for its path. Docker: a container field missing from the form (it was hidden) gets its
    default name.
    """
    values, notes = dict(values), []
    install = values.get(ARR_INSTALL) or 'docker'
    for d in services.SERVICES.values():
        if not d.url_base_from_config:
            continue
        p = 'services.' + d.id
        if on_host(d, install):
            values[p + '.container'] = None  # its runtime.json containers entry goes (shown on the review)
        if on_host(d, install) and (values.get(p + '.source') or '').startswith('docker'):
            values[p + '.source'] = 'config_xml'
            if values.get(p + '.paths') in (None, [], ['/config/config.xml']):
                values[p + '.paths'] = []
            notes.append(sc.Problem(p + '.paths', f'{d.label} runs on this machine, so its key is read from '
                                                  'its config.xml here. Give the path to that file (for '
                                                  f'example /var/lib/{d.id}/config.xml), or paste the key.',
                                    sc.WARN))
        elif install == 'docker' and p + '.container' not in values and d.container_setting:
            # Switched back from "installed directly": the container field was hidden, so use its default
            # name (shown on the review). No note: the Settings review reads the form in the saved shape, so
            # a note here would come back on every review.
            values[p + '.container'] = d.container_setting.split('.', 1)[1]
            if values.get(p + '.source') == 'config_xml' and not values.get(p + '.paths'):
                values[p + '.source'] = 'docker_config_xml'
    return values, notes


def normalize_disks(values):
    """Turn "Add a disk" into a disk and drop removed disks. Returns (values, notes)."""
    values, notes = dict(values), []
    new_id = (values.get('disks._new.id') or '').strip()
    if new_id:
        if new_id in disk_ids(values):
            return values, [sc.Problem('disks._new.id', 'There is already a disk called ' + new_id + '.')]
        values.update({f'disks.{new_id}.name': values.get('disks._new.name') or new_id,
                       f'disks.{new_id}.path': values.get('disks._new.path'),
                       f'disks.{new_id}.remote_path': values.get('disks._new.remote_path'),
                       f'disks.{new_id}.enabled': True, f'disks.{new_id}.media_types': ['Movie', 'TV'],
                       f'disks.{new_id}.min_free_gb': None, f'disks.{new_id}.remove': False})
        notes.append(sc.Problem(sc.DISKS, f'Disk {new_id} was added below. Choose which categories it holds, '
                                          'then continue.', sc.WARN))
    for k in ('id', 'name', 'path', 'remote_path'):
        values['disks._new.' + k] = None
    for d in disk_ids(values):
        if values.get(f'disks.{d}.remove'):
            for path in [p for p in values if p.startswith(f'disks.{d}.')]:
                del values[path]
            for path, ids in list(values.items()):
                if path.startswith('disks._placement.') and isinstance(ids, list):
                    values[path] = [i for i in ids if i != d]
    return values, notes


def kind_of(values, d):
    return values.get('services.' + d.id + '.source') or services.default_sources()[d.id]['source']


def write_section(section, docs, values, environ=None):
    """Apply one section's values to copies of the documents.

    Returns (docs, pasted, problems): pasted is {service: (key, url_base)} for keys typed in, which
    the caller stores with keys.store_key; until then their entry points at where the key will be.
    """
    docs = {name: copy.deepcopy(doc) for name, doc in docs.items()}
    problems, pasted = [], {}
    runtime = docs.setdefault(RUNTIME, {}) if SECTION_FILE[section] == RUNTIME else None
    if section == sc.SERVICES:
        secrets = dict(_dict(runtime.get('secrets')))
        containers, urls = dict(_dict(runtime.get('containers'))), dict(_dict(runtime.get('urls')))
        folder = secrets_folder_path(environ)
        install = values.get(ARR_INSTALL)
        write_arr_install(runtime, install)
        for d in services.SERVICES.values():
            p = 'services.' + d.id
            old = _dict(secrets.get(d.id))
            if on_host(d, install) and kind_of(values, d).startswith('docker'):
                problems.append(sc.Problem(p + '.source', f'{d.label} is installed directly on this machine: '
                                                          'read its key from config.xml here, a file, a '
                                                          'variable, or paste it.'))
            if d.url_setting:
                name = d.url_setting.split('.', 1)[1]
                if _blank(values.get(p + '.url')):
                    urls.pop(name, None)
                else:
                    urls[name] = values[p + '.url']
            kind = values.get(p + '.source') or services.default_sources()[d.id]['source']
            paths = [x for x in (values.get(p + '.paths') or []) if x]
            url_base = values.get(p + '.url_base')
            entry = {'source': 'file' if kind == PASTE else kind}
            container = values.get(p + '.container')
            container_key = d.container_setting.split('.', 1)[1] if d.container_setting else None
            if on_host(d, install):
                # Nothing runs `docker exec` in it any more (a container key source is refused above; keep
                # the entry then, so that is the only problem reported).
                if container_key and 'container' not in old and not kind.startswith('docker'):
                    containers.pop(container_key, None)
            elif any(s.startswith('docker') for s in d.sources):
                if container_key and 'container' not in old:
                    if _blank(container):
                        containers.pop(container_key, None)
                    else:
                        containers[container_key] = container
                elif kind.startswith('docker') and not _blank(container):
                    entry['container'] = container
            if kind == PASTE:
                key = values.get(p + '.key')
                entry['path'] = str(folder / d.id)
                if isinstance(key, str) and key:
                    pasted[d.id] = key
                elif key is not sc.SECRET_KEPT:
                    problems.append(sc.Problem(p + '.key', 'Paste the key, or choose another way to read it.'))
            elif kind in ('config_xml', 'file'):
                if len(paths) != 1:
                    problems.append(sc.Problem(p + '.paths', 'Give one file path.'))
                entry['path'] = paths[0] if paths else ''
            elif kind == 'docker_file':
                entry['paths'] = paths
            elif kind == 'docker_config_xml':
                if len(paths) > 1:
                    problems.append(sc.Problem(p + '.paths', 'Give at most one path inside the container.'))
                if paths and paths[0] != '/config/config.xml':
                    entry['path'] = paths[0]
            elif kind == 'env':
                entry['variable'] = values.get(p + '.variable') or ''
            if d.url_base_from_config and kind in ('file', 'env', PASTE) and not _blank(url_base):
                entry['url_base'] = url_base
            if entry == services.default_sources()[d.id] and d.id not in secrets:
                continue  # the default needs no entry
            secrets[d.id] = entry
        runtime['urls'], runtime['containers'] = urls, containers
        if secrets:
            runtime['secrets'] = secrets
        else:
            runtime.pop('secrets', None)
    elif section == sc.TOPOLOGY:
        if runtime.get('schema_version') == 1:
            problems.append(sc.Problem(sc.TOPOLOGY, 'runtime.json is still version 1. Run "migratarr doctor '
                                                    '--migrate" (or restart the UI) before changing this.'))
        mode = values.get('topology.mode') or runtime.get('topology') or 'nas_ssh'
        runtime['topology'] = mode
        if not _blank(values.get('topology.base_path')):
            runtime['base_path'] = values['topology.base_path']
        if mode == 'nas_ssh':
            runtime['nas'] = {k: values.get('topology.nas.' + k) or '' for k in ('host', 'user', 'ssh_key', 'python')}
            for k, message in NAS_REQUIRED.items():
                if _blank(values.get('topology.nas.' + k)):
                    problems.append(sc.Problem('topology.nas.' + k, message))
            user = values.get('topology.nas.user') or ''
            if IPV4.fullmatch(user):
                problems.append(sc.Problem('topology.nas.user', 'This looks like an IP address. The NAS address goes '
                                                                'in "NAS host"; this is the account on the NAS.'))
        else:
            runtime.pop('nas', None)
            targets = _dict(docs.get(TARGETS))
            for t in _list(targets.get('targets')):
                if isinstance(t, dict):
                    t.pop('remote_path', None)
    elif section == sc.DISKS:
        targets = docs.setdefault(TARGETS, {})
        nas = _dict(docs.get(RUNTIME)).get('topology', 'nas_ssh') == 'nas_ssh'
        old = {t.get('id'): t for t in _list(targets.get('targets')) if isinstance(t, dict)}
        reserve = values.get('disks._defaults.min_free_gb')
        targets['defaults'] = dict(_dict(targets.get('defaults')),
                                   minimum_free_space_gb=reserve if reserve is not None else 50)
        arr_root = values.get('disks._arr_root')
        if _blank(arr_root) or (arr_root == '/media' and 'arr_root' not in targets):
            targets.pop('arr_root', None)
        else:
            targets['arr_root'] = arr_root
        new_targets = []
        for d in disk_ids(values):
            t = dict(old.get(d, {}))  # keeps storage_class, priority and tags, which the form doesn't edit
            t.update(id=d, name=values.get(f'disks.{d}.name') or '', path=values.get(f'disks.{d}.path') or '',
                     enabled=bool(values.get(f'disks.{d}.enabled')),
                     media_types=list(values.get(f'disks.{d}.media_types') or []))
            if nas:
                t['remote_path'] = values.get(f'disks.{d}.remote_path') or ''
            else:
                t.pop('remote_path', None)
            if values.get(f'disks.{d}.min_free_gb') is None:
                t.pop('minimum_free_space_gb', None)
            else:
                t['minimum_free_space_gb'] = values[f'disks.{d}.min_free_gb']
            new_targets.append(t)
        targets['targets'] = new_targets
        folders = {m: dict(_dict(_dict(targets.get('category_paths')).get(m))) for m in CATEGORY_FOLDERS}
        placement = {m: dict(_dict(_dict(targets.get('placement')).get(m))) for m in CATEGORY_FOLDERS}
        for media, cats in CATEGORY_FOLDERS.items():
            for cat in cats:
                folder = values.get(f'disks._folders.{media}.{cat}')
                if not _blank(folder):
                    folders[media][cat] = folder
                ids = list(values.get(f'disks._placement.{media}.{cat}') or [])
                if ids:
                    placement[media][cat] = ids
                else:
                    placement[media].pop(cat, None)
        targets['category_paths'], targets['placement'] = folders, placement
    elif section in (sc.STREAMING, sc.SCORING, sc.OVERRIDES):
        planner = docs.setdefault(PLANNER, {'schema_version': 1})
        if section == sc.STREAMING:
            s = dict(_dict(planner.get('streaming')))
            s.update(region=values.get('streaming.region') or '',
                     subscribed=list(values.get('streaming.subscribed') or []),
                     user_free_access=list(values.get('streaming.user_free_access') or []))
            planner['streaming'] = s
        elif section == sc.SCORING:
            from migratarr.planner_settings import DEFAULT_SCORING
            weights = {k: values.get('scoring.weights.' + k) for k in WEIGHTS}
            for k, w in weights.items():
                if w is None:
                    problems.append(sc.Problem('scoring.weights.' + k, 'Enter a weight.'))
            scoring = dict(_dict(planner.get('scoring')))
            if weights == DEFAULT_SCORING['weights'] and 'weights' not in scoring:
                pass  # defaults: leave the file as it is
            else:
                scoring['weights'] = dict(_dict(scoring.get('weights')), **weights)
                planner['scoring'] = scoring
            total = sum(w for w in weights.values() if isinstance(w, (int, float)))
            if abs(total - 1) > 1e-6:
                problems.append(sc.Problem(sc.SCORING, f'The weights add up to {total:g}; they are usually '
                                                       'chosen to add up to 1.', sc.WARN))
        else:
            from migratarr.planner_settings import DEFAULT_OVERRIDES
            o = dict(_dict(planner.get('overrides')) or copy.deepcopy(DEFAULT_OVERRIDES))
            o['lock_tag'] = values.get('overrides.lock_tag') or ''
            o['category_tags'] = {values['overrides.category_tags.' + c]: c for c in OVERRIDE_CATEGORIES
                                  if not _blank(values.get('overrides.category_tags.' + c))}
            if o == DEFAULT_OVERRIDES and 'overrides' not in planner:
                pass
            else:
                planner['overrides'] = o
    return docs, pasted, problems


def problem_path(p):
    """Where on the forms an A-side Problem(file, field, message) belongs: (path, message)."""
    field, msg = p.field, p.message
    head = field.split('.', 1)[0].split('[', 1)[0].split(' ', 1)[0]
    rest = field.split('.', 1)[1] if '.' in field else ''
    if p.file == RUNTIME:
        if head == 'urls':
            for d in services.SERVICES.values():
                if d.url_setting == field:
                    return 'services.' + d.id + '.url', msg
            return sc.SERVICES, f'{field}: {msg}'
        if head == 'secrets' and rest.split('.', 1)[0] in services.SERVICES:
            return 'services.' + rest.split('.', 1)[0] + '.source', msg
        if head == 'containers':
            for d in services.SERVICES.values():
                if d.container_setting == field:
                    return 'services.' + d.id + '.container', msg
            return sc.SERVICES, f'{field}: {msg}'
        if head in ('secrets', 'arr_file_checks'):
            return sc.SERVICES, f'{field}: {msg}'
        if head == 'nas' and not rest:
            named = re.search(r'\bnas\.(host|user|ssh_key|python)\b', msg)
            if named:
                return 'topology.nas.' + named.group(1), msg
        if head == 'nas':
            return ('topology.nas.' + rest if rest in ('host', 'user', 'ssh_key', 'python') else sc.TOPOLOGY), \
                   (msg if rest else f'NAS: {msg}')
        if head == 'base_path':
            return 'topology.base_path', msg
        if head == 'topology':
            return 'topology.mode', msg
        if head in ('storage', 'storage_targets'):
            return sc.DISKS, f'{field}: {msg}'
        return sc.TOPOLOGY, f'runtime.json {field}: {msg}'
    if p.file == TARGETS:
        if head == 'targets' and '(' in field:
            return 'disks.' + field.split('(', 1)[1].rstrip(')'), f'Disk {field.split("(", 1)[1].rstrip(")")}: {msg}'
        if head == 'arr_root':
            return 'disks._arr_root', msg
        if head == 'defaults':
            return 'disks._defaults.min_free_gb', msg
        return sc.DISKS, f'{field}: {msg}'
    if field in ('streaming.region', 'streaming.subscribed', 'streaming.user_free_access'):
        return field, msg
    if head in ('overrides', 'scoring'):
        return head, f'{field}: {msg}'
    return sc.STREAMING, f'planner.json {field}: {msg}'


def translate(problems):
    out = []
    for p in problems:
        path, message = problem_path(p)
        out.append(sc.Problem(path, message, SEVERITY.get(p.severity, sc.FAIL)))
    return out


def in_section(problem, section):
    return problem.path == section or problem.path.startswith(section + '.')


# ---------------------------------------------------------------- the backend

class UiBackend:
    """settings_contract.SettingsBackend and HealthBackend over migratarr.settings and migratarr.health."""

    def __init__(self, environ=None, *, base=None, http=health.http_get, run=subprocess.run,
                 docker_cat=None, read_mounts=None, health_fs=None, docker=subprocess.run):
        self.environ = os.environ if environ is None else environ
        self.container = in_container(self.environ)
        self.base = base
        self.http, self.run, self.docker, self.health_fs = http, run, docker, health_fs or {}
        self.docker_cat, self.read_mounts = docker_cat, read_mounts

    # ------------------------------------------------------------ reading
    def report(self):
        return store.load(environ=self.environ)

    def base_of(self, report):
        if report.runtime is not None:
            return report.runtime.base_path
        data = _dict(report.data.get(RUNTIME))
        return Path(data['base_path']) if isinstance(data.get('base_path'), str) else Path(self.base or default_base())

    @staticmethod
    def revision_of(report):
        return hashlib.sha256(json.dumps(report.digests, sort_keys=True).encode()).hexdigest()[:16]

    def docs(self, report, draft=None):
        """The documents to edit: the files, or a fresh install's templates (plus the wizard's draft)."""
        fresh = templates(str(self.base) if self.base else None, container=self.container)
        docs = {name: copy.deepcopy(report.data.get(name)) if isinstance(report.data.get(name), dict)
                else fresh[name] for name in store.FILES}
        for section in sc.SECTIONS:
            if draft and section in draft:
                docs = write_section(section, docs, draft[section], self.environ)[0]
        return docs

    def status(self):
        report = self.report()
        problems = [] if report.missing else translate(report.problems)
        return sc.Status(configured=not report.missing, revision=self.revision_of(report),
                         job_running=store.busy(self.base_of(report)) is not None, problems=tuple(problems))

    def capabilities(self):
        return sc.Capabilities(topologies=('nas_ssh', 'local'), experimental=('local',),
                               secret_file=str(secrets_folder_path(self.environ)) + '/<service>')

    def sections(self):
        names = ', '.join(d.label for d in services.SERVICES.values())
        return [sc.SectionSpec(sc.SERVICES, 'Services', names + ': addresses and where their keys come from'),
                sc.SectionSpec(sc.TOPOLOGY, 'Where moves run', 'NAS over SSH or this machine; data folder'),
                sc.SectionSpec(sc.DISKS, 'Disks and categories', 'Disks, reserves, category folders, placement'),
                sc.SectionSpec(sc.STREAMING, 'Streaming', 'Region and the services you have'),
                sc.SectionSpec(sc.SCORING, 'Scoring', 'How replacement, streaming and use are weighed'),
                sc.SectionSpec(sc.OVERRIDES, 'Override tags', 'Radarr/Sonarr tags that lock or pin items')]

    def fields(self, section, values):
        if section == sc.SERVICES:
            install = values.get(ARR_INSTALL) or ('host' if self.container else 'docker')
            return [arr_install_field(install, self.container)] + [
                f for d in services.SERVICES.values() for f in service_fields(d, install, self.container)]
        if section == sc.TOPOLOGY:
            configured = not self.report().missing
            return [sc.Field('topology.mode', 'Moves run', sc.CHOICE,
                             'Changing this later means re-entering every disk; set up again to switch.' if configured
                             else "Local disks are experimental: not yet run end to end against real Radarr/Sonarr.",
                             True, TOPOLOGY_CHOICES, read_only=configured),
                    sc.Field('topology.base_path', 'Migratarr data folder', sc.PATH,
                             'Runs, manifests, approvals and logs are kept here.', True),
                    # Required for NAS over SSH only; checked on the server, since a browser "required" would
                    # also block choosing local disks.
                    sc.Field('topology.nas.host', 'NAS host', sc.TEXT, 'Required for NAS over SSH. The NAS\'s IP '
                             'address or host name.', group='NAS'),
                    sc.Field('topology.nas.user', 'NAS user', sc.TEXT, 'Required for NAS over SSH. The account '
                             'moves run as on the NAS.', group='NAS'),
                    sc.Field('topology.nas.ssh_key', 'SSH key', sc.PATH, 'Required for NAS over SSH. Full path to a '
                             'private key used only by Migratarr, e.g. /home/you/.ssh/migratarr_nas.', group='NAS'),
                    sc.Field('topology.nas.python', 'Python on the NAS', sc.PATH, 'Required for NAS over SSH, e.g. '
                             '/usr/bin/python3.', group='NAS')]
        if section == sc.DISKS:
            mode = values.get('disks._topology') or self.topology_of(self.docs(self.report()))
            return disk_fields(values, mode != 'local')
        if section == sc.STREAMING:
            return [sc.Field('streaming.region', 'Region', sc.TEXT, 'Two uppercase letters, e.g. US, GB, CA.', True),
                    sc.Field('streaming.subscribed', 'Services you pay for', sc.LINES,
                             'One per line, e.g. Hulu. Titles streaming on these score as easy to watch.'),
                    sc.Field('streaming.user_free_access', 'Free to you', sc.LINES,
                             'Services TMDB lists as "free" that you really have (a library card service). '
                             'Leave empty unless sure.')]
        if section == sc.SCORING:
            return [sc.Field('scoring.weights.' + k, k.capitalize(), sc.NUMBER, group='Weights', required=True)
                    for k in WEIGHTS]
        if section == sc.OVERRIDES:
            return [sc.Field('overrides.lock_tag', 'Lock tag', sc.TEXT, 'Items with this tag never move.', True)] + [
                sc.Field('overrides.category_tags.' + c, c, sc.TEXT, group='Tag that pins an item to a category')
                for c in OVERRIDE_CATEGORIES]
        raise sc.SettingsError('There is no settings section called ' + str(section) + '.')

    def current(self, section):
        values = read_section(section, self.docs(self.report()), self.environ)
        if section == sc.SERVICES and self.container:
            values = container_services(values)
        if section == sc.DISKS:
            values['disks._topology'] = self.topology_of(self.docs(self.report()))
        return values

    def defaults(self, section):
        report = self.report()
        values = read_section(section, self.docs(report), self.environ)
        if section == sc.SERVICES and self.container:
            values = container_services(values)
        if section == sc.DISKS:
            values['disks._topology'] = self.topology_of(self.docs(report))
        return values

    @staticmethod
    def topology_of(docs):
        return _dict(docs.get(RUNTIME)).get('topology') or 'nas_ssh'

    # ------------------------------------------------------------ checking
    def normalize(self, section, values, draft=None):
        """Called by the pages after parsing a form: "Add a disk" and "Remove" become real changes, and
        the Services form follows how Radarr and Sonarr are installed."""
        if section == sc.SERVICES:
            return normalize_services(container_services(values) if self.container else values)
        if section != sc.DISKS:
            return values, []
        if draft and sc.TOPOLOGY in draft:
            values = dict(values, **{'disks._topology': draft[sc.TOPOLOGY].get('topology.mode') or 'nas_ssh'})
        return normalize_disks(values)

    def build(self, section, values, draft=None):
        report = self.report()
        docs = self.docs(report, draft)
        values = {k: v for k, v in values.items() if k != 'disks._topology'}
        docs, pasted, problems = write_section(section, docs, values, self.environ)
        return report, docs, pasted, problems

    def validate(self, section, values, draft=None):
        report, docs, _, problems = self.build(section, values, draft)
        found = store.check(docs, self.environ)[0]
        # Only this section's problems: the others are shown (and fixed) in their own section or step,
        # and a fresh install's runtime.json can't be complete before the wizard's later steps.
        mine = {p.path for p in problems}
        return problems + [p for p in translate(found) if in_section(p, section) and p.path not in mine]

    def intro(self, section):
        """The wizard's text above a step, where the container changes it (None: the usual text)."""
        if self.container and section == sc.SERVICES:
            return ('Radarr, Sonarr, Jellyfin and TMDB. Migratarr runs in a container: give each service\'s '
                    'address as the other containers see it (for example http://radarr:7878) and paste its key. '
                    'Your media must be mounted at the same path in Migratarr, Radarr and Sonarr.')
        return None

    def can_discover(self, section):
        """Whether "Detect" has anything to find on this step."""
        return section in DISCOVERS

    def diff(self, section, values):
        before = self.current(section)
        labels = {f.path: f for f in self.fields(section, before) + self.fields(section, values)}
        changes = []
        for path in list(before) + [p for p in values if p not in before]:
            if path.endswith(('.remove', '._topology')) or path.startswith('disks._new.'):
                continue
            f = labels.get(path)
            old, new = before.get(path), values.get(path)
            label = (f.group + ': ' if f and f.group else '') + (f.label if f else path)
            if f and f.kind == sc.SECRET:
                if new not in (sc.SECRET_KEPT, None, ''):
                    changes.append(sc.Change(path, label, old, sc.SECRET_NEW, True))
                elif new is None and old is sc.SECRET_KEPT and values.get(path.rsplit('.', 1)[0] + '.source') != PASTE:
                    changes.append(sc.Change(path, label, old, None, True))
            elif (None if _blank(old) else old) != (None if _blank(new) else new):
                changes.append(sc.Change(path, label, old, new))
        return changes

    # ------------------------------------------------------------ writing
    def write(self, report, docs, pasted, files, base=None):
        """Check, store pasted keys, then save the given files through migratarr.settings.save."""
        changes = {name: docs[name] for name in files}
        try:
            if store.busy(base or self.base_of(report)):
                raise store.SettingsBusy('A job is running. Save after it finishes.')
            store.save(changes, report=report, base=base, environ=self.environ, dry_run=True)
            for service, key in pasted.items():
                entry = keys.store_key(service, key, environ=self.environ)
                if Path(entry['path']).expanduser() != secrets_folder_path(self.environ) / service:
                    raise keys.KeyStoreError('the key was stored somewhere unexpected; nothing else was saved')
            result = store.save(changes, report=report, base=base, environ=self.environ)
        except store.SettingsInvalid as exc:
            shown = [f'{m}' for m in (problem_path(p)[1] for p in exc.problems[:3])]
            raise sc.SaveRefused('Nothing was saved: ' + '; '.join(shown)) from None
        except store.SettingsConflict:
            raise sc.SaveRefused('The settings files changed since you reviewed this. Review the change again.') \
                from None
        except store.SettingsBusy as exc:
            raise sc.SaveRefused(str(exc)) from None
        except keys.KeyStoreError as exc:  # messages never contain the key
            raise sc.SaveRefused(str(exc)) from None
        return sc.SaveResult(tuple(result.written), tuple(result.backed_up) + ('',) * (len(result.written)
                                                                                       - len(result.backed_up)))

    def save(self, section, values, revision):
        report, docs, pasted, problems = self.build(section, values)
        if report.missing:
            raise sc.SaveRefused('Migratarr is not set up yet; finish setup first.')
        name = SECTION_FILE[section]
        if report.digests.get(name) and report.data.get(name) is None:
            raise sc.SaveRefused(f'{report.files.path(name)} is not readable JSON, so it can\'t be edited here. '
                                 'Fix or replace it by hand; see the problem shown on Settings & health.')
        if self.revision_of(report) != revision:
            raise sc.SaveRefused('The settings files changed since you reviewed this. Review the change again.')
        if sc.blocking(problems):
            raise sc.SaveRefused('Nothing was saved: ' + '; '.join(p.message for p in sc.blocking(problems)[:3]))
        return self.write(report, docs, pasted, (SECTION_FILE[section],))

    def create(self, draft):
        report = self.report()
        if not report.missing:
            raise sc.SaveRefused('Migratarr is already set up. Change settings from the Settings page.')
        docs, pasted, problems = self.docs(report), {}, []
        for section in (sc.TOPOLOGY, sc.SERVICES, sc.DISKS, sc.STREAMING):
            if section in draft:
                values = {k: v for k, v in draft[section].items() if k != 'disks._topology'}
                docs, found, more = write_section(section, docs, values, self.environ)
                pasted.update(found)
                problems += more
        if sc.blocking(problems):
            raise sc.SaveRefused('Nothing was saved: ' + '; '.join(p.message for p in sc.blocking(problems)[:3]))
        base = Path(str(docs[RUNTIME].get('base_path') or self.base or default_base())).expanduser()
        base.mkdir(parents=True, exist_ok=True)
        return self.write(report, docs, pasted, store.FILES, base=base)

    # ------------------------------------------------------------ health
    def run_checks(self, draft=None):
        """migratarr.health's checks (the ones `migratarr doctor` runs), as settings_contract results.

        With a draft, the checks run on the unsaved settings: they are built with store.check() and
        handed to health.run_checks. A key pasted into the draft isn't stored yet, so that service is
        reported as skipped, not failed.
        """
        if draft is None:
            report = self.report()
            pasted = set()
        else:
            base = self.report()
            docs, pasted = self.docs(base, None), set()
            for section in (sc.TOPOLOGY, sc.SERVICES, sc.DISKS, sc.STREAMING):
                if section in draft:
                    values = {k: v for k, v in draft[section].items() if k != 'disks._topology'}
                    docs, found, _ = write_section(section, docs, values, self.environ)
                    pasted |= set(found)
            problems, runtime, targets, planner = store.check(docs, self.environ)
            report = store.Report(base.files, docs, {n: 'draft' for n in store.FILES}, problems, runtime,
                                  targets, planner)
        checks = health.run_checks(report, environ=self.environ, http=self.http, run=self.run, docker=self.docker,
                                   **self.health_fs)
        out = []
        for c in checks:
            service = c.id.split('.', 1)[1] if c.id.startswith(('service.', 'arr_roots.')) else None
            if service in pasted:
                out.append(sc.CheckResult(c.id, c.label, sc.SKIPPED, 'Checked after you save: the pasted key '
                                          'is not stored yet.', ('services.' + service + '.key',)))
                continue
            out.append(sc.CheckResult(c.id, c.label, CHECK_STATUS.get(c.status, c.status), c.detail,
                                      check_paths(c), c.fix))
        return out

    # ------------------------------------------------------------ discovery
    def discover(self, section, draft):
        """Suggestions for empty fields. Reads config.xml for whether a key exists and its URL base;
        never returns, logs or keeps the key."""
        if section == sc.SERVICES:
            return self.discover_services(draft)
        if section == sc.DISKS:
            return self.discover_disks(draft)
        return sc.Discovery({}, ())

    def discover_services(self, draft):
        from migratarr.service_keys import SecretError, _docker_cat, _from_xml, docker_failure
        cat = self.docker_cat or _docker_cat
        values, notes = {}, []
        mine = dict(draft.get(sc.SERVICES, {}))
        install = mine.get(ARR_INSTALL) or ('host' if self.container else 'docker')
        for d in services.SERVICES.values():
            p = 'services.' + d.id
            if self.container and d.id != 'tmdb':
                notes.append(f'{d.label}: paste its API key' + (' (Settings > General > API Key)'
                                                                 if d.url_base_from_config else '')
                             + '. From inside its container Migratarr can\'t read other containers\' files.')
            elif on_host(d, install):
                found, url_base, where = self.discover_host_config(d, mine.get(p + '.paths') or [])
                if found:
                    values.update({p + '.source': 'config_xml', p + '.paths': [where]})
                    notes.append(f'{d.label}: {where} has an API key'
                                 + (f' (URL base {url_base})' if url_base else '') + '; it will be read from there.')
                else:
                    notes.append(f'{d.label}: no readable config.xml with an API key found'
                                 + (f' at {where}' if where else ' in the usual places') + '. Give the path '
                                 'to its config.xml, or paste the key.')
            elif d.url_base_from_config:
                container = mine.get(p + '.container') or d.container_setting.split('.', 1)[1]
                found, why = None, None
                try:
                    key, url_base = _from_xml(cat(container, '/config/config.xml'), d.id,
                                              {'source': 'docker_config_xml', 'container': container,
                                               'path': '/config/config.xml'})
                    found = bool(key)
                    key = None  # never kept
                except (OSError, subprocess.SubprocessError, SecretError, ValueError) as exc:
                    url_base, why = None, docker_failure(exc, container)
                if found:
                    values.update({p + '.source': 'docker_config_xml', p + '.container': container})
                    notes.append(f'{d.label}: container "{container}" has a config.xml with an API key'
                                 + (f' (URL base {url_base})' if url_base else '') + '; it will be read from there.')
                elif why and 'container' not in why:  # Docker itself: not installed or not reachable
                    notes.append(f'{d.label}: {why}. If Radarr and Sonarr are installed directly on this machine, '
                                 'choose that above; otherwise paste the key.')
                else:
                    notes.append(f'{d.label}: no config.xml found in a container called "{container}". '
                                 'Set the container name, or paste the key.')
            elif d.id == 'tmdb':
                variable = services.default_sources()['tmdb'].get('variable', 'TMDB_TOKEN')
                if self.environ.get(variable):
                    values.update({p + '.source': 'env', p + '.variable': variable})
                    notes.append(f'TMDB: {variable} is set for this process; it will be read from there.')
                else:
                    notes.append('TMDB: no token found in the environment. Paste your API Read Access Token.')
            if d.url_setting and _blank(mine.get(p + '.url')):
                values[p + '.url'] = d.default_url
        return sc.Discovery(values, tuple(notes))

    def discover_host_config(self, d, paths):
        """(found, url_base, path) for an Arr installed on this machine: the config.xml path given, else the
        first of its usual places that has a key. Reads the key only to see that there is one."""
        from migratarr.service_keys import SecretError, _from_xml
        home = Path(self.environ.get('HOME') or '~').expanduser()
        candidates = list(paths[:1]) or [str(Path(x.format(home=home))) for x in HOST_CONFIG_XML[d.id]]
        for where in candidates:
            try:
                key, url_base = _from_xml(Path(where).read_bytes(), d.id, {'source': 'config_xml', 'path': where})
            except (OSError, SecretError, ValueError):
                continue
            if key:
                key = None  # never kept
                return True, url_base, where
        return False, None, paths[0] if paths else None

    def discover_disks(self, draft):
        values, notes = {}, []
        mine = dict(draft.get(sc.DISKS, {}))
        known = {mine.get(f'disks.{d}.path') for d in disk_ids(mine)}
        mounts = (self.read_mounts or read_mounts)()
        empty = [d for d in disk_ids(mine) if _blank(mine.get(f'disks.{d}.path'))]
        fresh = [m for m in mounts if m not in known]
        for d, mount in zip(empty, fresh):
            values[f'disks.{d}.path'] = mount
        if fresh:
            notes.append('Mounted disks on this machine: ' + ', '.join(fresh[:8])
                         + (' and more' if len(fresh) > 8 else '') + '. Add any that hold media.')
        else:
            notes.append('No mounted media disks found under /mnt, /media or /srv.')
        try:
            checks = self.run_checks(draft) if draft else []
        except Exception:  # discovery is best effort
            checks = []
        for c in checks:
            if c.id.startswith('arr_roots.') and c.status in (sc.OK, sc.FAIL) and c.detail:
                notes.append(f'{c.label}: {c.detail}')
        return sc.Discovery(values, tuple(notes))


def check_paths(check):
    if check.id.startswith('service.'):
        return ('services.' + check.id.split('.', 1)[1] + '.url',)
    if check.id.startswith('arr_files.'):
        return (ARR_INSTALL,)
    if check.id.startswith('disk.'):
        return ('disks.' + check.id.split('.', 1)[1] + '.path',)
    if check.id.startswith('nas.disk.'):
        return ('disks.' + check.id.split('.', 2)[2] + '.remote_path',)
    if check.id.startswith('nas'):
        return ('topology.nas.host',)
    return ()


IGNORED_FS = {'proc', 'sysfs', 'tmpfs', 'devtmpfs', 'devpts', 'cgroup', 'cgroup2', 'overlay', 'squashfs',
              'autofs', 'mqueue', 'debugfs', 'tracefs', 'securityfs', 'pstore', 'bpf', 'configfs', 'fusectl',
              'hugetlbfs', 'binfmt_misc', 'nsfs', 'ramfs', 'efivarfs'}


def read_mounts(path='/proc/mounts'):
    """Mount points that could hold media: under /mnt, /media or /srv, not a system filesystem."""
    found = []
    try:
        lines = Path(path).read_text().splitlines()
    except OSError:
        return found
    for line in lines:
        parts = line.split()
        if len(parts) < 3 or parts[2] in IGNORED_FS:
            continue
        point = parts[1].replace('\\040', ' ')
        if PurePosixPath(point).parts[1:2] in (('mnt',), ('media',), ('srv',)) and point not in found \
                and point not in ('/mnt', '/media', '/srv'):
            found.append(point)
    return found
