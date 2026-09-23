import contextlib
import csv
import io
import json
from pathlib import Path
import tempfile
import unittest

from migratarr_validation.dry_run_parity import compare, record
from synthetic_library import NOW, offline_world


ROOT = Path(__file__).resolve().parents[1]
BASELINE = {'movie': ROOT / 'tests/fixtures/dry_run_movies_before_scoring_settings.py',
            'tv': ROOT / 'tests/fixtures/dry_run_tv_before_scoring_settings.py'}


class DryRunParityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)
        self.world = offline_world()
        self.addCleanup(self.world.close)

    def recording(self, kind):
        out = self.dir / f'{kind}.json'
        with contextlib.redirect_stderr(io.StringIO()):  # planner progress and retry chatter
            summary = record(kind, out, cache_dir=self.dir / 'cache', now=NOW)
        return out, summary

    def test_recording_holds_no_api_keys(self):
        for kind in ('movie', 'tv'):
            with self.subTest(kind=kind):
                out, summary = self.recording(kind)
                self.assertGreater(summary['requests'] + summary['cached'], 10)
                self.assertNotIn('fake-key', out.read_text(encoding='utf-8'))

    def test_synthetic_library_reaches_every_placement(self):
        expected = {'movie': {'Rare', 'Archive', 'Library', 'Common', 'HOLD'},
                    'tv': {'Rare', 'Archive', 'Library', 'Current', 'HOLD'}}
        for kind in ('movie', 'tv'):
            with self.subTest(kind=kind):
                out, _ = self.recording(kind)
                result = compare(out, BASELINE[kind])
                self.assertTrue(result['byte_identical'], json.dumps(result, indent=2))
                self.assertEqual(result['recording_reproduced'], {'baseline': True, 'candidate': True})
                rows = self.replay_rows(out, kind)
                self.assertEqual({r['recommended'] for r in rows}, expected[kind])

    def replay_rows(self, out, kind):
        from migratarr_validation.dry_run_parity import replay
        data, misses = replay(ROOT / f'dry_run_{"movies" if kind == "movie" else "tv"}.py',
                              json.loads(out.read_text(encoding='utf-8')))
        self.assertEqual(misses, [])
        return list(csv.DictReader(io.StringIO(data.decode('utf-8'))))

    def test_replay_is_offline_and_frozen(self):
        out, summary = self.recording('movie')
        self.world.close()
        self.world = offline_world(network=False)  # any HTTP or Docker call now raises
        rows_again = compare(out, BASELINE['movie'])
        self.assertTrue(rows_again['byte_identical'])
        self.assertEqual(rows_again['candidate_csv_sha256'], summary['csv_sha256'])

    def test_changed_scoring_is_reported_row_by_row(self):
        out, _ = self.recording('movie')
        changed = self.dir / 'changed.py'
        source = BASELINE['movie'].read_text(encoding='utf-8')
        self.assertEqual(source.count('elif final >= 70:'), 1)
        changed.write_text(source.replace('elif final >= 70:', 'elif final >= 40:'), encoding='utf-8')
        result = compare(out, BASELINE['movie'], changed)
        self.assertFalse(result['byte_identical'])
        self.assertGreater(result['differences']['rows_changed'], 0)
        self.assertIn('recommended', result['differences']['sample'][0]['changed'])

    def test_planner_json_scoring_changes_whole_script_output(self):
        from unittest.mock import patch
        out, _ = self.recording('movie')
        config = json.loads((ROOT / 'config/planner.example.json').read_text(encoding='utf-8'))
        config['scoring'] = {'thresholds': {'rare': 40, 'library': 20}}
        custom = self.dir / 'planner.json'
        custom.write_text(json.dumps(config), encoding='utf-8')
        with patch.dict('os.environ', {'MIGRATARR_PLANNER_CONFIG': str(custom)}):
            result = compare(out, BASELINE['movie'])
        self.assertFalse(result['byte_identical'])
        changed = {row['title']: row['changed'] for row in result['differences']['sample']}
        # Just Added scored 41.0: Library at the default 70/35, Rare at 40.
        self.assertEqual(changed['Just Added']['recommended'], ['Library', 'Rare'])
        self.assertEqual(changed['Just Added']['decision_reason'],
                         ['Placement score 35-69.9', 'Placement score >=40'])

    def test_request_missing_from_recording_fails_parity(self):
        out, _ = self.recording('movie')
        data = json.loads(out.read_text(encoding='utf-8'))
        dropped = next(k for k in data['cached'] if k.startswith('tmdb_provider|'))
        del data['cached'][dropped]
        out.write_text(json.dumps(data), encoding='utf-8')
        result = compare(out, BASELINE['movie'])
        self.assertFalse(result['byte_identical'])
        self.assertEqual(result['replay_misses']['baseline'], ['cached:' + dropped])


if __name__ == '__main__':
    unittest.main()
