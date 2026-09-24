"""Planner rows whose dry-run `current` is not a configured category.

dry_run_movies.py / dry_run_tv.py current_bucket() emit "Unknown" when an Arr
path is not <arr_root>/<category folder>/<item> (docs/media-layout.md).
build_move_plan.py resolve_host_source() indexes
TARGETS.category_paths[media_type][current], so such rows need their own
handling; every other row must stay byte-identical to the pinned baseline
tests/fixtures/planner_before_storage_targets.py.
"""
import csv
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from runtime_config import load_config
from storage_targets import load_targets
from migratarr_validation import MoveRequest, ValidationEngine, ValidationPolicy
from migratarr_validation.parity import _csv_requests, load_legacy, run_legacy
from migratarr_validation.planner_parity import compare_planners, csv_bytes


ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / 'tests/fixtures/planner_before_storage_targets.py'
GIB = 1024**3
MOVIE_FIELDS = ['radarr_id', 'title', 'year', 'path', 'current', 'recommended',
                'final_score', 'replacement', 'replacement_confidence', 'decision_reason']
TV_FIELDS = ['sonarr_id', 'title', 'path', 'current', 'recommended',
             'final_score', 'replacement', 'replacement_confidence', 'decision_reason']

WELL_FORMED_MOVIES = [
    # Cross-disk move to the freer Library disk.
    dict(radarr_id=1, title='Alpha', year=2001, path='/media/Movies/Common/Alpha',
         current='Common', recommended='Library'),
    # Rare demotion to Archive with low confidence.
    dict(radarr_id=2, title='Bravo', year=2002, path='/media/Movies/Rare/Bravo',
         current='Rare', recommended='Archive', replacement_confidence='low'),
    # Not on any disk: SOURCE_MISSING via the logical Arr path fallback.
    dict(radarr_id=3, title='Charlie', year=2003, path='/media/Movies/Common/Charlie',
         current='Common', recommended='Library'),
    # On two disks: ambiguous sentinel.
    dict(radarr_id=4, title='Delta', year=2004, path='/media/Movies/Library/Delta',
         current='Library', recommended='Common'),
    # Pre-override loop filters.
    dict(radarr_id=5, title='Hold', year=2005, path='/media/Movies/Common/Hold',
         current='Common', recommended='HOLD'),
    dict(radarr_id=6, title='Same', year=2006, path='/media/Movies/Common/Same',
         current='Common', recommended='Common'),
]
WELL_FORMED_TV = [
    dict(sonarr_id=11, title='Show One', path='/media/TV/Current/Show One',
         current='Current', recommended='Library'),
    dict(sonarr_id=12, title='Show Two', path='/media/TV/Library/Show Two',
         current='Library', recommended='Rare'),
]
# Paths that media_layout.arr_category() cannot place in a category folder.
UNKNOWN_MOVIES = [
    dict(radarr_id=7, title='Echo', year=2007, path='/media/Echo',
         current='Unknown', recommended='Library'),
]
UNKNOWN_TV = [
    dict(sonarr_id=13, title='Show Three', path='/media/TV/Show Three',
         current='Unknown', recommended='Archive'),
]


def native(path):
    return str(Path(path))


class PlannerUnknownCategoryTests(unittest.TestCase):
    def setUp(self):
        self.targets = load_targets(ROOT / 'config/storage-targets.example.json')
        self.runtime = load_config(ROOT / 'config/runtime.example.json', environ={})
        # Baseline compares native Path.parts to mount_root.parts; emulate
        # Linux separator semantics on Windows test hosts.
        self.runtime.mount_root = Path('/mnt/nas')
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        nas = '/mnt/nas/'
        self.existing = {native(nas + p) for p in (
            'media01/Movies/Common/Alpha',
            'media02/Movies/Rare/Bravo',
            'media03/Movies/Library/Delta',
            'media04/Movies/Library/Delta',
            'media01/TV/Current/Show One',
            'media03/TV/Library/Show Two',
        )}
        for media, categories in self.targets.destination_roots.items():
            for roots in categories.values():
                self.existing.update(native(root) for root in roots)
        self.free = {native(nas + 'media03'): 300 * GIB,
                     native(nas + 'media03/Movies/Library'): 300 * GIB,
                     native(nas + 'media03/TV/Library'): 300 * GIB}

    def write_inputs(self, movies, tv, name='inputs'):
        folder = self.base / name
        folder.mkdir()
        movie_csv, tv_csv = folder / 'movie_dry_run.csv', folder / 'tv_dry_run.csv'
        defaults = dict(final_score=50, replacement='yes',
                        replacement_confidence='high', decision_reason='score')
        for path, fields, rows in ((movie_csv, MOVIE_FIELDS, movies),
                                   (tv_csv, TV_FIELDS, tv)):
            with path.open('w', newline='') as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                for row in rows:
                    writer.writerow({**defaults, **row})
        return movie_csv, tv_csv

    def stub(self, namespace):
        namespace['dir_size_bytes'] = lambda p: 2 * GIB
        namespace['free_bytes'] = lambda p: self.free.get(native(p), 500 * GIB)
        return namespace

    def destination_roots(self):
        # Native paths, as build_move_plan.py DESTINATION_ROOTS builds them.
        return {media: {category: tuple(Path(str(root)) for root in roots)
                        for category, roots in categories.items()}
                for media, categories in self.targets.destination_roots.items()}

    def filesystem(self):
        return patch.object(Path, 'exists', lambda p: str(p) in self.existing)

    def parity(self, movie_csv, tv_csv, overrides=None):
        def loader(*args, **kwargs):
            tree, namespace = load_legacy(*args, **kwargs)
            return tree, self.stub(namespace)
        with patch('migratarr_validation.planner_parity.load_legacy', loader), self.filesystem():
            return compare_planners(BASELINE, movie_csv, tv_csv, overrides or {},
                                    self.runtime, self.targets)[0]

    def plans(self, movie_csv, tv_csv, overrides=None, planner=None):
        """Run one planner's candidate loops and cumulative block in memory."""
        kwargs = dict(planner=planner) if planner else dict(targets=self.targets)
        tree, namespace = load_legacy(self.runtime, **kwargs)
        with self.filesystem():
            return tree, run_legacy(tree, self.stub(namespace), movie_csv, tv_csv,
                                    overrides or {})

    def test_well_formed_rows_are_byte_identical_to_baseline(self):
        movie_csv, tv_csv = self.write_inputs(WELL_FORMED_MOVIES, WELL_FORMED_TV)
        result = self.parity(movie_csv, tv_csv)
        self.assertTrue(result['byte_identical'])
        self.assertEqual(result['baseline_rows'], 6)

    def test_well_formed_row_outcomes(self):
        """Pin build_move_plan.py evaluate_move() results for each row shape."""
        movie_csv, tv_csv = self.write_inputs(WELL_FORMED_MOVIES, WELL_FORMED_TV)
        _, rows = self.plans(movie_csv, tv_csv)
        pinned = [(r['title'], r['status'], r['blockers'], r['source_disk'],
                   r['target_disk'], r['transfer_type'], Path(r['source_path']).as_posix())
                  for r in rows]
        self.assertEqual(pinned, [
            ('Alpha (2001)', 'READY_FOR_REVIEW', '', 'media01', 'media04',
             'CROSS_DISK_TRANSFER', '/mnt/nas/media01/Movies/Common/Alpha'),
            ('Bravo (2002)', 'READY_FOR_REVIEW', '', 'media02', 'media04',
             'CROSS_DISK_TRANSFER', '/mnt/nas/media02/Movies/Rare/Bravo'),
            ('Charlie (2003)', 'BLOCKED', 'SOURCE_MISSING', '', 'media04',
             'CROSS_DISK_TRANSFER', '/media/Movies/Common/Charlie'),
            ('Delta (2004)', 'BLOCKED', 'SOURCE_MISSING', '', 'media01',
             'CROSS_DISK_TRANSFER', '/__MIGRATARR_AMBIGUOUS__/Movie/Library/Delta'),
            ('Show One', 'READY_FOR_REVIEW', '', 'media01', 'media04',
             'CROSS_DISK_TRANSFER', '/mnt/nas/media01/TV/Current/Show One'),
            ('Show Two', 'READY_FOR_REVIEW', '', 'media03', 'media02',
             'CROSS_DISK_TRANSFER', '/mnt/nas/media03/TV/Library/Show Two'),
        ])

    def test_unknown_source_category_is_blocked_without_probing_disks(self):
        movie_csv, tv_csv = self.write_inputs(UNKNOWN_MOVIES, UNKNOWN_TV)
        probed = []
        real = lambda p: str(p) in self.existing
        with patch.object(Path, 'exists', lambda p: probed.append(str(p)) or real(p)):
            tree, namespace = load_legacy(self.runtime, targets=self.targets)
            rows = run_legacy(tree, self.stub(namespace), movie_csv, tv_csv, {})
        self.assertEqual(rows[0], {
            'media_type': 'Movie', 'title': 'Echo (2007)', 'current': 'Unknown',
            'scored_recommendation': 'Library', 'recommended': 'Library',
            'override_type': '', 'override_tag': '',
            'source_path': '/media/Echo', 'target_path': '',
            'size_gb': '', 'destination_free_gb': '', 'free_after_move_gb': '',
            'source_disk': '', 'target_disk': '', 'transfer_type': '',
            'final_score': '50', 'replacement': 'yes', 'replacement_confidence': 'high',
            'decision_reason': 'score', 'arr_path_update_required': 'YES',
            'status': 'BLOCKED', 'blockers': 'SOURCE_CATEGORY_UNKNOWN', 'warnings': '',
        })
        self.assertEqual([(r['title'], r['status'], r['blockers']) for r in rows], [
            ('Echo (2007)', 'BLOCKED', 'SOURCE_CATEGORY_UNKNOWN'),
            ('Show Three', 'BLOCKED', 'SOURCE_CATEGORY_UNKNOWN'),
        ])
        self.assertFalse([p for p in probed if 'Echo' in p or 'Show Three' in p])
        # Blocked rows reserve nothing and sample no free space.
        self.assertEqual(namespace['PROJECTED_FREE'], {})

    def test_non_unknown_rows_stay_byte_identical_with_unknown_rows_present(self):
        movie_csv, tv_csv = self.write_inputs(
            WELL_FORMED_MOVIES[:2] + UNKNOWN_MOVIES + WELL_FORMED_MOVIES[2:],
            UNKNOWN_TV + WELL_FORMED_TV)
        old_tree, old_rows = self.plans(movie_csv, tv_csv, planner=BASELINE)
        new_tree, new_rows = self.plans(movie_csv, tv_csv)
        known = lambda rows: [r for r in rows if r['current'] != 'Unknown']
        self.assertEqual(len(known(new_rows)), 6)
        self.assertEqual(csv_bytes(old_tree, known(old_rows)),
                         csv_bytes(new_tree, known(new_rows)))
        self.assertEqual([r['blockers'] for r in new_rows if r['current'] == 'Unknown'],
                         ['SOURCE_CATEGORY_UNKNOWN'] * 2)

    def test_overrides_on_unknown_source_category(self):
        movie_csv, tv_csv = self.write_inputs(UNKNOWN_MOVIES, UNKNOWN_TV)
        overrides = {'Movie': {7: {'migratarr-lock'}},
                     'TV': {13: {'migratarr-rare', 'migratarr-archive'}}}
        _, rows = self.plans(movie_csv, tv_csv, overrides)
        # A lock keeps current == recommended, so the loop drops the row
        # (the baseline raised KeyError on DESTINATION_ROOTS["Unknown"]).
        self.assertEqual([(r['title'], r['blockers']) for r in rows], [
            ('Show Three', 'CONFLICTING_MANUAL_OVERRIDES;SOURCE_CATEGORY_UNKNOWN'),
        ])
        _, rows = self.plans(movie_csv, tv_csv, {'Movie': {7: {'migratarr-rare'}}})
        self.assertEqual((rows[0]['recommended'], rows[0]['override_type'],
                          rows[0]['blockers'], rows[0]['warnings']),
                         ('Rare', 'CATEGORY', 'SOURCE_CATEGORY_UNKNOWN',
                          'MANUAL_CATEGORY_OVERRIDE'))

    def test_engine_matches_planner_including_unknown_rows(self):
        movie_csv, tv_csv = self.write_inputs(
            WELL_FORMED_MOVIES + UNKNOWN_MOVIES, UNKNOWN_TV + WELL_FORMED_TV)
        overrides = {'TV': {13: {'migratarr-rare', 'migratarr-archive'}}}
        _, planner_rows = self.plans(movie_csv, tv_csv, overrides)
        policy = ValidationPolicy(
            self.destination_roots(),
            tuple(Path(str(t.path)) for t in self.targets.targets),
            category_paths=self.targets.category_paths)
        engine = ValidationEngine(policy, exists=lambda p: str(p) in self.existing,
                                  size_bytes=lambda p: 2 * GIB,
                                  free_bytes=lambda p: self.free.get(native(p), 500 * GIB),
                                  overrides=overrides)
        with self.filesystem():
            engine_rows = engine.evaluate_candidates(
                _csv_requests(movie_csv, tv_csv, policy.destination_roots))
            engine.apply_cumulative_capacity(engine_rows)
        self.assertEqual(engine_rows, planner_rows)
        self.assertEqual(sum(r['current'] == 'Unknown' for r in engine_rows), 2)

    def test_baseline_blocked_unknown_rows_as_source_missing(self):
        # The pre-storage-targets planner searched <disk>/Movies/Unknown/<name>,
        # found nothing and fell back to the Arr path.
        movie_csv, tv_csv = self.write_inputs(UNKNOWN_MOVIES, UNKNOWN_TV)
        _, rows = self.plans(movie_csv, tv_csv, planner=BASELINE)
        self.assertEqual([(r['title'], r['status'], r['blockers'],
                           Path(r['source_path']).as_posix()) for r in rows], [
            ('Echo (2007)', 'BLOCKED', 'SOURCE_MISSING', '/media/Echo'),
            ('Show Three', 'BLOCKED', 'SOURCE_MISSING', '/media/TV/Show Three'),
        ])

    def test_engine_without_category_paths_keeps_legacy_source_missing(self):
        # No configured categories: the engine's Movies/<current> search is
        # the baseline's, so an Unknown row still falls through to the Arr path.
        policy = ValidationPolicy(
            self.destination_roots(),
            tuple(Path(str(t.path)) for t in self.targets.targets))
        engine = ValidationEngine(policy, exists=lambda p: str(p) in self.existing,
                                  size_bytes=lambda p: GIB, free_bytes=lambda p: 500 * GIB)
        plan = engine.evaluate(MoveRequest('Movie', 7, 'Echo (2007)', '/media/Echo',
                                           'Unknown', 'Library'))
        self.assertEqual(plan['blockers'], 'SOURCE_MISSING')


if __name__ == '__main__':
    unittest.main()
