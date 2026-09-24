"""Streaming provider families: planner.json streaming.families vs the old literals."""
import ast
import json
from pathlib import Path
import unittest

from planner_settings import DEFAULT_FAMILIES, SettingsError, family_of, parse_settings


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / 'tests/fixtures'

# Provider names as TMDB watch/providers returns them (US and elsewhere),
# plus names that exercise the substring order and the fallback.
PROVIDER_NAMES = [
    'Netflix', 'Netflix basic with Ads', 'Netflix Kids', 'Amazon Prime Video', 'Amazon Prime Video with Ads',
    'Amazon Video', 'Apple TV', 'Apple TV Plus', 'Apple TV+', 'Apple TV Amazon Channel', 'Disney Plus',
    'Disney+ Hotstar', 'Hulu', 'Max', 'HBO Max', 'Max Amazon Channel', 'HBO Max Amazon Channel',
    'Cinemax Amazon Channel', 'Cinemax Apple TV Channel', 'MGM Plus', 'MGM+ Amazon Channel', 'MGM Plus Roku Premium Channel',
    'Paramount Plus', 'Paramount+ Amazon Channel', 'Paramount+ with Showtime', 'Paramount Plus Apple TV Channel',
    'Paramount+ Roku Premium Channel', 'Peacock', 'Peacock Premium', 'Peacock Premium Plus', 'Starz',
    'Starz Amazon Channel', 'Starz Apple TV Channel', 'Starz Roku Premium Channel', 'Tubi TV', 'Pluto TV',
    'The Roku Channel', 'Crunchyroll', 'fuboTV', 'Sling TV', 'YouTube TV', 'YouTube Premium', 'Google Play Movies',
    'Vudu', 'Fandango At Home', 'Plex', 'Kanopy', 'Hoopla', 'Criterion Channel', 'MUBI', 'Shudder', 'AMC+',
    'AMC+ Amazon Channel', 'BritBox', 'Acorn TV', 'Philo', 'DIRECTV', 'Spectrum On Demand', 'Freevee',
    'Maxdome', 'Sky Go', 'NOW', 'Crave', 'Stan', 'BINGE', 'Canal+', 'RTL+', 'Joyn', 'Magenta TV', 'Videoland',
    'CBC Gem', 'ITVX', 'BBC iPlayer', 'Channel 4', '', 'MAX', 'hbo', 'PARAMOUNT', 'amazon prime video',
]


def old_function(script, name):
    """The planner's own function at e11681b (unchanged since 6dc21bd)."""
    path = FIXTURE / f'{script}_before_media_layout.py'
    tree = ast.parse(path.read_text(encoding='utf-8'))
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name]
    namespace = {}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), 'exec'), namespace)
    return namespace[name]


def current_function(script, name, settings):
    path = ROOT / f'{script}.py'
    tree = ast.parse(path.read_text(encoding='utf-8'))
    nodes = [n for n in tree.body if (isinstance(n, ast.FunctionDef) and n.name == name)
             or (isinstance(n, ast.Assign) and [getattr(t, 'id', None) for t in n.targets] == ['STREAMING_FAMILIES'])]
    namespace = {'PLANNER_SETTINGS': settings, 'family_of': family_of}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), 'exec'), namespace)
    return namespace[name]


def settings(families=None):
    data = json.loads((ROOT / 'config/planner.example.json').read_text(encoding='utf-8'))
    data['streaming'].pop('families', None)
    if families is not None:
        data['streaming']['families'] = families
    return parse_settings(data)


class DefaultFamilyTests(unittest.TestCase):
    def test_defaults_match_both_planners_literals(self):
        rules = settings().families
        for script, name in (('dry_run_movies', 'provider_family'), ('dry_run_tv', 'family')):
            old, new = old_function(script, name), current_function(script, name, settings())
            for provider in PROVIDER_NAMES:
                with self.subTest(script=script, provider=provider):
                    self.assertEqual(family_of(provider, rules), old(provider))
                    self.assertEqual(new(provider), old(provider))

    def test_order_and_substring_quirks_are_preserved(self):
        rules = settings().families
        self.assertEqual(family_of('Paramount+ Amazon Channel', rules), 'Paramount+')
        self.assertEqual(family_of('Apple TV Amazon Channel', rules), 'Apple TV')
        self.assertEqual(family_of('Cinemax Amazon Channel', rules), 'Max')  # "max" substring, as before
        self.assertEqual(family_of('Amazon Video', rules), 'Amazon Video')
        self.assertEqual(settings().families, tuple((e['family'], tuple(e['match'])) for e in DEFAULT_FAMILIES))


class ConfiguredFamilyTests(unittest.TestCase):
    def test_configured_list_replaces_defaults_in_order(self):
        custom = settings([{'family': 'Sky', 'match': ['sky', 'now']},
                           {'family': 'Cinemax', 'match': ['cinemax']},
                           {'family': 'HBO', 'match': ['hbo', 'max']}])
        movies = current_function('dry_run_movies', 'provider_family', custom)
        tv = current_function('dry_run_tv', 'family', custom)
        for provider, expected in (('Sky Go', 'Sky'), ('NOW', 'Sky'), ('Cinemax Amazon Channel', 'Cinemax'),
                                   ('HBO Max', 'HBO'), ('Paramount Plus', 'Paramount Plus'), ('Hulu', 'Hulu')):
            with self.subTest(provider):
                self.assertEqual(movies(provider), expected)
                self.assertEqual(tv(provider), expected)

    def test_configured_families_drive_streaming_scores(self):
        from migratarr_validation.streaming_parity import load_streaming
        custom = settings([{'family': 'Sky', 'match': ['sky', 'now']}])
        data = json.loads((ROOT / 'config/planner.example.json').read_text(encoding='utf-8'))
        data['streaming'].update(subscribed=['Sky'], families=[{'family': 'Sky', 'match': ['sky', 'now']}],
                                 region='GB')
        custom = parse_settings(data)
        response = {'results': {'GB': {'flatrate': [{'provider_name': 'NOW'}, {'provider_name': 'Sky Go'}]}}}
        for kind, function in (('movies', 'streaming_score'), ('tv', 'provider_score')):
            ns = load_streaming(kind, settings=custom)
            if kind == 'movies':
                ns['tmdb_movie_providers'] = lambda _id: (response, True)
                self.assertEqual(ns[function](1), (0, 'Subscribed: Sky'))
            else:
                self.assertEqual(ns[function](response), (0, 'Subscribed: Sky'))

    def test_rejects_malformed_families(self):
        for label, families in [
            ('not a list', {'family': 'Max', 'match': ['max']}),
            ('empty list', []),
            ('extra key', [{'family': 'Max', 'match': ['max'], 'order': 1}]),
            ('missing match', [{'family': 'Max'}]),
            ('empty match', [{'family': 'Max', 'match': []}]),
            ('uppercase pattern', [{'family': 'Max', 'match': ['Max']}]),
            ('empty pattern', [{'family': 'Max', 'match': ['']}]),
            ('blank family', [{'family': ' ', 'match': ['max']}]),
            ('duplicate family', [{'family': 'Max', 'match': ['max']}, {'family': 'Max', 'match': ['hbo']}]),
        ]:
            with self.subTest(label), self.assertRaises(SettingsError):
                settings(families)


if __name__ == '__main__':
    unittest.main()
