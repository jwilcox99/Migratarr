import json
from pathlib import Path
import tempfile
import unittest

from planner_settings import SettingsError, load_settings, parse_settings
from migratarr_validation.streaming_parity import compare_streaming, load_streaming


ROOT = Path(__file__).resolve().parents[1]


def response(region='US', **kinds):
    return {'id': 1, 'results': {region: {kind: [{'provider_name': n} for n in names]
                                          for kind, names in kinds.items()}}}


# Pins dry_run_movies.py streaming_score() and dry_run_tv.py provider_score()
# exactly as they behaved at 6dc21bd with SUBSCRIBED = {"Hulu", "Peacock"},
# USER_FREE_ACCESS = set() and the literal "US" region lookup.
CASES = [
    (response(flatrate=['Hulu', 'Netflix']), (0, 'Subscribed: Hulu')),
    (response(flatrate=['Peacock Premium', 'Hulu']), (0, 'Subscribed: Hulu, Peacock')),
    # "free" only counts when listed in USER_FREE_ACCESS, which is empty.
    (response(free=['Tubi TV']), (100, 'No US availability found')),
    (response(ads=['Tubi TV', 'Pluto TV'], flatrate=['Netflix']), (25, 'Free with ads: Pluto TV, Tubi TV')),
    (response(flatrate=['Netflix', 'HBO Max', 'Disney Plus']), (35, '3 subscription families')),
    (response(flatrate=['Amazon Prime Video', 'Paramount+ Amazon Channel']), (40, '2 subscription families')),
    (response(flatrate=['Apple TV Plus', 'Apple TV+']), (50, '1 subscription family: Apple TV')),
    (response(rent=['Apple TV'], buy=['Apple TV']), (60, 'Rental available')),
    (response(buy=['Google Play Movies']), (80, 'Purchase only')),
    # Only the US entry is ever read, even when another region is subscribed.
    (response('GB', flatrate=['Hulu']), (100, 'No US availability found')),
    ({'id': 1}, (100, 'No US availability found')),
]


class StreamingCharacterizationTests(unittest.TestCase):
    def setUp(self):
        self.movies = load_streaming('movies')
        self.tv = load_streaming('tv')

    def test_movie_and_tv_scoring_match_pinned_behavior(self):
        for data, expected in CASES:
            with self.subTest(data=data):
                self.movies['tmdb_movie_providers'] = lambda _id, data=data: (data, True)
                self.assertEqual(self.movies['streaming_score'](42), expected)
                self.assertEqual(self.tv['provider_score'](data), expected)

    def test_movie_lookup_failure_is_unscored(self):
        def fail(_id):
            raise OSError('offline')
        self.movies['tmdb_movie_providers'] = fail
        self.assertEqual(self.movies['streaming_score'](42), (None, 'Streaming lookup failed'))

    def test_tv_season_falls_back_to_series_only_with_regional_data(self):
        seasons = {1: response(flatrate=['Hulu']), 2: response('GB', flatrate=['Hulu'])}
        def season(_id, number):
            if number not in seasons:
                raise OSError('offline')
            return seasons[number], True
        self.tv['tmdb_season_providers'] = season
        self.tv['tmdb_series_providers'] = lambda _id: (response(ads=['Tubi TV']), True)
        self.assertEqual(self.tv['series_streaming'](7, {3, 1, 2}), (22.5, '; '.join([
            'S1=0 [season] (Subscribed: Hulu)',
            'S2=25 [series-fallback] (Free with ads: Tubi TV)',
            'S3=25 [series-fallback] (Free with ads: Tubi TV)'])))
        self.tv['tmdb_series_providers'] = lambda _id: (response('GB', ads=['Tubi TV']), True)
        self.assertEqual(self.tv['series_streaming'](7, {2}), (None, 'S2=UNKNOWN (no provider data)'))


BASELINE_MOVIES = ROOT / 'tests/fixtures/dry_run_movies_before_planner_settings.py'
BASELINE_TV = ROOT / 'tests/fixtures/dry_run_tv_before_planner_settings.py'


class StreamingSettingsTests(unittest.TestCase):
    def settings(self, region='US', subscribed=('Hulu', 'Peacock'), free=()):
        return parse_settings({'schema_version': 1, 'streaming': {
            'region': region, 'subscribed': list(subscribed), 'user_free_access': list(free)}})

    def test_pinned_cases_also_describe_the_baseline(self):
        movies, tv = load_streaming('movies', BASELINE_MOVIES), load_streaming('tv', BASELINE_TV)
        for data, expected in CASES:
            with self.subTest(data=data):
                movies['tmdb_movie_providers'] = lambda _id, data=data: (data, True)
                self.assertEqual(movies['streaming_score'](42), expected)
                self.assertEqual(tv['provider_score'](data), expected)

    def test_configured_region_and_subscriptions_drive_scoring(self):
        settings = self.settings('GB', ['Netflix'], ['Tubi TV'])
        movies, tv = load_streaming('movies', settings=settings), load_streaming('tv', settings=settings)
        for data, expected in [
            (response('GB', flatrate=['Netflix', 'Hulu']), (0, 'Subscribed: Netflix')),
            (response('GB', free=['Tubi TV']), (0, 'Free access: Tubi TV')),
            (response('GB', flatrate=['Hulu']), (50, '1 subscription family: Hulu')),
            (response('US', flatrate=['Netflix']), (100, 'No GB availability found')),
        ]:
            with self.subTest(data=data):
                movies['tmdb_movie_providers'] = lambda _id, data=data: (data, True)
                self.assertEqual(movies['streaming_score'](42), expected)
                self.assertEqual(tv['provider_score'](data), expected)
        tv['tmdb_season_providers'] = lambda _id, _s: (response('US', flatrate=['Netflix']), True)
        tv['tmdb_series_providers'] = lambda _id: (response('GB', flatrate=['Netflix']), True)
        self.assertEqual(tv['series_streaming'](7, {1}),
                         (0.0, 'S1=0 [series-fallback] (Subscribed: Netflix)'))

    def write_cache(self, directory):
        for index, (data, _) in enumerate(CASES):
            (directory / f'tmdb_provider_{index}.json').write_text(json.dumps(data))
            (directory / f'tmdb_tv_series_provider_{index}.json').write_text(json.dumps(data))
            (directory / f'tmdb_tv_provider_{index}_s1.json').write_text(json.dumps(CASES[-index][0]))
        (directory / 'tmdb_tv_provider_99_s2.json').write_text(json.dumps(CASES[0][0]))
        (directory / 'jellyfin_ignored.json').write_text('{}')

    def test_parity_harness_on_cached_responses(self):
        with tempfile.TemporaryDirectory() as temp:
            cache = Path(temp)
            self.write_cache(cache)
            result = compare_streaming(BASELINE_MOVIES, BASELINE_TV, cache, self.settings())
            self.assertTrue(result['byte_identical'], result)
            self.assertEqual((result['movie_responses'], result['tv_responses'], result['tv_series']),
                             (len(CASES), 2 * len(CASES) + 1, len(CASES) + 1))
            changed = compare_streaming(BASELINE_MOVIES, BASELINE_TV, cache, self.settings(subscribed=['Hulu']))
            self.assertFalse(changed['byte_identical'])
            self.assertIn('movie/1', changed['differences'])
            with self.assertRaisesRegex(ValueError, 'Baseline must be'):
                compare_streaming(ROOT / 'dry_run_movies.py', BASELINE_TV, cache, self.settings())


class PlannerSettingsTests(unittest.TestCase):
    def valid(self):
        return json.loads((ROOT / 'config/planner.example.json').read_text(encoding='utf-8'))

    def test_example_matches_pinned_literals(self):
        settings = load_settings(ROOT / 'config/planner.example.json')
        self.assertEqual(settings.region, 'US')
        self.assertEqual(settings.subscribed, {'Hulu', 'Peacock'})
        self.assertEqual(settings.user_free_access, frozenset())

    def test_rejects_malformed_settings(self):
        mutations = [
            lambda d: d.update(extra=1),
            lambda d: d.update(schema_version=2),
            lambda d: d['streaming'].pop('region'),
            lambda d: d['streaming'].update(region='us'),
            lambda d: d['streaming'].update(region='USA'),
            lambda d: d['streaming'].update(subscribed='Hulu'),
            lambda d: d['streaming'].update(subscribed=['Hulu', 'Hulu']),
            lambda d: d['streaming'].update(subscribed=[' Hulu']),
            lambda d: d['streaming'].update(user_free_access=['']),
        ]
        for mutate in mutations:
            data = self.valid()
            mutate(data)
            with self.subTest(data=data), self.assertRaises(SettingsError):
                parse_settings(data)

    def test_missing_or_duplicate_field_file_fails_loud(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(SettingsError, 'cannot load'):
                load_settings(Path(temp) / 'absent.json')
            dup = Path(temp) / 'dup.json'
            dup.write_text('{"schema_version": 1, "schema_version": 1}')
            with self.assertRaisesRegex(SettingsError, 'duplicate field'):
                load_settings(dup)
            with self.assertRaisesRegex(SettingsError, 'cannot load'):
                load_settings(environ={'MIGRATARR_PLANNER_CONFIG': str(Path(temp) / 'env.json')})


if __name__ == '__main__':
    unittest.main()
