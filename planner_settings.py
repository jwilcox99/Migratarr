"""Validated planner preferences: what the owner can stream, and where."""
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re


class SettingsError(ValueError):
    pass


def _require(ok, message):
    if not ok:
        raise SettingsError('Planner settings: ' + message)


def _fields(value, expected, label):
    _require(isinstance(value, dict), label + ' must be an object')
    _require(set(value) == set(expected), label + ' requires exactly: ' + ', '.join(expected))


def _families(value, label):
    # Names must equal what provider_family()/family() return, e.g. "Max", "Disney+".
    _require(isinstance(value, list) and all(
        isinstance(v, str) and v == v.strip() and v and not any(ord(c) < 32 for c in v)
        for v in value), label + ' must be a list of provider family names')
    _require(len(set(value)) == len(value), label + ' has duplicates')
    return frozenset(value)


@dataclass(frozen=True)
class PlannerSettings:
    # ISO 3166-1 alpha-2 key of TMDB's watch/providers "results" object.
    region: str
    subscribed: frozenset
    user_free_access: frozenset


def parse_settings(data):
    _fields(data, ('schema_version', 'streaming'), 'root')
    _require(type(data['schema_version']) is int and data['schema_version'] == 1,
             'unsupported schema_version')
    streaming = data['streaming']
    _fields(streaming, ('region', 'subscribed', 'user_free_access'), 'streaming')
    _require(isinstance(streaming['region'], str) and re.fullmatch(r'[A-Z]{2}', streaming['region']),
             'streaming.region must be a two-letter uppercase TMDB region code')
    return PlannerSettings(streaming['region'],
                           _families(streaming['subscribed'], 'streaming.subscribed'),
                           _families(streaming['user_free_access'], 'streaming.user_free_access'))


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
