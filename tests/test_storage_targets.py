import copy
import json
from pathlib import Path
import tempfile
import unittest

from migratarr_validation.config import parse_policy
from storage_targets import load_targets, parse_targets, TargetState


ROOT = Path(__file__).resolve().parents[1]


class StorageTargetTests(unittest.TestCase):
    def setUp(self):
        self.data = json.loads((ROOT / 'config/storage-targets.example.json').read_text())

    def test_existing_configuration_parity(self):
        model = parse_targets(self.data)
        legacy = json.loads((ROOT / 'config/legacy-storage.json').read_text())
        policy = parse_policy(legacy)
        runtime = json.loads((ROOT / 'config/runtime.example.json').read_text())
        self.assertEqual({t.id: {'local_path': str(t.path), 'remote_path': str(t.remote_path)}
                          for t in model.targets}, runtime['storage'])
        self.assertEqual(tuple(Path(str(p)) for p in model.source_roots), policy.source_roots)
        self.assertEqual({m: {c: tuple(Path(str(p)) for p in ps) for c, ps in cats.items()}
                          for m, cats in model.destination_roots.items()}, policy.destination_roots)
        self.assertEqual({m: {c: Path(str(p)) for c, p in cats.items()}
                          for m, cats in model.category_paths.items()}, policy.category_paths)
        self.assertTrue(all(t.minimum_free_space_gb * 1024**3 == policy.min_free_after_bytes
                            for t in model.targets))

    def test_fifth_target_and_disabled_source(self):
        fifth = dict(id='media05', name='Fifth disk', path='/mnt/nas/media05',
                     enabled=True, media_types=['Movie'])
        self.data['targets'].append(fifth)
        self.data['placement']['Movie']['Library'].append('media05')
        model = parse_targets(self.data)
        self.assertIsNone(model.targets[-1].remote_path)
        self.assertEqual(str(model.destination_roots['Movie']['Library'][-1]), '/mnt/nas/media05/Movies/Library')
        self.data['placement']['Movie']['Library'].remove('media05')
        fifth['enabled'] = False
        model = parse_targets(self.data)
        self.assertIn(model.targets[-1].path, model.source_roots)
        self.assertEqual(model.physical_target('/mnt/nas/media05/Movies/X').id, 'media05')
        self.assertIsNone(model.physical_target('/mnt/nas/media050/Movies/X'))

    def test_plan_only_paths_and_stable_ids(self):
        for t in self.data['targets']:
            del t['remote_path']
            t['path'] = '/data/' + t['id'] + '/library'
        model = parse_targets(self.data)
        self.assertEqual(model.physical_target('/data/media01/library/X').id, 'media01')

    def test_defaults_overrides_and_inert_fields(self):
        t = self.data['targets'][0]
        del t['minimum_free_space_gb']
        self.data['defaults']['minimum_free_space_gb'] = 99
        t.update(priority=-10, storage_class='HOT', tags=['ssd'])
        model = parse_targets(self.data)
        self.assertEqual(model.targets[0].minimum_free_space_gb, 99)
        self.assertEqual(model.targets[1].minimum_free_space_gb, 50)
        self.assertEqual(model.placement['Movie']['Library'], ('media03', 'media04'))

    def test_invalid_target_fields(self):
        cases = {'id': ['UPPER', 'x'*33, 3], 'name': ['', None],
                 'enabled': [1, 'yes'], 'path': ['/mnt/nas/../media01', '/mnt//nas/media01',
                 '/mnt/nas/media01/', '/mnt/nas/media01\n', 'relative', '/a/b/wrong'],
                 'remote_path': [None, '/too/deep/root'], 'media_types': [['Movie', 'Movie'], ['Music'], 'Movie', [{}]],
                 'minimum_free_space_gb': [-1, True, 1.5], 'storage_class': ['OTHER', []],
                 'priority': [True, '100'], 'tags': [['a', 'a'], [None], 'a']}
        for field, values in cases.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    data = copy.deepcopy(self.data)
                    data['targets'][0][field] = value
                    with self.assertRaises(ValueError):
                        parse_targets(data)

    def test_invalid_relationships(self):
        for change in (
            lambda d: d['targets'].append(copy.deepcopy(d['targets'][0])),
            lambda d: d['targets'][1].update(path=d['targets'][0]['path']),
            lambda d: d['targets'][1].update(path='/mnt/nas/media01/nested'),
            lambda d: d['targets'][1].update(path='/other/nas/media02'),
            lambda d: d['targets'][1].update(remote_path=d['targets'][0]['remote_path']),
            lambda d: d['targets'][0].update(enabled=False),
            lambda d: d['targets'][0].update(media_types=['TV']),
            lambda d: d['placement']['Movie'].update(Common=['unknown']),
            lambda d: d['placement']['Movie'].update(Common=['media01', 'media01']),
            lambda d: d['placement']['Movie'].update(Unknown=['media01']),
            lambda d: d['category_paths']['Movie'].update(Common='../escape'),
            lambda d: d['category_paths']['Movie'].update(Common='Movies/Rare'),
        ):
            data = copy.deepcopy(self.data)
            change(data)
            with self.subTest(data=data), self.assertRaises(ValueError):
                parse_targets(data)

    def test_exact_schema_and_observed_values_not_configurable(self):
        for location in ((), ('defaults',), ('targets', 0)):
            for key in ('capacity', 'free_space', 'typo'):
                data = copy.deepcopy(self.data)
                obj = data
                for part in location:
                    obj = obj[part]
                obj[key] = 100
                with self.subTest(location=location, key=key), self.assertRaises(ValueError):
                    parse_targets(data)
        for version in (True, 2, '1'):
            self.data['schema_version'] = version
            with self.assertRaises(ValueError):
                parse_targets(self.data)

    def test_duplicate_json_fields_and_explicit_loader(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'targets.json'
            path.write_text(json.dumps(self.data))
            self.assertEqual(load_targets(path), parse_targets(self.data))
            for raw in ('{"schema_version":1,"schema_version":1}',
                        '{"defaults":{"minimum_free_space_gb":50,"minimum_free_space_gb":50}}'):
                path.write_text(raw)
                with self.assertRaisesRegex(ValueError, 'duplicate JSON field'):
                    load_targets(path)

    def test_observed_state(self):
        self.assertEqual(TargetState('media01', 100, 50).free_space, 50)
        for capacity, free in ((-1, 0), (100, 101), (100, -1), (True, 0), (100, 1.5)):
            with self.subTest(capacity=capacity, free=free), self.assertRaises(ValueError):
                TargetState('media01', capacity, free)
