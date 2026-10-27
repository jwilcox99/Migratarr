"""Declared storage targets; no runtime callers or filesystem probes yet."""
from dataclasses import dataclass
import json
from pathlib import Path, PurePosixPath
import re


def _require(ok, message):
    if not ok:
        raise ValueError('Storage targets: ' + message)


def _fields(value, required, optional=()):
    _require(isinstance(value, dict), 'expected an object')
    _require(set(required) <= set(value) <= set(required) | set(optional),
             'missing or unknown fields')


def _text(value):
    return isinstance(value, str) and bool(value.strip()) and not any(ord(c) < 32 for c in value)


def _path(value, absolute=True):
    _require(_text(value), 'invalid path')
    p = PurePosixPath(value)
    _require(p.is_absolute() == absolute and str(p) == value
             and '..' not in p.parts and value not in {'/', '.'}
             and '\\' not in value and not value.startswith('//'), 'noncanonical path')
    return p


def _reserve(value):
    _require(type(value) is int and value >= 0, 'reserve must be a nonnegative integer')
    return value


@dataclass(frozen=True)
class StorageTarget:
    id: str
    name: str
    path: PurePosixPath
    remote_path: PurePosixPath | None
    enabled: bool
    media_types: tuple[str, ...]
    minimum_free_space_gb: int
    storage_class: str | None
    priority: int
    tags: tuple[str, ...]


@dataclass(frozen=True)
class TargetState:
    """Observed bytes, supplied by a future planner probe, never configuration."""
    target_id: str
    capacity: int
    free_space: int

    def __post_init__(self):
        _require(isinstance(self.target_id, str) and
                 re.fullmatch(r'[a-z][a-z0-9_-]{0,31}', self.target_id), 'invalid state target ID')
        _require(type(self.capacity) is int and type(self.free_space) is int
                 and 0 <= self.free_space <= self.capacity, 'invalid observed space')


@dataclass
class StorageTargets:
    targets: tuple[StorageTarget, ...]
    category_paths: dict
    placement: dict
    # Radarr/Sonarr-visible root: an item is <arr_root>/<category path>/<name>.
    arr_root: PurePosixPath = PurePosixPath('/media')

    @property
    def source_roots(self):
        # Disabled destinations still resolve as sources.
        return tuple(t.path for t in self.targets)

    @property
    def destination_roots(self):
        by_id = {t.id: t for t in self.targets}
        return {media: {category: tuple(by_id[i].path / self.category_paths[media][category]
                                       for i in ids)
                        for category, ids in categories.items()}
                for media, categories in self.placement.items()}

    def physical_target(self, value):
        path = _path(value)
        return next((t for t in sorted(self.targets, key=lambda t: len(t.path.parts), reverse=True)
                     if path == t.path or t.path in path.parents), None)


def _parse_target(raw, reserve, earlier):
    _fields(raw, {'id', 'name', 'path', 'enabled', 'media_types'},
            {'remote_path', 'minimum_free_space_gb', 'storage_class', 'priority', 'tags'})
    ident = raw['id']
    _require(isinstance(ident, str) and re.fullmatch(r'[a-z][a-z0-9_-]{0,31}', ident), 'invalid ID')
    _require(ident not in {t.id for t in earlier}, 'duplicate ID')
    _require(_text(raw['name']), 'invalid name')
    _require(type(raw['enabled']) is bool, 'enabled must be boolean')
    media = raw['media_types']
    _require(isinstance(media, list) and all(isinstance(m, str) for m in media), 'invalid media_types')
    _require(set(media) <= {'Movie', 'TV'} and len(media) == len(set(media)), 'invalid media_types')
    storage_class = raw.get('storage_class')
    _require(storage_class is None or (isinstance(storage_class, str) and
             storage_class in {'HOT', 'PRIMARY', 'BULK', 'PROTECTED', 'ARCHIVE'}), 'invalid storage_class')
    priority = raw.get('priority', 100)
    _require(type(priority) is int, 'priority must be an integer')
    tags = raw.get('tags', [])
    _require(isinstance(tags, list) and all(_text(t) for t in tags), 'invalid tags')
    _require(len(tags) == len(set(tags)), 'duplicate tags')
    return StorageTarget(ident, raw['name'], _path(raw['path']),
                         _path(raw['remote_path']) if 'remote_path' in raw else None,
                         raw['enabled'], tuple(media), _reserve(raw.get('minimum_free_space_gb', reserve)),
                         storage_class, priority, tuple(tags))


def _check_roots(targets):
    for i, target in enumerate(targets):
        for other in targets[i + 1:]:
            _require(target.path != other.path and target.path not in other.path.parents
                     and other.path not in target.path.parents, 'overlapping target roots')
    remotes = [t.remote_path for t in targets if t.remote_path is not None]
    _require(len(remotes) == len(set(remotes)), 'duplicate remote roots')
    # Any depth; executors find an item's disk by the one root containing it.
    _require(not any(a in b.parents or b in a.parents for i, a in enumerate(remotes) for b in remotes[i + 1:]),
             'overlapping remote roots')


def _parse_category_paths(raw_paths):
    _require(isinstance(raw_paths, dict) and raw_paths, 'categories must be nonempty')
    paths = {}
    for category, value in raw_paths.items():
        _require(_text(category) and category not in {'.', '..', 'HOLD'}
                 and '/' not in category and '\\' not in category, 'invalid category')
        paths[category] = _path(value, absolute=False)
    _require(len(set(paths.values())) == len(paths), 'duplicate category directories')
    return paths


def _parse_placement(raw_placement, media, paths, by_id):
    _require(isinstance(raw_placement, dict) and raw_placement, 'placement must be nonempty')
    placement = {}
    for category, ids in raw_placement.items():
        _require(category in paths, 'missing category directory')
        _require(isinstance(ids, list) and ids and all(isinstance(i, str) for i in ids), 'invalid placement')
        _require(len(ids) == len(set(ids)), 'duplicate placement IDs')
        _require(all(i in by_id and by_id[i].enabled and media in by_id[i].media_types
                     for i in ids), 'unknown, disabled or incompatible destination')
        placement[category] = tuple(ids)
    return placement


def parse_targets(data):
    _fields(data, {'schema_version', 'defaults', 'targets', 'category_paths', 'placement'}, {'arr_root'})
    _require(type(data['schema_version']) is int and data['schema_version'] == 1,
             'unsupported schema_version')
    _fields(data['defaults'], {'minimum_free_space_gb'})
    reserve = _reserve(data['defaults']['minimum_free_space_gb'])
    _require(isinstance(data['targets'], list) and data['targets'], 'targets must be nonempty')
    targets = []
    for raw in data['targets']:
        targets.append(_parse_target(raw, reserve, targets))
    _check_roots(targets)
    paths, placement = {}, {}
    _fields(data['category_paths'], {'Movie', 'TV'})
    _fields(data['placement'], {'Movie', 'TV'})
    by_id = {t.id: t for t in targets}
    for media in ('Movie', 'TV'):
        paths[media] = _parse_category_paths(data['category_paths'][media])
        placement[media] = _parse_placement(data['placement'][media], media, paths[media], by_id)
    arr_root = _path(data.get('arr_root', '/media'))
    return StorageTargets(tuple(targets), paths, placement, arr_root)


def target_problems(data):
    """Every problem in a storage-targets.json document as (field, message).

    parse_targets stops at the first problem; this checks each part on its own,
    so the settings page and `doctor` can list them all. A part that depends on
    a broken one (placement on a broken target) is checked against what parsed.
    """
    found = []

    def attempt(field, check):
        try:
            return check()
        except ValueError as exc:
            found.append((field, str(exc)))
        except (KeyError, TypeError, AttributeError):
            found.append((field, 'Storage targets: ' + field + ' is missing or malformed'))
        return None

    if not isinstance(data, dict):
        return [('root', 'Storage targets: expected an object')]
    attempt('root', lambda: _fields(data, {'schema_version', 'defaults', 'targets', 'category_paths',
                                           'placement'}, {'arr_root'}))
    attempt('schema_version', lambda: _require(type(data.get('schema_version')) is int
                                              and data['schema_version'] == 1, 'unsupported schema_version'))
    reserve = attempt('defaults', lambda: (_fields(data['defaults'], {'minimum_free_space_gb'}),
                                           _reserve(data['defaults']['minimum_free_space_gb']))[1])
    reserve = 0 if reserve is None else reserve
    targets, broken = [], set()
    raw_targets = data.get('targets')
    if attempt('targets', lambda: _require(isinstance(raw_targets, list) and raw_targets,
                                           'targets must be nonempty')) is None and isinstance(raw_targets, list):
        for index, raw in enumerate(raw_targets):
            label = 'targets[%d]' % index
            if isinstance(raw, dict) and isinstance(raw.get('id'), str):
                label += ' (' + raw['id'] + ')'
            parsed = attempt(label, lambda: _parse_target(raw, reserve, targets))
            if parsed is not None:
                targets.append(parsed)
            elif isinstance(raw, dict) and isinstance(raw.get('id'), str):
                broken.add(raw['id'])
        attempt('targets', lambda: _check_roots(targets))
    by_id = {t.id: t for t in targets}
    for media in ('Movie', 'TV'):
        paths = attempt('category_paths.' + media, lambda: _parse_category_paths(data['category_paths'][media]))
        raw_placement = data.get('placement', {}).get(media) if isinstance(data.get('placement'), dict) else None
        refers_to_broken = isinstance(raw_placement, dict) and any(
            isinstance(ids, list) and broken.intersection(i for i in ids if isinstance(i, str))
            for ids in raw_placement.values())
        if paths is not None and not refers_to_broken:
            attempt('placement.' + media,
                    lambda: _parse_placement(data['placement'][media], media, paths, by_id))
    if 'arr_root' in data:
        attempt('arr_root', lambda: _path(data['arr_root']))
    return found

def _unique(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, 'duplicate JSON field: ' + key)
        result[key] = value
    return result


def load_targets(filename):
    """Explicit opt-in file load; no implicit production migration or fallback."""
    return parse_targets(json.loads(Path(filename).read_text(encoding='utf-8'), object_pairs_hook=_unique))


def deployed_targets_path(runtime, environ=None):
    """The storage-targets.json a deployment uses.

    $MIGRATARR_STORAGE_TARGETS, else (runtime.json version 2) the file
    runtime_config.load_config read the disks from, else (version 1)
    <base_path>/config/storage-targets.json, as before.
    """
    import os
    env = os.environ if environ is None else environ
    if env.get('MIGRATARR_STORAGE_TARGETS'):
        return Path(env['MIGRATARR_STORAGE_TARGETS'])
    if getattr(runtime, 'targets_path', None) is not None:
        return Path(runtime.targets_path)
    return Path(runtime.base_path) / 'config' / 'storage-targets.json'


def check_runtime_consistency(runtime, targets):
    """Fail loud if runtime.json's storage block disagrees with storage-targets.json.

    The two files are still independently maintained; Phase One executors read
    disk roots from runtime_config.py, not this module, and this does not make
    either file derive from the other. It only turns silent drift between them
    into an explicit refusal, the same equivalence tests already pin between
    the two example configs.
    """
    by_id = {t.id: t for t in targets.targets}
    for disk, entry in runtime.storage.items():
        _require(disk in by_id, 'runtime storage disk missing from storage targets: ' + disk)
        target = by_id[disk]
        _require(str(target.path) == entry['local_path'],
                 disk + ' local_path disagrees between runtime and storage targets')
        _require(target.remote_path is not None and str(target.remote_path) == entry['remote_path'],
                 disk + ' remote_path disagrees between runtime and storage targets')
