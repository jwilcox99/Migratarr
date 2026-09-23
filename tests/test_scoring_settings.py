import ast
import copy
from datetime import timedelta, timezone
import json
from pathlib import Path
from statistics import mean
import unittest

import planner_settings
from planner_settings import DEFAULT_SCORING, SettingsError, at_least, at_most, parse_settings
from migratarr_validation.dry_run_parity import frozen_datetime
from synthetic_library import NOW


ROOT = Path(__file__).resolve().parents[1]
BASELINE = {'movie': ROOT / 'tests/fixtures/dry_run_movies_before_scoring_settings.py',
            'tv': ROOT / 'tests/fixtures/dry_run_tv_before_scoring_settings.py'}
CANDIDATE = {'movie': ROOT / 'dry_run_movies.py', 'tv': ROOT / 'dry_run_tv.py'}
TIER_FUNCTIONS = {'movie': ('scarcity_from_count', 'recency_points', 'repeat_points', 'users_points'),
                  'tv': ('scarcity', 'recency_points', 'repeat_points', 'user_points')}


def example(scoring=None):
    data = json.loads((ROOT / 'config/planner.example.json').read_text(encoding='utf-8'))
    data.pop('scoring', None)
    if scoring is not None:
        data['scoring'] = scoring
    return parse_settings(data)


def load_functions(path, names, settings):
    """Execute only the named planner functions (and SCORING) without module side effects."""
    tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
    nodes = [n for n in tree.body if (isinstance(n, ast.FunctionDef) and n.name in names)
             or (isinstance(n, ast.Assign) and [getattr(t, 'id', None) for t in n.targets] == ['SCORING'])]
    namespace = {'PLANNER_SETTINGS': settings, 'datetime': frozen_datetime(NOW), 'timezone': timezone,
                 'mean': mean, 'at_least': at_least, 'at_most': at_most}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), 'exec'), namespace)
    return namespace


class ScoringSettingsTests(unittest.TestCase):
    def test_omitted_scoring_is_exactly_the_defaults(self):
        self.assertEqual(example().scoring.as_dict(), DEFAULT_SCORING)

    def test_partial_scoring_merges_onto_defaults(self):
        scoring = example({'thresholds': {'rare': 80}, 'usage': {'plays': [[5, 100], [1, 50]]}}).scoring
        self.assertEqual(scoring.thresholds.rare, 80)
        self.assertEqual(scoring.thresholds.library, 35)
        self.assertEqual(scoring.usage.plays, ((5, 100), (1, 50)))
        self.assertEqual(scoring.usage.users, ((4, 100), (3, 80), (2, 60), (1, 35)))
        with self.assertRaises(AttributeError):
            scoring.thresholds.rare = 1

    def test_rejects_malformed_scoring(self):
        for label, bad in [
            ('unknown top', {'weight': {}}),
            ('unknown nested', {'weights': {'replacment': 0.5}}),
            ('not an object', {'weights': 0.5}),
            ('bool', {'thresholds': {'rare': True}}),
            ('string', {'thresholds': {'rare': '70'}}),
            ('negative', {'streaming': {'ads': -1}}),
            ('nan', {'weights': {'usage': float('nan')}}),
            ('tiers not list', {'replacement': {'tiers': {'20': 0}}}),
            ('empty tiers', {'replacement': {'tiers': []}}),
            ('tier shape', {'replacement': {'tiers': [[20, 0, 1]]}}),
            ('minimums not decreasing', {'replacement': {'tiers': [[10, 15], [20, 0]]}}),
            ('maximums not increasing', {'usage': {'recency_days': [[30, 90], [7, 100]]}}),
            ('library above rare', {'thresholds': {'library': 70}}),
            ('grace order', {'usage': {'grace_full_days': 200}}),
        ]:
            with self.subTest(label), self.assertRaises(SettingsError):
                example(bad)

    def test_tier_helpers(self):
        tiers = ((10, 'ten'), (3, 'three'))
        self.assertEqual([at_least(v, tiers, 'none') for v in (11, 10, 9, 3, 2)],
                         ['ten', 'ten', 'three', 'three', 'none'])
        self.assertEqual([at_most(v, tiers[::-1], 'older') for v in (2, 3, 4, 10, 11)],
                         ['three', 'three', 'ten', 'ten', 'older'])


class TierCharacterizationTests(unittest.TestCase):
    """Candidate tier functions equal the f49052d literals for every input."""

    def pair(self, kind):
        old = load_functions(BASELINE[kind], TIER_FUNCTIONS[kind], example())
        new = load_functions(CANDIDATE[kind], TIER_FUNCTIONS[kind], example())
        return old, new

    def test_count_tiers_match_exhaustively(self):
        for kind in ('movie', 'tv'):
            old, new = self.pair(kind)
            for name in TIER_FUNCTIONS[kind]:
                if name == 'recency_points':
                    continue
                for value in range(0, 61):
                    with self.subTest(kind=kind, function=name, value=value):
                        self.assertEqual(new[name](value), old[name](value))

    def test_recency_tiers_match_across_every_boundary(self):
        days = [d / 4 for d in range(0, 4 * 1000)]
        for limit, _ in DEFAULT_SCORING['usage']['recency_days']:
            days += [limit - 1e-6, limit, limit + 1e-6]
        for kind in ('movie', 'tv'):
            old, new = self.pair(kind)
            self.assertEqual(new['recency_points'](None), old['recency_points'](None))
            for value in days:
                last = NOW - timedelta(days=value)
                with self.subTest(kind=kind, days=value):
                    self.assertEqual(new['recency_points'](last), old['recency_points'](last))

    def test_configured_tiers_take_effect(self):
        settings = example({'replacement': {'tiers': [[5, 10]], 'none': 99},
                            'usage': {'recency_days': [[1, 7]], 'recency_older': 3}})
        for kind in ('movie', 'tv'):
            new = load_functions(CANDIDATE[kind], TIER_FUNCTIONS[kind], settings)
            scarcity = new[TIER_FUNCTIONS[kind][0]]
            self.assertEqual((scarcity(5), scarcity(4)), (10, 99))
            self.assertEqual(new['recency_points'](NOW - timedelta(hours=12)), 7)
            self.assertEqual(new['recency_points'](NOW - timedelta(days=2)), 3)


if __name__ == '__main__':
    unittest.main()
