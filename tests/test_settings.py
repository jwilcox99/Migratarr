"""Phase 1 settings foundation: runtime.json v2, one disk definition, problem
reports, migration, atomic backed-up saves, service registry, pasted keys."""
import copy
import fcntl
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from migratarr import services, settings
from migratarr.settings import keys as key_store
from planner_settings import parse_settings, settings_problems, SettingsError
from runtime_config import ConfigError, RuntimeConfig, load_config, storage_from_targets
from storage_targets import deployed_targets_path, load_targets, parse_targets, target_problems
import service_keys

ROOT = Path(__file__).resolve().parents[1]
V1 = ROOT / 'config/examples/runtime.v1.example.json'
V2 = ROOT / 'config/runtime.example.json'
LOCAL = ROOT / 'config/examples/runtime.local.example.json'
TARGETS = ROOT / 'config/storage-targets.example.json'
PLANNER = ROOT / 'config/planner.example.json'


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


class Deployment:
    """A throwaway deployment folder with all three files, version 1 by default."""

    def __init__(self, runtime=None, targets=None, planner=None):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.config = self.base / 'config'
        self.config.mkdir()
        runtime = copy.deepcopy(runtime or read(V1))
        runtime['base_path'] = str(self.base)
        self.write('runtime.json', runtime)
        self.write('storage-targets.json', targets or read(TARGETS))
        self.write('planner.json', planner or read(PLANNER))
        self.env = {'MIGRATARR_CONFIG': str(self.config / 'runtime.json'),
                    'MIGRATARR_PLANNER_CONFIG': str(self.config / 'planner.json')}

    def write(self, name, data):
        (self.config / name).write_text(json.dumps(data, indent=2) + '\n', encoding='utf-8')

    def read(self, name):
        return read(self.config / name)

    def close(self):
        self.temp.cleanup()


class RuntimeVersionTwoTests(unittest.TestCase):
    def test_nas_example_matches_version_one_effective_settings(self):
        v1 = load_config(V1, environ={})
        v2 = load_config(V2, environ={'MIGRATARR_STORAGE_TARGETS': str(TARGETS)})
        self.assertEqual((v2.schema_version, v2.topology), (2, 'nas_ssh'))
        self.assertEqual(v2.storage, v1.storage)
        self.assertEqual((v2.ssh_target, v2.ssh_key, v2.remote_python),
                         (v1.ssh_target, v1.ssh_key, v1.remote_python))
        self.assertEqual((v2.urls, v2.containers, v2.secrets, v2.arr_file_checks),
                         (v1.urls, v1.containers, v1.secrets, v1.arr_file_checks))
        self.assertEqual(v2.remote_disks, v1.remote_disks)
        self.assertEqual(v2.targets_path, TARGETS.resolve())

    def test_version_one_unchanged(self):
        v1 = load_config(V1, environ={})
        self.assertEqual((v1.schema_version, v1.topology, v1.targets_path), (1, 'nas_ssh', None))
        self.assertEqual(RuntimeConfig(v1.as_dict()).as_dict(), v1.as_dict())

    def test_local_example(self):
        c = load_config(LOCAL, environ={'MIGRATARR_NAS_HOST': 'ignored'})
        self.assertEqual(c.topology, 'local')
        self.assertIsNone(c.ssh_target)
        for disk, entry in c.storage.items():
            self.assertEqual(entry['local_path'], entry['remote_path'])
        self.assertNotIn('nas', c.as_dict())

    def test_version_two_rules(self):
        targets = load_targets(TARGETS)
        base = read(V2)
        RuntimeConfig(base, targets)
        cases = {
            'storage': dict(base, storage=read(V1)['storage']),
            'no topology': {k: v for k, v in base.items() if k != 'topology'},
            'bad topology': dict(base, topology='cloud'),
            'nas missing': {k: v for k, v in base.items() if k != 'nas'},
            'nas on local': dict(base, topology='local'),
        }
        for label, data in cases.items():
            with self.subTest(label), self.assertRaises(ConfigError):
                RuntimeConfig(data, targets)
        with self.assertRaisesRegex(ConfigError, 'none was loaded'):
            RuntimeConfig(base)

    def test_topology_decides_remote_paths(self):
        nas = load_targets(TARGETS)
        local = load_targets(ROOT / 'config/examples/storage-targets.local.example.json')
        with self.assertRaisesRegex(ConfigError, 'needs remote_path'):
            storage_from_targets(local, 'nas_ssh')
        with self.assertRaisesRegex(ConfigError, 'topology local does not use'):
            storage_from_targets(nas, 'local')

    def test_storage_targets_location(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            data = dict(read(V2), storage_targets='../shared/targets.json')
            (folder / 'config').mkdir()
            (folder / 'shared').mkdir()
            (folder / 'shared/targets.json').write_text(TARGETS.read_text())
            (folder / 'config/runtime.json').write_text(json.dumps(data))
            c = load_config(folder / 'config/runtime.json', environ={})
            self.assertEqual(c.targets_path, (folder / 'shared/targets.json').resolve())
            self.assertEqual(deployed_targets_path(c, environ={}), c.targets_path)
            self.assertEqual(deployed_targets_path(c, environ={'MIGRATARR_STORAGE_TARGETS': '/x.json'}),
                             Path('/x.json'))
        v1 = load_config(V1, environ={})
        self.assertEqual(deployed_targets_path(v1, environ={}),
                         v1.base_path / 'config' / 'storage-targets.json')

    def test_problems_lists_every_section(self):
        data = {'schema_version': 2, 'base_path': 'relative', 'topology': 'nas_ssh',
                'containers': {'radarr': 'bad name'},
                'urls': {'radarr': 'ftp://x', 'sonarr': 'http://ok/', 'jellyfin': 'http://ok'},
                'nas': {'host': 'h'}}
        fields = [f for f, _ in RuntimeConfig.problems(data, load_targets(TARGETS))]
        self.assertEqual(fields, ['base_path', 'containers', 'urls.radarr', 'urls.sonarr', 'nas'])
        self.assertEqual(RuntimeConfig.problems(read(V1)), [])
        self.assertEqual(RuntimeConfig.problems(read(V2), load_targets(TARGETS)), [])

    def test_problems_agree_with_strict_validation(self):
        # Every single-field mutation: the strict constructor fails iff problems() reports one.
        base = read(V1)
        mutations = [('base_path', 'x'), ('containers', []), ('urls', {}), ('nas', None), ('storage', {}),
                     ('secrets', {'tmdb': {'source': 'nope'}}), ('arr_file_checks', {'x': 1}),
                     ('schema_version', 3), ('extra', 1)]
        for key, value in mutations:
            data = dict(base, **{key: value})
            with self.subTest(key):
                self.assertTrue(RuntimeConfig.problems(data))
                with self.assertRaises(ConfigError):
                    RuntimeConfig(data)


class FileProblemReportTests(unittest.TestCase):
    def test_examples_have_no_problems(self):
        self.assertEqual(target_problems(read(TARGETS)), [])
        self.assertEqual(target_problems(read(ROOT / 'config/examples/storage-targets.local.example.json')), [])
        self.assertEqual(settings_problems(read(PLANNER)), [])

    def test_targets_report_each_target(self):
        data = read(TARGETS)
        data['targets'][1]['path'] = 'relative'
        data['targets'][2]['enabled'] = 'yes'
        data['category_paths']['TV']['Rare'] = '/absolute'
        fields = [f for f, _ in target_problems(data)]
        self.assertEqual(fields, ['targets[1] (media02)', 'targets[2] (media03)', 'category_paths.TV'])
        with self.assertRaises(ValueError):
            parse_targets(data)

    def test_targets_problems_agree_with_parser(self):
        base = read(TARGETS)
        for key, value in [('schema_version', 2), ('defaults', {}), ('targets', []), ('arr_root', 'x'),
                           ('placement', {'Movie': {}, 'TV': {}}), ('surprise', 1)]:
            data = dict(base, **{key: value})
            with self.subTest(key):
                self.assertTrue(target_problems(data))
                with self.assertRaises(ValueError):
                    parse_targets(data)

    def test_planner_reports_each_field(self):
        data = read(PLANNER)
        data['streaming']['region'] = 'us'
        data['streaming']['subscribed'] = ['Hulu', 'Hulu']
        fields = [f for f, _ in settings_problems(data)]
        self.assertEqual(fields, ['streaming.region', 'streaming.subscribed'])
        with self.assertRaises(SettingsError):
            parse_settings(data)


class ServiceRegistryTests(unittest.TestCase):
    def test_service_keys_values_are_unchanged(self):
        # The literals service_keys.py had before the registry (main @ 696d7d4).
        self.assertEqual(service_keys.SERVICES, ('radarr', 'sonarr', 'jellyfin', 'tmdb'))
        self.assertEqual(service_keys.ARR, ('radarr', 'sonarr'))
        self.assertEqual(service_keys.DEFAULT_SOURCES, {
            'radarr': {'source': 'docker_config_xml'},
            'sonarr': {'source': 'docker_config_xml'},
            'jellyfin': {'source': 'docker_file',
                         'paths': ['/run/secrets/jellyfin_api_key', '/run/secrets/jellyfin_key']},
            'tmdb': {'source': 'env', 'variable': 'TMDB_TOKEN'},
        })
        self.assertEqual(service_keys.DEFAULT_CONTAINER,
                         {'radarr': 'radarr', 'sonarr': 'sonarr', 'jellyfin': 'homepage'})

    def test_descriptors_agree_with_credential_parser(self):
        containers = {'radarr': 'radarr', 'sonarr': 'sonarr', 'homepage': 'homepage'}
        samples = {'docker_config_xml': {}, 'config_xml': {'path': '/c.xml'},
                   'docker_file': {'paths': ['/run/secrets/x'], 'container': 'homepage'}, 'file': {'path': '/k'},
                   'env': {'variable': 'K'}}
        for service in services.SERVICES.values():
            for kind, extra in samples.items():
                entry = dict(extra, source=kind)
                with self.subTest(service=service.id, kind=kind):
                    if kind in service.sources:
                        service_keys.parse_secrets({service.id: entry}, containers)
                    else:
                        with self.assertRaises(service_keys.SecretError):
                            service_keys.parse_secrets({service.id: entry}, containers)
            if service.url_setting:
                self.assertIn(service.url_setting.split('.', 1)[1], read(V2)['urls'])


class SettingsLoadAndMigrateTests(unittest.TestCase):
    def setUp(self):
        self.d = Deployment()
        self.addCleanup(self.d.close)

    def test_version_one_report(self):
        report = settings.load(environ=self.d.env)
        self.assertTrue(report.ok)
        self.assertTrue(report.needs_migration)
        self.assertEqual([(p.field, p.severity) for p in report.problems], [('schema_version', 'warning')])
        self.assertEqual(report.files.storage_targets, self.d.config / 'storage-targets.json')

    def test_report_lists_problems_across_files_without_raising(self):
        runtime = self.d.read('runtime.json')
        runtime['urls']['radarr'] = 'nope'
        self.d.write('runtime.json', runtime)
        planner = self.d.read('planner.json')
        planner['streaming']['region'] = 'usa'
        self.d.write('planner.json', planner)
        (self.d.config / 'storage-targets.json').write_text('{broken', encoding='utf-8')
        report = settings.load(environ=self.d.env)
        self.assertFalse(report.ok)
        got = {(p.file, p.field) for p in report.errors}
        self.assertEqual(got, {('runtime', 'urls.radarr'), ('planner', 'streaming.region'),
                               ('storage_targets', 'file')})
        json.dumps(report.as_dict())

    def test_inconsistent_version_one_is_an_error(self):
        targets = self.d.read('storage-targets.json')
        targets['targets'][0]['remote_path'] = '/volume9/media01'
        self.d.write('storage-targets.json', targets)
        report = settings.load(environ=self.d.env)
        self.assertIn(('runtime', 'storage'), {(p.file, p.field) for p in report.errors})
        with self.assertRaises(settings.SettingsInvalid):
            settings.migrate(environ=self.d.env)
        self.assertEqual(self.d.read('runtime.json')['schema_version'], 1)

    def test_migration(self):
        before = load_config(environ=self.d.env)
        original = (self.d.config / 'runtime.json').read_bytes()
        result = settings.migrate(environ=self.d.env)
        after = load_config(environ=self.d.env)
        data = self.d.read('runtime.json')
        self.assertEqual(data['schema_version'], 2)
        self.assertEqual(data['topology'], 'nas_ssh')
        self.assertNotIn('storage', data)
        self.assertNotIn('storage_targets', data)  # beside runtime.json: the default
        self.assertEqual(after.storage, before.storage)
        self.assertEqual(after.ssh_target, before.ssh_target)
        backup = Path(result.backup) / 'runtime.json'
        self.assertEqual(backup.read_bytes(), original)
        self.assertEqual(stat.S_IMODE(Path(result.backup).stat().st_mode), 0o700)
        self.assertIsNone(settings.migrate(environ=self.d.env))  # once
        self.assertIsNone(settings.migrate_if_needed(environ=self.d.env, log=None))

    def test_migration_records_a_targets_file_elsewhere(self):
        shared = self.d.base / 'elsewhere.json'
        shared.write_text((self.d.config / 'storage-targets.json').read_text())
        env = dict(self.d.env, MIGRATARR_STORAGE_TARGETS=str(shared))
        settings.migrate(environ=env)
        self.assertEqual(self.d.read('runtime.json')['storage_targets'], str(shared.resolve()))
        self.assertEqual(load_config(environ=self.d.env).targets_path, shared.resolve())

    def test_migration_refuses_extra_target_disks(self):
        targets = self.d.read('storage-targets.json')
        extra = dict(targets['targets'][0], id='media05', path='/mnt/nas/media05',
                     remote_path='/volume5/media05')
        targets['targets'].append(extra)
        self.d.write('storage-targets.json', targets)
        with self.assertRaisesRegex(settings.SettingsInvalid, 'media05'):
            settings.migrate(environ=self.d.env)

    def test_migration_waits_for_running_job(self):
        (self.d.base / 'jobs').mkdir()
        with open(self.d.base / 'jobs/runner.lock', 'a') as held:
            fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(settings.busy(self.d.base), 'a UI job is running')
            with self.assertRaises(settings.SettingsBusy):
                settings.migrate(environ=self.d.env)
            message = settings.migrate_if_needed(environ=self.d.env, log=None)
            self.assertIn('postponed', message)
        self.assertEqual(self.d.read('runtime.json')['schema_version'], 1)
        self.assertIsNone(settings.busy(self.d.base))

    def test_executor_lock_also_blocks(self):
        (self.d.base / 'execution_logs').mkdir()
        with open(self.d.base / 'execution_logs/executor.lock', 'a') as held:
            fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(settings.busy(self.d.base), 'an executor is running')


class SaveTests(unittest.TestCase):
    def setUp(self):
        self.d = Deployment()
        self.addCleanup(self.d.close)
        settings.migrate(environ=self.d.env)

    def test_save_validates_backs_up_and_writes_atomically(self):
        os.chmod(self.d.config / 'planner.json', 0o640)
        report = settings.load(environ=self.d.env)
        planner = copy.deepcopy(report.data['planner'])
        planner['streaming']['subscribed'] = ['Hulu']
        diff = settings.diff(report, {'planner': planner})
        self.assertIn('planner', diff)
        self.assertIn('-', diff['planner'])
        result = settings.save({'planner': planner}, report=report, environ=self.d.env)
        self.assertEqual(self.d.read('planner.json')['streaming']['subscribed'], ['Hulu'])
        self.assertEqual(stat.S_IMODE((self.d.config / 'planner.json').stat().st_mode), 0o640)
        self.assertEqual(json.loads((Path(result.backup) / 'planner.json').read_text())['streaming'],
                         report.data['planner']['streaming'])
        self.assertEqual(list(self.d.config.glob('.planner.json.*')), [])

    def test_invalid_change_is_refused_with_every_problem(self):
        report = settings.load(environ=self.d.env)
        runtime = copy.deepcopy(report.data['runtime'])
        runtime['urls']['radarr'] = 'x'
        runtime['urls']['sonarr'] = 'y'
        with self.assertRaises(settings.SettingsInvalid) as caught:
            settings.save({'runtime': runtime}, report=report, environ=self.d.env)
        self.assertEqual({p.field for p in caught.exception.problems}, {'urls.radarr', 'urls.sonarr'})
        self.assertEqual(self.d.read('runtime.json'), report.data['runtime'])

    def test_change_to_targets_is_checked_against_runtime(self):
        report = settings.load(environ=self.d.env)
        targets = copy.deepcopy(report.data['storage_targets'])
        del targets['targets'][0]['remote_path']  # topology nas_ssh needs it
        with self.assertRaisesRegex(settings.SettingsInvalid, 'needs remote_path'):
            settings.save({'storage_targets': targets}, report=report, environ=self.d.env)

    def test_lost_update_is_refused(self):
        report = settings.load(environ=self.d.env)
        other = self.d.read('planner.json')
        other['streaming']['region'] = 'CA'
        self.d.write('planner.json', other)
        mine = copy.deepcopy(report.data['planner'])
        mine['streaming']['subscribed'] = []
        with self.assertRaises(settings.SettingsConflict):
            settings.save({'planner': mine}, report=report, environ=self.d.env)
        self.assertEqual(self.d.read('planner.json')['streaming']['region'], 'CA')

    def test_save_refused_while_job_runs(self):
        report = settings.load(environ=self.d.env)
        planner = copy.deepcopy(report.data['planner'])
        planner['streaming']['region'] = 'GB'
        (self.d.base / 'jobs').mkdir()
        with open(self.d.base / 'jobs/runner.lock', 'a') as held:
            fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(settings.SettingsBusy):
                settings.save({'planner': planner}, report=report, environ=self.d.env)
        self.assertEqual(self.d.read('planner.json')['streaming']['region'], 'US')

    def test_dry_run_and_no_change(self):
        report = settings.load(environ=self.d.env)
        self.assertEqual(settings.save({'planner': report.data['planner']}, report=report,
                                       environ=self.d.env).written, [])
        planner = copy.deepcopy(report.data['planner'])
        planner['streaming']['region'] = 'GB'
        result = settings.save({'planner': planner}, report=report, environ=self.d.env, dry_run=True)
        self.assertEqual(result.written, [])
        self.assertIn('planner', result.diff)
        self.assertEqual(self.d.read('planner.json')['streaming']['region'], 'US')

    def test_switch_to_local_topology_in_one_save(self):
        report = settings.load(environ=self.d.env)
        runtime = copy.deepcopy(report.data['runtime'])
        runtime['topology'] = 'local'
        del runtime['nas']
        targets = copy.deepcopy(report.data['storage_targets'])
        for target in targets['targets']:
            del target['remote_path']
        settings.save({'runtime': runtime, 'storage_targets': targets}, report=report, environ=self.d.env)
        c = load_config(environ=self.d.env)
        self.assertEqual(c.topology, 'local')
        self.assertEqual(c.storage['media01'], {'local_path': '/mnt/nas/media01',
                                                'remote_path': '/mnt/nas/media01'})

    def test_report_never_contains_credential_values(self):
        token = 'tok-' + 'x' * 30
        secret = self.d.base.parent / ('k-' + self.d.base.name)
        secret.write_text(token)
        self.addCleanup(secret.unlink)
        os.chmod(secret, 0o600)
        report = settings.load(environ=self.d.env)
        runtime = copy.deepcopy(report.data['runtime'])
        runtime['secrets'] = {'tmdb': {'source': 'file', 'path': str(secret)}}
        settings.save({'runtime': runtime}, report=report, environ=self.d.env)
        with patch.dict(os.environ, {'TMDB_TOKEN': token}):
            report = settings.load(environ=self.d.env)
            text = json.dumps(report.as_dict()) + json.dumps(settings.redacted_secrets(report.runtime))
        self.assertNotIn(token, text)
        self.assertEqual(service_keys.read_key('tmdb', report.runtime), token)


class PastedKeyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name) / 'secrets'

    def test_store_and_read_back(self):
        value = 'a1b2c3d4e5f60718293a4b5c6d7e8f90'
        entry = key_store.store_key('radarr', '  ' + value + '\n', folder=self.folder, url_base='/radarr')
        self.assertNotIn(value, json.dumps(entry))
        path = Path(entry['path'])
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.folder.stat().st_mode), 0o700)
        runtime = load_config(V1, environ={})
        parsed = service_keys.parse_secrets({'radarr': entry}, runtime.containers)
        runtime.secrets = dict(runtime.secrets, radarr=parsed['radarr'])
        self.assertEqual(service_keys.read_credential('radarr', runtime), (value, '/radarr'))
        self.assertTrue(key_store.forget_key('radarr', folder=self.folder))
        self.assertFalse(path.exists())

    def test_refusals_never_echo_the_value(self):
        for service, value, kwargs in [('radarr', 'two words', {}), ('radarr', 'a\nb', {}),
                                       ('nope', 'abc', {}), ('tmdb', 'abc', {'url_base': '/x'})]:
            with self.subTest(service=service, value=value):
                with self.assertRaises(key_store.KeyStoreError) as caught:
                    key_store.store_key(service, value, folder=self.folder, **kwargs)
                self.assertNotIn(value, str(caught.exception))

    def test_refuses_folder_inside_repository(self):
        with self.assertRaises(key_store.KeyStoreError):
            key_store.store_key('tmdb', 'abc', folder=ROOT / 'config' / 'secrets')
        self.assertFalse((ROOT / 'config' / 'secrets').exists())


if __name__ == '__main__':
    unittest.main()
