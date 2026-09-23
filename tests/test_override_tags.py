from itertools import chain, combinations
import json
from pathlib import Path
import unittest

from planner_settings import CATEGORIES, DEFAULT_OVERRIDES, SettingsError, load_settings, parse_settings
from migratarr_validation.rules import load_rule_policy


ROOT = Path(__file__).resolve().parents[1]
LEGACY_SET = {'migratarr-common', 'migratarr-rare', 'migratarr-library', 'migratarr-archive', 'migratarr-current'}


# The inline check every executor carried at 6dc21bd, verbatim, e.g.
# execute_cross_movie.py:345-347, execute_cross_tv.py:374-376,
# execute_movie.py:169-171, execute_movie_nas.py:216-218,
# execute_tv_nas.py:257-261, recover_cross_0102.py:65-68, recover_cross_0116.py:64-67.
def legacy_locked(tags):
    return 'migratarr-lock' in tags


def legacy_agrees(tags, recommended):
    overrides = tags & LEGACY_SET
    return not overrides or overrides == {'migratarr-' + recommended.lower()}


def subsets(values):
    values = sorted(values)
    return chain.from_iterable(combinations(values, n) for n in range(len(values) + 1))


def settings_with(overrides):
    data = json.loads((ROOT / 'config/planner.example.json').read_text(encoding='utf-8'))
    if overrides is not None:
        data['overrides'] = overrides
    return parse_settings(data)


class OverrideTagCharacterizationTests(unittest.TestCase):
    def test_default_tags_match_legacy_executor_check_exhaustively(self):
        tags = settings_with(None).overrides
        universe = LEGACY_SET | {'migratarr-lock', 'unrelated', ''}
        recommended = [*CATEGORIES, *(c.lower() for c in CATEGORIES), 'HOLD', 'Unknown', '']
        cases = 0
        for subset in subsets(universe):
            present = set(subset)
            self.assertEqual(tags.locked(present), legacy_locked(present), present)
            for value in recommended:
                self.assertEqual(tags.agrees(present, value), legacy_agrees(present, value), (present, value))
                cases += 1
        self.assertEqual(cases, 2 ** len(universe) * len(recommended))

    def test_planner_and_validation_defaults_agree(self):
        planner = load_settings(ROOT / 'config/planner.example.json').overrides
        rules = load_rule_policy(ROOT / 'config/legacy-rules.json')
        self.assertEqual(planner.lock_tag, rules.lock_tag)
        self.assertEqual(dict(planner.category_tags), dict(rules.category_override_tags))
        self.assertEqual(dict(planner.category_tags), DEFAULT_OVERRIDES['category_tags'])

    def test_configured_tags_replace_the_defaults(self):
        tags = settings_with({'lock_tag': 'keep', 'category_tags': {'vault': 'Rare', 'shelf': 'Library'}}).overrides
        self.assertTrue(tags.locked({'keep'}))
        self.assertFalse(tags.locked({'migratarr-lock'}))
        self.assertTrue(tags.agrees({'vault'}, 'Rare'))
        self.assertFalse(tags.agrees({'vault'}, 'Library'))
        self.assertFalse(tags.agrees({'vault', 'shelf'}, 'Rare'))
        self.assertTrue(tags.agrees({'migratarr-archive'}, 'Rare'))

    def test_rejects_malformed_overrides(self):
        good = {'lock_tag': 'keep', 'category_tags': {'vault': 'Rare'}}
        for label, bad in [
            ('missing lock', {'category_tags': {'vault': 'Rare'}}),
            ('extra field', dict(good, extra=1)),
            ('uppercase tag', dict(good, lock_tag='Keep')),
            ('spaced tag', dict(good, category_tags={'my vault': 'Rare'})),
            ('separator', dict(good, lock_tag='a,b')),
            ('empty map', dict(good, category_tags={})),
            ('unknown category', dict(good, category_tags={'vault': 'Vault'})),
            ('lowercase category', dict(good, category_tags={'vault': 'rare'})),
            ('duplicate category', dict(good, category_tags={'vault': 'Rare', 'gem': 'Rare'})),
            ('lock is category', dict(good, category_tags={'keep': 'Rare'})),
        ]:
            with self.subTest(label), self.assertRaises(SettingsError):
                settings_with(bad)


if __name__ == '__main__':
    unittest.main()
