"""Validated planner preferences: streaming access and Arr override tag names."""
from dataclasses import dataclass
from functools import lru_cache
import json
from types import MappingProxyType
import os
from pathlib import Path
import re


class SettingsError(ValueError):
    pass


def _require(ok, message):
    if not ok:
        raise SettingsError('Planner settings: ' + message)


def _fields(value, expected, label, optional=()):
    _require(isinstance(value, dict), label + ' must be an object')
    _require(set(expected) <= set(value) <= set(expected) | set(optional),
             label + ' requires exactly: ' + ', '.join(expected)
             + (' (optional: ' + ', '.join(optional) + ')' if optional else ''))


def _families(value, label):
    # Names must equal what provider_family()/family() return, e.g. "Max", "Disney+".
    _require(isinstance(value, list) and all(
        isinstance(v, str) and v == v.strip() and v and not any(ord(c) < 32 for c in v)
        for v in value), label + ' must be a list of provider family names')
    _require(len(set(value)) == len(value), label + ' has duplicates')
    return frozenset(value)


# Logical category IDs the planners' decision logic branches on. They are code
# identities, not settings; only the Arr tags that select them are configurable.
CATEGORIES = ('Common', 'Current', 'Library', 'Rare', 'Archive')

# Migratarr's own tag namespace, used when planner.json has no "overrides".
DEFAULT_OVERRIDES = {
    'lock_tag': 'migratarr-lock',
    'category_tags': {
        'migratarr-common': 'Common',
        'migratarr-current': 'Current',
        'migratarr-library': 'Library',
        'migratarr-rare': 'Rare',
        'migratarr-archive': 'Archive',
    },
}


@dataclass(frozen=True)
class OverrideTags:
    """Arr tag labels (compared lowercased) that pin or lock an item's category."""
    lock_tag: str
    category_tags: MappingProxyType

    def locked(self, tags):
        return self.lock_tag in tags

    def agrees(self, tags, recommended):
        """True unless a category tag is present that doesn't select `recommended`.

        Matches the executors' former `'migratarr-' + recommended.lower()`
        comparison for every input, including unexpected `recommended` values.
        """
        found = set(tags) & set(self.category_tags)
        expected = {tag for tag, category in self.category_tags.items()
                    if category.lower() == str(recommended).lower()}
        return not found or found == expected


@dataclass(frozen=True)
class PlannerSettings:
    # ISO 3166-1 alpha-2 key of TMDB's watch/providers "results" object.
    region: str
    subscribed: frozenset
    user_free_access: frozenset
    overrides: OverrideTags


def _tag(value, label):
    # Arr labels are lowercased before comparison, so an uppercase tag could never match.
    _require(isinstance(value, str) and value and value == value.lower()
             and not any(c.isspace() or c in ',;' or ord(c) < 32 for c in value),
             label + ' must be a lowercase tag without spaces or separators')


def _overrides(data):
    _fields(data, ('lock_tag', 'category_tags'), 'overrides')
    _tag(data['lock_tag'], 'overrides.lock_tag')
    tags = data['category_tags']
    _require(isinstance(tags, dict) and tags, 'overrides.category_tags must be a nonempty object')
    for tag, category in tags.items():
        _tag(tag, 'overrides.category_tags key')
        _require(category in CATEGORIES,
                 'overrides.category_tags values must be one of: ' + ', '.join(CATEGORIES))
    _require(len(set(tags.values())) == len(tags), 'overrides.category_tags must map to distinct categories')
    _require(data['lock_tag'] not in tags, 'overrides.lock_tag cannot also be a category tag')
    return OverrideTags(data['lock_tag'], MappingProxyType(dict(tags)))


def parse_settings(data):
    _fields(data, ('schema_version', 'streaming'), 'root', optional=('overrides',))
    _require(type(data['schema_version']) is int and data['schema_version'] == 1,
             'unsupported schema_version')
    streaming = data['streaming']
    _fields(streaming, ('region', 'subscribed', 'user_free_access'), 'streaming')
    _require(isinstance(streaming['region'], str) and re.fullmatch(r'[A-Z]{2}', streaming['region']),
             'streaming.region must be a two-letter uppercase TMDB region code')
    return PlannerSettings(streaming['region'],
                           _families(streaming['subscribed'], 'streaming.subscribed'),
                           _families(streaming['user_free_access'], 'streaming.user_free_access'),
                           _overrides(data.get('overrides', DEFAULT_OVERRIDES)))


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, 'duplicate field: ' + key)
        result[key] = value
    return result


def load_settings(filename=None, environ=None):
    env = os.environ if environ is None else environ
    filename = (filename or env.get('MIGRATARR_PLANNER_CONFIG')
                or Path(__file__).parent / 'config' / 'planner.json')
    try:
        data = json.loads(Path(filename).expanduser().read_text(encoding='utf-8'),
                          object_pairs_hook=_unique_object)
    except (OSError, ValueError) as exc:
        if isinstance(exc, SettingsError):
            raise
        raise SettingsError('Planner settings: cannot load ' + str(filename) + ': ' + str(exc)) from exc
    return parse_settings(data)


@lru_cache(maxsize=1)
def get_settings():
    """Freeze effective settings for the lifetime of this process."""
    return load_settings()
