import copy
import csv
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from runtime_config import load_config
from storage_targets import load_targets, parse_targets
from migratarr_validation.parity import load_legacy
from migratarr_validation.planner_parity import compare_planners


ROOT = Path(__file__).resolve().parents[1]
GIB = 1024**3


class PlannerTargetsTests(unittest.TestCase):
    def setUp(self):
        self.targets = load_targets(ROOT / 'config/storage-targets.example.json')
        self.runtime = load_config(ROOT / 'config/runtime.example.json', environ={})
        # Original planner compares native Path.parts to mount_root.parts;
        # emulate its Linux separator semantics on Windows test hosts.
        self.runtime.mount_root = Path('/mnt/nas')
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.movie, self.tv = base / 'movies.csv', base / 'tv.csv'
        fields = ['radarr_id', 'title', 'year', 'path', 'current', 'recommended',
                  'final_score', 'replacement', 'replacement_confidence', 'decision_reason']
        with self.movie.open('w', newline='') as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerow(dict(radarr_id=1, title='Film', year=2020, path='/media/Movies/Common/Film',
                                 current='Common', recommended='Library', final_score=90,
                                 replacement='yes', replacement_confidence='high', decision_reason='score'))
        self.tv.write_text('sonarr_id,title,path,current,recommended\n')
        self.facts = {'exists': {}, 'resolve': {}, 'size': {}, 'free': {}}
        def put(kind, path, value):
            self.facts[kind][str(Path(path))] = value
        for n in range(1, 5):
            root = f'/mnt/nas/media0{n}'
            put('exists', root + '/Movies/Common/Film', n == 1)
            put('free', root, 500 * GIB)
        for n in (3, 4):
            root = f'/mnt/nas/media0{n}/Movies/Library'
            put('exists', root, True)
            put('exists', root + '/Film', False)
            put('free', root, 500 * GIB)
        put('size', '/mnt/nas/media01/Movies/Common/Film', GIB)

    def compare(self, facts=None, targets=None):
        return compare_planners(ROOT / 'tests/fixtures/planner_before_storage_targets.py',
                                self.movie, self.tv, {}, self.runtime, targets or self.targets,
                                self.facts if facts is None else facts)[0]

    def test_frozen_observations_produce_identical_csv_bytes(self):
        with patch('os.walk', side_effect=AssertionError('live probe')):
            result = self.compare()
        self.assertTrue(result['byte_identical'])
        self.assertEqual(result['baseline_rows'], 1)
        self.assertEqual(result['baseline_csv_sha256'], result['candidate_csv_sha256'])

    def test_missing_frozen_observation_refuses(self):
        facts = copy.deepcopy(self.facts)
        facts['size'].clear()
        with self.assertRaisesRegex(ValueError, 'Missing frozen observation'):
            self.compare(facts)

    def test_changed_reserve_is_detected_in_bytes(self):
        data = json.loads((ROOT / 'config/storage-targets.example.json').read_text())
        data['targets'][2]['minimum_free_space_gb'] = 600
        result = self.compare(targets=parse_targets(data))
        self.assertFalse(result['byte_identical'])

    def test_fifth_target_resolves_and_plans_without_runtime_disk_entry(self):
        data = json.loads((ROOT / 'config/storage-targets.example.json').read_text())
        data['targets'].append(dict(id='media05', name='Fifth', path='/mnt/nas/media05',
                                    enabled=True, media_types=['Movie'], minimum_free_space_gb=75))
        data['placement']['Movie']['Library'] = ['media05']
        _, ns = load_legacy(self.runtime, parse_targets(data))
        source = Path('/mnt/nas/media01/Movies/Common/Film')
        destination = Path('/mnt/nas/media05/Movies/Library')
        ns.update(ARR_OVERRIDES={}, dir_size_bytes=lambda p: GIB, free_bytes=lambda p: 500*GIB)
        with patch.object(Path, 'exists', lambda p: p in (source, destination)):
            plan = ns['evaluate_move']('Movie', 1, 'Film', '/media/Movies/Common/Film',
                                       'Common', 'Library', 90, 'yes', 'high', 'score')
        self.assertEqual(plan['status'], 'READY_FOR_REVIEW')
        self.assertEqual(plan['target_disk'], 'media05')
        self.assertEqual(ns['PROJECTED_FREE']['media05'], 499 * GIB)
        self.assertEqual(ns['minimum_free_bytes']('media05'), 75 * GIB)

    def test_configured_category_source_and_disabled_target(self):
        data = json.loads((ROOT / 'config/storage-targets.example.json').read_text())
        data['targets'][0]['enabled'] = False
        data['placement']['Movie']['Common'] = ['media02']
        data['placement']['TV']['Current'] = ['media02']
        data['category_paths']['Movie']['Common'] = 'Films/Popular'
        _, ns = load_legacy(self.runtime, parse_targets(data))
        source = Path('/mnt/nas/media01/Films/Popular/Film')
        with patch.object(Path, 'exists', lambda p: p == source):
            self.assertEqual(ns['resolve_host_source']('Movie', '/media/Movies/Common/Film', 'Common'), source)
        self.assertEqual(ns['physical_disk']('/mnt/nas/media010/Film'), '')

    def test_cumulative_reserve_and_order_remain_byte_identical(self):
        facts = copy.deepcopy(self.facts)
        for key in facts['free']:
            facts['free'][key] = 49 * GIB
        self.assertTrue(self.compare(facts)['byte_identical'])
