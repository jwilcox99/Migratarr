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


def parse_targets(data):
    _fields(data, {'schema_version', 'defaults', 'targets', 'category_paths', 'placement'})
    _require(type(data['schema_version']) is int and data['schema_version'] == 1,
             'unsupported schema_version')
    _fields(data['defaults'], {'minimum_free_space_gb'})
    reserve = _reserve(data['defaults']['minimum_free_space_gb'])
    _require(isinstance(data['targets'], list) and data['targets'], 'targets must be nonempty')
    targets = []
    for raw in data['targets']:
        _fields(raw, {'id', 'name', 'path', 'enabled', 'media_types'},
                {'remote_path', 'minimum_free_space_gb', 'storage_class', 'priority', 'tags'})
        ident = raw['id']
        _require(isinstance(ident, str) and re.fullmatch(r'[a-z][a-z0-9_-]{0,31}', ident), 'invalid ID')
        _require(ident not in {t.id for t in targets}, 'duplicate ID')
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
        targets.append(StorageTarget(ident, raw['name'], _path(raw['path']),
                       _path(raw['remote_path']) if 'remote_path' in raw else None,
                       raw['enabled'], tuple(media), _reserve(raw.get('minimum_free_space_gb', reserve)),
                       storage_class, priority, tuple(tags)))
    for i, target in enumerate(targets):
        for other in targets[i + 1:]:
            _require(target.path != other.path and target.path not in other.path.parents
                     and other.path not in target.path.parents, 'overlapping target roots')
    remotes = [t.remote_path for t in targets if t.remote_path is not None]
    _require(len(remotes) == len(set(remotes)), 'duplicate remote roots')
    if remotes:
        # phase1-fixed-depth applies to the entire mixed configuration.
        _require(all(len(t.path.parts) == 4 and t.path.name == t.id for t in targets),
                 'phase1-fixed-depth local root must be /a/b/<id>')
        _require(len({t.path.parent for t in targets}) == 1, 'local roots must share a parent')
        _require(all(len(p.parts) == 3 for p in remotes), 'remote roots must have two components')
    paths, placement = {}, {}
    _fields(data['category_paths'], {'Movie', 'TV'})
    _fields(data['placement'], {'Movie', 'TV'})
    by_id = {t.id: t for t in targets}
    for media in ('Movie', 'TV'):
        raw_paths, raw_placement = data['category_paths'][media], data['placement'][media]
        _require(isinstance(raw_paths, dict) and raw_paths, 'categories must be nonempty')
        paths[media] = {}
        for category, value in raw_paths.items():
            _require(_text(category) and category not in {'.', '..', 'HOLD'}
                     and '/' not in category and '\\' not in category, 'invalid category')
            paths[media][category] = _path(value, absolute=False)
        _require(len(set(paths[media].values())) == len(paths[media]), 'duplicate category directories')
        _require(isinstance(raw_placement, dict) and raw_placement, 'placement must be nonempty')
        placement[media] = {}
        for category, ids in raw_placement.items():
            _require(category in paths[media], 'missing category directory')
            _require(isinstance(ids, list) and ids and all(isinstance(i, str) for i in ids), 'invalid placement')
            _require(len(ids) == len(set(ids)), 'duplicate placement IDs')
            _require(all(i in by_id and by_id[i].enabled and media in by_id[i].media_types
                         for i in ids), 'unknown, disabled or incompatible destination')
            placement[media][category] = tuple(ids)
    return StorageTargets(tuple(targets), paths, placement)


def _unique(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, 'duplicate JSON field: ' + key)
        result[key] = value
    return result


def load_targets(filename):
    """Explicit opt-in file load; no implicit production migration or fallback."""
    return parse_targets(json.loads(Path(filename).read_text(encoding='utf-8'), object_pairs_hook=_unique))


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
