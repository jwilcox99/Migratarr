"""Old fixed-depth path checks vs media_layout, over generated good and bad paths.

Baselines are the executors and planners at e11681b, pinned in tests/fixtures/
*_before_media_layout.py. paths() and remote_path() run from those pinned
sources; the NAS-side checks are transcribed verbatim (Path -> PurePosixPath,
identical on the Linux NAS) because they only exist inside shipped programs.
"""
import ast
import contextlib
import importlib
import json
from itertools import product
from pathlib import Path, PurePosixPath
import unittest
from unittest.mock import patch

from media_layout import MediaLayout, split_media_path
from migratarr_validation import layout_parity
from migratarr_validation.layout_parity import old_nas_cross, old_nas_same, outcome
from planner_settings import load_settings
from runtime_config import load_config
from storage_targets import load_targets


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / 'tests/fixtures'
RUNTIME = load_config(ROOT / 'config/runtime.example.json', environ={})
TARGETS = load_targets(ROOT / 'config/storage-targets.example.json')
DISKS = sorted(RUNTIME.storage)
MEDIA = {'Movie': ('Movies', ('Common', 'Rare', 'Library', 'Archive')),
         'TV': ('TV', ('Current', 'Rare', 'Library', 'Archive'))}
EXECUTORS = {  # name: (media, cross-disk)
    'execute_movie': ('Movie', False), 'execute_movie_nas': ('Movie', False),
    'execute_tv_nas': ('TV', False), 'execute_cross_movie': ('Movie', True),
    'execute_cross_tv': ('TV', True)}


def new_modules():
    settings = load_settings(ROOT / 'config/planner.example.json')
    with patch('runtime_config.get_config', return_value=RUNTIME), \
            patch('planner_settings.get_settings', return_value=settings), \
            patch('media_layout.get_targets', return_value=TARGETS):
        return {name: importlib.import_module(name) for name in EXECUTORS}


def local(disk, media, category, name='Example Title (2001)'):
    return f'/mnt/nas/{disk}/{MEDIA[media][0]}/{category}/{name}'


def path_variants(media, disk, category, name='Example Title (2001)'):
    """A valid local path and malformed neighbours of it."""
    good = local(disk, media, category, name)
    other_media = 'TV' if media == 'Movie' else 'Movies'
    return [good, good + '/extra', str(PurePosixPath(good).parent), good + '/',
            good.replace('/mnt/nas/', '/mnt/nas//'), good[1:], good.replace('/mnt/nas/', '/mnt/other/'),
            good.replace(f'/{disk}/', '/media09/'), f'/mnt/nas/{disk}',
            good.replace(f'/{MEDIA[media][0]}/', f'/{other_media}/'),
            good.replace(f'/{category}/', f'/{category.lower()}/'), good.replace(f'/{category}/', '/Vault/'),
            good.replace(f'/{category}/', f'/{category}/../{category}/'),
            good.replace(f'/{category}/', f'/{category}/./'), '//' + good.lstrip('/'),
            local(disk, media, category, '.hidden'), local(disk, media, category, 'A  B')]


def rows(media, cross):
    cats = MEDIA[media][1]
    for sd, td, cur, rec in product(DISKS, DISKS, cats, cats):
        if (sd != td) != cross:
            continue
        base = dict(source_path=local(sd, media, cur), target_path=local(td, media, rec),
                    current=cur, recommended=rec, source_disk=sd, target_disk=td)
        yield base
        if (sd, cur) != (DISKS[0], cats[0]) and (sd, rec) != (DISKS[1], cats[1]):
            continue  # full mutation set on a representative subset
        for field, disk_field, cat_field, disk, cat in (
                ('source_path', 'source_disk', 'current', sd, cur),
                ('target_path', 'target_disk', 'recommended', td, rec)):
            for variant in path_variants(media, disk, cat):
                yield dict(base, **{field: variant})
            for other in DISKS:
                yield dict(base, **{disk_field: other})
            for other in (*cats, 'Vault', cat.lower()):
                yield dict(base, **{cat_field: other})
        yield dict(base, target_path=local(td, media, rec, 'Other Name'))
        yield dict(base, target_path=base['source_path'])


class ExecutorPathParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.new = new_modules()

    def test_paths_accept_refuse_and_derive_identically(self):
        for name, (media, cross) in EXECUTORS.items():
            old = layout_parity.old_functions(name, RUNTIME)
            accepted = total = 0
            for row in rows(media, cross):
                total += 1
                expected, actual = outcome(old['paths'], row), outcome(self.new[name].paths, row)
                with self.subTest(executor=name, row=row):
                    self.assertEqual(actual, expected)
                if expected[0] == 'ok':
                    accepted += 1
                    if name == 'execute_movie':
                        continue  # local-only executor, no NAS mapping
                    for path in expected[1][:2]:
                        # The old code did PurePosixPath(str(path)); on the Linux host
                        # str() of a Path is its POSIX form, so compare with that.
                        path = path.as_posix()
                        self.assertEqual(outcome(self.new[name].remote_path, path),
                                         outcome(old['remote_path'], path))
            self.assertGreater(accepted, 10, name)
            self.assertGreater(total - accepted, 50, name)

    def test_remote_path_never_accepts_what_the_old_one_refused(self):
        for name, (media, _) in EXECUTORS.items():
            if name == 'execute_movie':
                continue  # local-only executor, no NAS mapping
            old = layout_parity.old_functions(name, RUNTIME)
            for disk, category in product(DISKS, MEDIA[media][1]):
                for variant in path_variants(media, disk, category):
                    new_result = outcome(self.new[name].remote_path, variant)
                    if new_result[0] == 'ok':
                        with self.subTest(executor=name, path=variant):
                            self.assertEqual(new_result, outcome(old['remote_path'], variant))


def remote_variants(media):
    layout = MediaLayout(RUNTIME, TARGETS)
    for disk, category in product(DISKS, MEDIA[media][1]):
        for variant in path_variants(media, disk, category):
            if variant.startswith('/mnt/nas/') and '//' not in variant:
                suffix = variant[len('/mnt/nas/'):].split('/', 1)
                root = RUNTIME.remote_disks.get(suffix[0], '/volume9/' + suffix[0])
                yield disk, (root + '/' + suffix[1]) if len(suffix) > 1 else root
            else:
                yield disk, variant
        good = layout.to_remote(local(disk, media, category), media)
        yield disk, '/' + good
        yield disk, good.replace('/volume', '/Volume')


class NasCheckParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.new = new_modules()

    def test_cross_disk_nas_checks_match(self):
        for name, media in (('execute_cross_movie', 'Movie'), ('execute_cross_tv', 'TV')):
            module = self.new[name]
            candidates = [p for _, p in remote_variants(media)]
            pairs = [(a, b) for a in candidates[::3] for b in candidates[1::3]]
            accepted = 0
            for source, destination in pairs:
                expected = outcome(old_nas_cross, source, destination, media, RUNTIME.remote_disks)
                actual = outcome(module.nas_pair, source, destination, module.remote_layout())
                with self.subTest(executor=name, source=source, destination=destination):
                    self.assertEqual(actual[0], expected[0])
                    if expected[0] == 'refused':
                        self.assertEqual(actual[1], expected[1])
                accepted += expected[0] == 'ok'
            self.assertGreater(accepted, 20, name)

    def test_same_disk_nas_checks_match(self):
        for name, media in (('execute_movie_nas', 'Movie'), ('execute_tv_nas', 'TV')):
            module = self.new[name]
            by_disk = {}
            for disk, path in remote_variants(media):
                by_disk.setdefault(disk, []).append(path)
            accepted = 0
            for disk, candidates in by_disk.items():
                root = RUNTIME.remote_disks[disk]
                for source, destination in product(candidates[::2], candidates[1::2]):
                    expected = outcome(old_nas_same, source, destination, root, media)
                    actual = outcome(module.nas_pair, source, destination, module.remote_layout(disk))
                    with self.subTest(executor=name, source=source, destination=destination):
                        self.assertEqual(actual[0], expected[0])
                        if expected[0] == 'refused':
                            self.assertEqual(actual[1], expected[1])
                    accepted += expected[0] == 'ok'
            self.assertGreater(accepted, 20, name)


def planner_current_bucket(path, layout=None):
    tree = ast.parse(path.read_text(encoding='utf-8'))
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'current_bucket']
    namespace = {'LAYOUT': layout}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), 'exec'), namespace)
    return namespace['current_bucket']


class PlannerCategoryTests(unittest.TestCase):
    def test_well_formed_arr_paths_match_and_malformed_become_unknown(self):
        layout = MediaLayout(RUNTIME, TARGETS)
        for script, media in (('dry_run_movies', 'Movie'), ('dry_run_tv', 'TV')):
            old = planner_current_bucket(FIXTURE / f'{script}_before_media_layout.py')
            new = planner_current_bucket(ROOT / f'{script}.py', layout)
            folder, cats = MEDIA[media]
            for category in cats:
                good = f'/media/{folder}/{category}/Some Title'
                with self.subTest(path=good):
                    self.assertEqual(new(good), old(good))
                    self.assertEqual(new(good), category)
            # The old substring search also claimed paths no executor accepts;
            # the new one reports them Unknown. Listed so any other difference fails.
            loose = [f'/media/{folder}/{cats[1]}/Collection/Title', f'/media/{folder.lower()}/{cats[1].lower()}/T',
                     f'/data/{cats[1]}/Title']
            for path in loose:
                with self.subTest(path=path):
                    self.assertNotEqual(old(path), 'Unknown')
                    self.assertEqual(new(path), 'Unknown')
            for path in ('', None, '/media/Other/Title', f'/media/{folder}/Vault/Title',
                         f'/media/{folder}/{cats[1]}'):
                with self.subTest(path=path):
                    self.assertEqual(new(path), old(path))
                    self.assertEqual(new(path), 'Unknown')


class LayoutTests(unittest.TestCase):
    def test_split_media_path(self):
        roots = {'a': '/srv/pool/disk-a', 'b': '/data'}
        dirs = {'Common': 'films/everyday', 'Rare': 'films/keep'}
        self.assertEqual(split_media_path('/srv/pool/disk-a/films/keep/X', roots, dirs), ('a', 'Rare', 'X'))
        self.assertEqual(split_media_path('/data/films/everyday/Y', roots, dirs), ('b', 'Common', 'Y'))
        for bad in ('/srv/pool/disk-a/films/keep', '/srv/pool/disk-a/films/keep/X/Y', '/data/films/other/X',
                    '/srv/pool/disk-a/../disk-a/films/keep/X', '/srv/pool/films/keep/X', 'data/films/keep/X'):
            with self.subTest(bad):
                self.assertIsNone(split_media_path(bad, roots, dirs))

    def test_layout_views(self):
        layout = MediaLayout(RUNTIME, TARGETS)
        path = local('media02', 'TV', 'Library', 'Show')
        self.assertEqual(layout.parse_local(path, 'TV'), ('media02', 'Library', 'Show'))
        self.assertEqual(layout.to_remote(path, 'TV'), '/volume1/media02/TV/Library/Show')
        self.assertEqual(layout.logical('TV', 'Library', 'Show'), '/media/TV/Library/Show')
        self.assertEqual(layout.arr_category('/media/TV/Library/Show', 'TV'), 'Library')


if __name__ == '__main__':
    unittest.main()


class ManifestReplayTests(unittest.TestCase):
    def test_replay_over_manifest_directory(self):
        import csv
        import tempfile
        new_modules()  # imported under example config, as on a deployment
        transfer = {False: 'SAME_DISK_RENAME', True: 'CROSS_DISK_TRANSFER'}
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp) / 'manifests' / '20260101T000000Z'
            folder.mkdir(parents=True)
            fields = ['execution_id', 'media_type', 'transfer_type', 'current', 'recommended',
                      'source_path', 'target_path', 'source_disk', 'target_disk']
            with (folder / 'execution_manifest.csv').open('w', newline='', encoding='utf-8') as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                n = 0
                for media, cross in (('Movie', False), ('TV', False), ('Movie', True), ('TV', True)):
                    for row in rows(media, cross):
                        n += 1
                        writer.writerow(dict(row, execution_id=f'20260101T000000Z-{n:04}',
                                             media_type=media, transfer_type=transfer[cross]))
                writer.writerow(dict(row, media_type='Music', transfer_type='OTHER'))
            report = layout_parity.replay_manifests(temp, RUNTIME)
        self.assertTrue(report['identical'], report['differences'][:3])
        self.assertEqual(report['skipped_rows'], 1)
        self.assertGreater(report['accepted'], 100)
        self.assertGreater(report['refused'], 200)


def custom_layout():
    """A deliberately non-Synology deployment: uneven root depths, renamed
    category folders, a different Arr root."""
    data = RUNTIME.as_dict()
    data['storage'] = {'fast': dict(local_path='/srv/pool/fast', remote_path='/mnt/fast'),
                       'bulk': dict(local_path='/data', remote_path='/share/deep/nested/bulk')}
    from runtime_config import RuntimeConfig
    runtime = RuntimeConfig(data)
    raw = json.loads((ROOT / 'config/storage-targets.example.json').read_text(encoding='utf-8'))
    template = raw['targets'][0]
    raw['targets'] = [dict(template, id='fast', path='/srv/pool/fast', remote_path='/mnt/fast'),
                      dict(template, id='bulk', path='/data', remote_path='/share/deep/nested/bulk')]
    raw['arr_root'] = '/library'
    raw['category_paths'] = {
        'Movie': {'Common': 'films/everyday', 'Rare': 'films/keep', 'Library': 'films/shelf',
                  'Archive': 'cold/films'},
        'TV': {'Current': 'shows/airing', 'Rare': 'shows/keep', 'Library': 'shows/shelf',
               'Archive': 'cold/shows'}}
    raw['placement'] = {media: {category: ['fast'] for category in raw['category_paths'][media]}
                        for media in ('Movie', 'TV')}
    from storage_targets import check_runtime_consistency, parse_targets
    targets = parse_targets(raw)
    check_runtime_consistency(runtime, targets)
    return runtime, targets


class CustomLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.modules = new_modules()
        cls.runtime, cls.targets = custom_layout()

    def patched(self, module):
        stack = contextlib.ExitStack()
        stack.enter_context(patch.object(module, 'RUNTIME', self.runtime))
        stack.enter_context(patch.object(module, 'TARGETS', self.targets))
        return stack

    def test_cross_disk_executors_follow_the_configured_layout(self):
        for name, media, cat_a, cat_b, dir_a, dir_b in (
                ('execute_cross_movie', 'Movie', 'Common', 'Rare', 'films/everyday', 'films/keep'),
                ('execute_cross_tv', 'TV', 'Current', 'Archive', 'shows/airing', 'cold/shows')):
            m = self.modules[name]
            row = dict(source_path=f'/srv/pool/fast/{dir_a}/Title', target_path=f'/data/{dir_b}/Title',
                       current=cat_a, recommended=cat_b, source_disk='fast', target_disk='bulk')
            with self.subTest(executor=name), self.patched(m):
                src, dst, logical_src, logical_dst = m.paths(row)
                self.assertEqual((logical_src, logical_dst),
                                 (f'/library/{dir_a}/Title', f'/library/{dir_b}/Title'))
                self.assertEqual(m.remote_path(dst), f'/share/deep/nested/bulk/{dir_b}/Title')
                remote = [m.remote_path(src), m.remote_path(dst)]
                self.assertEqual(remote[0], f'/mnt/fast/{dir_a}/Title')
                # The NAS side runs the shipped copy with the shipped tables.
                program = m.remote_program()
                self.assertIn('/share/deep/nested/bulk', program)
                namespace = {}
                header = program.split('data = json.load(sys.stdin)')[0]
                exec(compile(header.replace('import ctypes, hashlib, json, os, stat, sys, fcntl, time, shutil, re',
                                            'import ctypes, hashlib, json, os, stat, sys, time, shutil, re'),
                             '<NAS program>', 'exec'), namespace)
                namespace['nas_pair'](*remote, namespace['NAS_LAYOUT'])
                for bad in ([remote[0], remote[0]], [remote[0].replace('/mnt/fast', '/volume1/fast'), remote[1]],
                            [remote[0], remote[1].replace(dir_b, 'Movies/Rare')]):
                    with self.assertRaises(namespace['Refused']):
                        namespace['nas_pair'](*bad, namespace['NAS_LAYOUT'])
                # Synology-shaped rows from the old layout are refused, not guessed at.
                old_style = dict(row, source_path=local('media01', media, cat_a),
                                 target_path=local('media02', media, cat_b),
                                 source_disk='media01', target_disk='media02')
                with self.assertRaises(m.Refused):
                    m.paths(old_style)

    def test_same_disk_executors_follow_the_configured_layout(self):
        for name, media, cat_a, cat_b, dir_a, dir_b in (
                ('execute_movie_nas', 'Movie', 'Library', 'Archive', 'films/shelf', 'cold/films'),
                ('execute_tv_nas', 'TV', 'Rare', 'Library', 'shows/keep', 'shows/shelf'),
                ('execute_movie', 'Movie', 'Common', 'Library', 'films/everyday', 'films/shelf')):
            m = self.modules[name]
            row = dict(source_path=f'/data/{dir_a}/Title', target_path=f'/data/{dir_b}/Title',
                       current=cat_a, recommended=cat_b, source_disk='bulk', target_disk='bulk')
            with self.subTest(executor=name), self.patched(m):
                src, dst, logical_src, logical_dst = m.paths(row)
                self.assertEqual(logical_dst, f'/library/{dir_b}/Title')
                with self.assertRaises(m.Refused):
                    m.paths(dict(row, target_path=f'/srv/pool/fast/{dir_b}/Title'))
                if name == 'execute_movie':
                    continue
                remote = [m.remote_path(src), m.remote_path(dst)]
                self.assertEqual(remote[1], f'/share/deep/nested/bulk/{dir_b}/Title')
                m.nas_pair(*remote, m.remote_layout('bulk'))
                with self.assertRaises(m.Refused):
                    m.nas_pair(*remote, m.remote_layout('fast'))
                self.assertIn(repr(m.remote_layout('bulk')), m.remote_program('bulk'))

    def test_planner_reads_categories_from_the_configured_layout(self):
        layout = MediaLayout(self.runtime, self.targets)
        for script, media, path, expected in (
                ('dry_run_movies', 'Movie', '/library/films/keep/Title', 'Rare'),
                ('dry_run_movies', 'Movie', '/media/Movies/Rare/Title', 'Unknown'),
                ('dry_run_tv', 'TV', '/library/cold/shows/Show', 'Archive')):
            with self.subTest(path=path):
                self.assertEqual(planner_current_bucket(ROOT / f'{script}.py', layout)(path), expected)
