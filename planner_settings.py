"""Validated planner preferences: streaming access, Arr override tags, scoring."""
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


# Every scoring number both dry-run planners use, as they were literals at
# f49052d. planner.json "scoring" may override any subset; nested objects merge
# onto these, lists (tiers) replace whole. Keep ints as ints: the planners print
# some of these values into their CSVs, where 50 and 50.0 differ.
DEFAULT_SCORING = {
    # final = replacement*w + streaming*w + usage*w (+ movie franchise bonus)
    'weights': {'replacement': 0.45, 'streaming': 0.30, 'usage': 0.25},
    'thresholds': {
        'rare': 70,                      # final >= rare -> Rare
        'library': 35,                   # movies: final >= library -> Library, else Common
        'near_margin': 5,                # movies: NEAR_<threshold> flag within this distance
        'blackout_min_replacement': 15,  # streaming unavailable and replacement >= this -> Rare
    },
    # Points by number of distinct viable releases: first [minimum, points] met wins.
    'replacement': {'tiers': [[20, 0], [10, 15], [6, 30], [4, 45], [3, 60], [2, 75], [1, 90]],
                    'none': 100},
    # Points by best streaming situation in the configured region.
    'streaming': {'subscribed': 0, 'free_access': 0, 'ads': 25, 'three_or_more_families': 35,
                  'two_families': 40, 'one_family': 50, 'rental': 60, 'purchase': 80,
                  'unavailable': 100},
    'usage': {
        'weights': {'recency': 0.60, 'repeat': 0.25, 'users': 0.15},
        # First [maximum days since last play, points] met wins.
        'recency_days': [[7, 100], [30, 90], [90, 75], [180, 55], [365, 35], [730, 20]],
        'recency_older': 10,
        'recency_never': 0,
        'plays': [[10, 100], [6, 80], [3, 60], [2, 40], [1, 20]],
        'plays_none': 0,
        'users': [[4, 100], [3, 80], [2, 60], [1, 35]],
        'users_none': 0,
        # Never-played items: full grace points until grace_full_days after being
        # added, decaying linearly to 0 at grace_end_days.
        'grace_points': 50,
        'grace_full_days': 90,
        'grace_end_days': 180,
    },
    'archive': {
        'max_replacement': 35,           # replacement must be below this
        'min_days_since_added': 180,
        'min_days_since_played': 180,    # never played also qualifies
        'movie_min_age_years': 5,
        'tv_min_days_since_finale': 365,
    },
    'franchise': {'collection_bonus': 5, 'protected_bonus': 5, 'min_protected': 2, 'max_bonus': 10},
    # TV: episode -> season -> series blends of the hardest and the average score.
    'tv_aggregation': {'worst': 0.70, 'average': 0.30},
}
AT_LEAST_TIERS = {('replacement', 'tiers'), ('usage', 'plays'), ('usage', 'users')}
AT_MOST_TIERS = {('usage', 'recency_days')}


def at_least(value, tiers, default):
    """Points of the first [minimum, points] tier with value >= minimum."""
    for minimum, points in tiers:
        if value >= minimum:
            return points
    return default


def at_most(value, tiers, default):
    """Points of the first [maximum, points] tier with value <= maximum."""
    for maximum, points in tiers:
        if value <= maximum:
            return points
    return default


class Section:
    """Read-only attribute view of one validated scoring object."""
    def __init__(self, data):
        for key, value in data.items():
            if isinstance(value, dict):
                value = Section(value)
            elif isinstance(value, list):
                value = tuple(tuple(pair) for pair in value)
            object.__setattr__(self, key, value)

    def __setattr__(self, key, value):
        raise AttributeError('scoring settings are read-only')

    def as_dict(self):
        return {k: v.as_dict() if isinstance(v, Section) else
                [list(p) for p in v] if isinstance(v, tuple) else v for k, v in vars(self).items()}

    def __eq__(self, other):
        return isinstance(other, Section) and self.as_dict() == other.as_dict()


def _number(value, label):
    _require(type(value) in (int, float) and value == value and abs(value) != float('inf') and value >= 0,
             label + ' must be a nonnegative number')


def _merge(default, given, path):
    label = 'scoring' + ''.join('.' + p for p in path)
    if isinstance(default, dict):
        _require(isinstance(given, dict), label + ' must be an object')
        unknown = set(given) - set(default)
        _require(not unknown, label + ' has unknown fields: ' + ', '.join(sorted(unknown)))
        return {k: _merge(v, given[k], path + (k,)) if k in given else _merge(v, v, path + (k,))
                for k, v in default.items()}
    if isinstance(default, list):
        _require(isinstance(given, list) and given and all(
            isinstance(p, list) and len(p) == 2 for p in given), label + ' must be a nonempty list of [limit, points]')
        for limit, points in given:
            _number(limit, label + ' limit')
            _number(points, label + ' points')
        limits = [p[0] for p in given]
        if path in AT_LEAST_TIERS:
            _require(all(a > b for a, b in zip(limits, limits[1:])), label + ' minimums must strictly decrease')
        else:
            _require(all(a < b for a, b in zip(limits, limits[1:])), label + ' maximums must strictly increase')
        return [list(p) for p in given]
    _number(given, label)
    return given


def _scoring(data):
    merged = _merge(DEFAULT_SCORING, data, ())
    t, u = merged['thresholds'], merged['usage']
    _require(t['library'] < t['rare'], 'scoring.thresholds.library must be below rare')
    _require(u['grace_full_days'] < u['grace_end_days'],
             'scoring.usage.grace_full_days must be below grace_end_days')
    return Section(merged)


@dataclass(frozen=True)
class PlannerSettings:
    # ISO 3166-1 alpha-2 key of TMDB's watch/providers "results" object.
    region: str
    subscribed: frozenset
    user_free_access: frozenset
    overrides: OverrideTags
    scoring: Section


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
    _fields(data, ('schema_version', 'streaming'), 'root', optional=('overrides', 'scoring'))
    _require(type(data['schema_version']) is int and data['schema_version'] == 1,
             'unsupported schema_version')
    streaming = data['streaming']
    _fields(streaming, ('region', 'subscribed', 'user_free_access'), 'streaming')
    _require(isinstance(streaming['region'], str) and re.fullmatch(r'[A-Z]{2}', streaming['region']),
             'streaming.region must be a two-letter uppercase TMDB region code')
    return PlannerSettings(streaming['region'],
                           _families(streaming['subscribed'], 'streaming.subscribed'),
                           _families(streaming['user_free_access'], 'streaming.user_free_access'),
                           _overrides(data.get('overrides', DEFAULT_OVERRIDES)),
                           _scoring(data.get('scoring', {})))


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
