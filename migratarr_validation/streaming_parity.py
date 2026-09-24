"""Read-only comparison of pinned pre-settings streaming scoring and candidate.

Both dry-run planners are parsed, not imported: importing them contacts
Docker, Sonarr/Radarr and TMDB. Only the streaming-scarcity functions and
their settings constants are executed, fed from an existing TMDB cache.
"""
import argparse
import ast
import hashlib
import json
from pathlib import Path
import re
from statistics import mean

ROOT = Path(__file__).resolve().parents[1]
BASELINE_SHA256 = {
    'movies': '257383f1e380239b414eeea7aa8f3bd910141986db6a0baa661257716d3e6bb7',
    'tv': '422ba0e1db5bf34b1cf0778b433da26f570ae7712eb81ac028957d6ba9ab7b50',
}
FUNCTIONS = {
    'movies': {'provider_family', 'streaming_score'},
    'tv': {'family', 'provider_score', 'series_streaming'},
}
CONSTANTS = {'SUBSCRIBED', 'USER_FREE_ACCESS', 'STREAMING_REGION', 'SCORING'}
FETCHERS = ('tmdb_movie_providers', 'tmdb_series_providers', 'tmdb_season_providers')


def _assigned_name(node):
    if not isinstance(node, ast.Assign) or len(node.targets) != 1:
        return None
    target = node.targets[0]
    return target.id if isinstance(target, ast.Name) else None


def load_streaming(kind, planner=None, settings=None):
    """Execute only a planner's streaming functions; fetchers must be supplied."""
    planner = planner or ROOT / ('dry_run_' + kind + '.py')
    tree = ast.parse(planner.read_text(encoding='utf-8'), filename=str(planner))
    if settings is None:
        from planner_settings import load_settings
        settings = load_settings(ROOT / 'config' / 'planner.example.json')
    namespace = {'PLANNER_SETTINGS': settings, 'mean': mean}
    nodes = [n for n in tree.body
             if (isinstance(n, ast.FunctionDef) and n.name in FUNCTIONS[kind])
             or _assigned_name(n) in CONSTANTS]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(planner), 'exec'), namespace)
    return namespace


def _read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def cached_responses(cache_dir):
    """Group the planners' own TMDB provider cache files by what they answer."""
    movies, series, seasons = {}, {}, {}
    for path in sorted(cache_dir.glob('tmdb_*.json')):
        name = path.stem
        if m := re.fullmatch(r'tmdb_provider_(\d+)', name):
            movies[m[1]] = path
        elif m := re.fullmatch(r'tmdb_tv_series_provider_(\d+)', name):
            series[m[1]] = path
        elif m := re.fullmatch(r'tmdb_tv_provider_(\d+)_s(\d+)', name):
            seasons.setdefault(m[1], {})[int(m[2])] = path
    return movies, series, seasons


def score_all(movie_ns, tv_ns, cache_dir):
    movies, series, seasons = cached_responses(cache_dir)

    def missing(*key):
        raise FileNotFoundError('not cached: ' + '/'.join(map(str, key)))

    movie_ns['tmdb_movie_providers'] = lambda i: (_read(movies[str(i)]), True) if str(i) in movies else missing(i)
    tv_ns['tmdb_series_providers'] = lambda i: (_read(series[str(i)]), True) if str(i) in series else missing(i)
    tv_ns['tmdb_season_providers'] = lambda i, s: ((_read(seasons[str(i)][s]), True)
                                                   if s in seasons.get(str(i), {}) else missing(i, s))
    results = {'movie': {i: movie_ns['streaming_score'](i) for i in movies},
               'tv_response': {}, 'tv_series': {}}
    for path in [*series.values(), *(p for s in seasons.values() for p in s.values())]:
        results['tv_response'][path.name] = tv_ns['provider_score'](_read(path))
    for tmdb_id in sorted(set(series) | set(seasons)):
        results['tv_series'][tmdb_id] = tv_ns['series_streaming'](tmdb_id, set(seasons.get(tmdb_id, ())))
    return results


def compare_streaming(baseline_movies, baseline_tv, cache_dir, settings):
    for kind, path in (('movies', baseline_movies), ('tv', baseline_tv)):
        digest = hashlib.sha256(path.read_text(encoding='utf-8').encode()).hexdigest()
        if digest != BASELINE_SHA256[kind]:
            raise ValueError('Baseline must be dry_run_' + kind + '.py at 6dc21bd')
    old = score_all(load_streaming('movies', baseline_movies), load_streaming('tv', baseline_tv), cache_dir)
    new = score_all(load_streaming('movies', settings=settings), load_streaming('tv', settings=settings), cache_dir)
    encode = lambda value: json.dumps(value, sort_keys=True).encode()
    old_bytes, new_bytes = encode(old), encode(new)
    digest = lambda value: hashlib.sha256(value).hexdigest()
    return {'byte_identical': old_bytes == new_bytes,
            'movie_responses': len(old['movie']), 'tv_responses': len(old['tv_response']),
            'tv_series': len(old['tv_series']),
            'baseline_sha256': digest(old_bytes), 'candidate_sha256': digest(new_bytes),
            'differences': sorted(f'{section}/{key}' for section in old for key in old[section]
                                  if old[section][key] != new[section].get(key))}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('baseline-movies', 'baseline-tv', 'cache-dir', 'planner-config'):
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    from planner_settings import load_settings
    result = compare_streaming(args.baseline_movies, args.baseline_tv, args.cache_dir,
                               load_settings(args.planner_config))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result['byte_identical'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
