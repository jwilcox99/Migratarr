"""A small fake Radarr/Sonarr/Jellyfin/TMDB world that exercises every
placement branch of dry_run_movies.py and dry_run_tv.py, served through a
fake urllib.request.urlopen so the planners run unmodified."""
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from runtime_config import load_config

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
USERS = ['u1', 'u2', 'u3', 'u4']


def ago(days):
    return (NOW - timedelta(days=days)).isoformat().replace('+00:00', 'Z')


def releases(count, prefix='Release', **extra):
    return [dict(title=f'{prefix} {n} 1080p', **extra) for n in range(count)]


HARD = {'rejections': ['Not an upgrade for existing file']}
SOFT = {'rejections': [{'reason': 'Existing file meets cutoff: Bluray-1080p'}]}


def providers(**kinds):
    return {'id': 1, 'results': {'US': {k: [{'provider_name': n} for n in v] for k, v in kinds.items()}}}


# (title, year, bucket, collection, releases|None=error, providers|None=error, usage)
# usage: (days_since_added, plays_per_user list, days_since_last_play or None)
MOVIES = [
    ('Blackout', 2015, 'Library', None, 2, {'id': 1, 'results': {'US': {}}}, (400, [], None)),
    ('Beloved', 2021, 'Common', None, 1, providers(rent=['Apple TV']), (500, [5, 4, 2, 1], 3)),
    ('Old Cold Easy', 2000, 'Common', None, 25, providers(flatrate=['Hulu']), (800, [1], 400)),
    ('Middling', 2024, 'Common', None, 5, providers(ads=['Tubi TV']), (300, [3, 1], 60)),
    ('Plentiful', 2023, 'Library', None, 12, providers(flatrate=['Hulu']), (400, [], None)),
    ('Search Failed', 2010, 'Common', None, None, providers(flatrate=['Hulu']), (400, [], None)),
    ('Just Added', 2025, 'Common', None, 8, providers(flatrate=['Netflix']), (30, [], None)),
    ('Grace Fading', 2025, 'Common', None, 8, providers(flatrate=['Netflix', 'HBO Max']), (120, [], None)),
    ('Saga One', 2005, 'Rare', {'tmdbId': 900}, 6, providers(flatrate=['Netflix', 'Max', 'Disney Plus']), (900, [2], 200)),
    ('Saga Two', 2008, 'Library', {'tmdbId': 900}, 3, providers(flatrate=['Peacock Premium']), (900, [], None)),
    ('Saga Three', 2012, 'Common', {'tmdbId': 900}, 20, providers(rent=['Apple TV']), (900, [1, 1], 20)),
    ('Streaming Failed', 2019, 'Common', None, 4, None, (400, [], None)),
    ('Soft Rejections', 2018, 'Common', None, 0, providers(buy=['Google Play']), (365, [10], 700)),
    ('Unknown Folder', 2017, None, 'Anthology', 10, providers(free=['Tubi TV']), (250, [6, 6, 6], 100)),
]

# (title, status, bucket, tmdb, seasons {n: (episodes, finale_days_ago, releases|None|'empty')},
#  providers {season|'series': data|None}, usage)
SERIES = [
    ('Running Show', 'continuing', 'Current', 5001, {1: (8, 400, 12), 2: (8, 20, 3)},
     {1: providers(flatrate=['Hulu']), 2: providers(ads=['Pluto TV'])}, (700, [4, 3], 5)),
    ('Finished Long Ago', 'ended', 'Library', 5002, {1: (10, 4000, 25), 2: (10, 3700, 22)},
     {1: providers(flatrate=['Hulu']), 2: providers(flatrate=['Peacock'])}, (1500, [1], 900)),
    ('Nowhere To Stream', 'ended', 'Library', 5003, {1: (6, 2000, 2)},
     {1: providers(free=['Tubi TV'])}, (1000, [], None)),
    ('Season Search Broke', 'ended', 'Rare', 5004, {1: (5, 1500, None)},
     {'series': providers(flatrate=['Netflix'])}, (900, [], None)),
    ('Empty Search', 'continuing', 'Library', 5005, {1: (4, 30, 'empty')},
     {1: providers(flatrate=['Netflix'])}, (60, [], None)),
    ('No Tmdb', 'deleted', 'Library', None, {1: (3, 900, 5)}, {}, None),
    ('Only Series Data', 'ended', 'Archive', 5007, {1: (6, 900, 1), 2: (6, 800, 1)},
     {'series': providers(flatrate=['Netflix', 'Max'])}, (500, [2], 250)),
    ('Unknown Streaming Archive', 'ended', 'Archive', 5008, {1: (4, 3000, 2)}, {}, (2000, [], None)),
    ('Unknown Streaming Upcoming', 'upcoming', 'Library', 5009, {1: (2, 10, 1)}, {}, (40, [], None)),
    ('Scarce Favorite', 'ended', 'Library', 5010, {1: (5, 500, 1)},
     {1: providers(rent=['Apple TV'])}, (700, [8, 6, 4, 2], 2)),
    ('Archive Unknown Easy', 'ended', 'Archive', 5011, {1: (4, 3000, 30)}, {}, (2000, [], None)),
    ('Ended Unknown', 'ended', 'Library', 5012, {1: (3, 600, 4)}, {}, (500, [], None)),
]


def radarr_movies():
    result = []
    for n, (title, year, bucket, collection, *_rest) in enumerate(MOVIES, 1):
        folder = f'/media/Movies/{bucket}/{title} ({year})' if bucket else f'/media/Other/{title}'
        movie = dict(id=n, tmdbId=1000 + n, title=title, year=year, path=folder, hasFile=True)
        if collection is not None:
            movie['collection'] = collection
        result.append(movie)
    return result


def sonarr_series():
    return [dict(id=100 + n, tmdbId=tmdb, title=title, status=status,
                 path=f'/media/TV/{bucket}/{title}', statistics=dict(episodeFileCount=sum(
                     s[0] for s in seasons.values())))
            for n, (title, status, bucket, tmdb, seasons, *_rest) in enumerate(SERIES, 1)]


def jellyfin_items(user, kind):
    source = ([(1000 + n, m[6]) for n, m in enumerate(MOVIES, 1)] if kind == 'Movie' else
              [(s[3], s[6]) for s in SERIES if s[3] and s[6]])
    index = USERS.index(user)
    items = []
    for tmdb, (added, plays, last) in source:
        count = plays[index] if index < len(plays) else 0
        data = {'PlayCount': count}
        if count and last is not None:
            data['LastPlayedDate'] = ago(last + index)
        items.append({'ProviderIds': {'Tmdb': str(tmdb)}, 'DateCreated': ago(added), 'UserData': data})
    return {'Items': items}


def route(url):
    parts = urlsplit(url)
    query = {k: v[0] for k, v in parse_qs(parts.query).items()}
    path = parts.path
    if parts.hostname == 'api.themoviedb.org':
        bits = path.split('/')
        # /3/movie/<id>/watch/providers, /3/tv/<id>[/season/<n>]/watch/providers
        if bits[2] == 'movie':
            data = MOVIES[int(bits[3]) - 1001][5]
        else:
            series = next(s for s in SERIES if s[3] == int(bits[3]))
            data = series[5].get(int(bits[5]) if bits[4] == 'season' else 'series')
        if data is None:
            raise OSError('TMDB unavailable')
        return data
    if path == '/Users':
        return [{'Id': u} for u in USERS]
    if path.startswith('/Users/'):
        return jellyfin_items(path.split('/')[2], query['IncludeItemTypes'])
    if path == '/api/v3/movie':
        return radarr_movies()
    if path == '/api/v3/series':
        return sonarr_series()
    if path == '/api/v3/release' and 'movieId' in query:
        count = MOVIES[int(query['movieId']) - 1][4]
        if count is None:
            raise TimeoutError('indexer timeout')
        return releases(count) + releases(2, 'Rejected', **HARD) + releases(1, 'Cutoff', **SOFT)
    series = SERIES[int(query['seriesId']) - 101]
    if path == '/api/v3/episode':
        return [dict(seasonNumber=s, episodeNumber=e, hasFile=True, airDateUtc=ago(finale + (count - e)))
                for s, (count, finale, _) in series[4].items() for e in range(1, count + 1)]
    if path == '/api/v3/release':
        count = series[4][int(query['seasonNumber'])][2]
        if count is None:
            raise TimeoutError('indexer timeout')
        if count == 'empty':
            return []
        packs = releases(count, 'Pack', fullSeason=True)
        return packs + [dict(title=f'Episode 1 only {n}', episodeNumbers=[1]) for n in range(count)]
    raise AssertionError('unexpected request: ' + url)


def fake_urlopen(request, timeout=None):
    return io.BytesIO(json.dumps(route(request.full_url)).encode())


def offline_world(network=True):
    """Patches that let a planner run for real against this fake world.

    network=False keeps the example config but makes any HTTP or Docker call fail.
    """
    stack = ExitStack()
    config = load_config(ROOT / 'config/runtime.example.json', environ={})
    stack.enter_context(patch('runtime_config.get_config', return_value=config))
    stack.enter_context(patch.dict('os.environ', {
        'TMDB_TOKEN': 'fake', 'MIGRATARR_PLANNER_CONFIG': str(ROOT / 'config/planner.example.json')}))
    if network:
        stack.enter_context(patch('urllib.request.urlopen', fake_urlopen))
        stack.enter_context(patch('subprocess.check_output', return_value='fake-key\n'))
    else:
        for target in ('urllib.request.urlopen', 'subprocess.check_output', 'subprocess.run'):
            stack.enter_context(patch(target, side_effect=AssertionError('I/O during replay: ' + target)))
    stack.enter_context(patch('time.sleep'))
    return stack
