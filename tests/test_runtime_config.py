"""Offline config, command wiring and NAS helper characterization."""
import ast
import copy
import importlib
import json
from pathlib import Path, PurePosixPath
import tempfile
import unittest
from unittest.mock import patch, Mock

from runtime_config import ConfigError, RuntimeConfig, get_config, load_config

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / 'config/runtime.example.json'


class RuntimeConfigTests(unittest.TestCase):
    def setUp(self):
        self.data = json.loads(EXAMPLE.read_text())

    def test_production_characterization(self):
        c = load_config(EXAMPLE, environ={})
        self.assertEqual(c.base_path, Path('/opt/media-stack/migratarr'))
        self.assertEqual(c.ssh_target, 'migratarr@nas.example')
        self.assertEqual(c.ssh_key, str(Path.home() / '.ssh/migratarr_nas'))
        self.assertEqual(c.containers, dict(radarr='radarr', sonarr='sonarr', homepage='homepage'))
        self.assertEqual(c.urls, dict(radarr='http://localhost:7878', sonarr='http://localhost:8989',
                                     jellyfin='http://localhost:8096'))
        self.assertEqual(c.remote_disks, dict(media01='/volume4/media01', media02='/volume1/media02',
                                           media03='/volume2/media03', media04='/volume3/media04'))
        for disk in c.storage:
            self.assertEqual(c.local(disk), Path('/mnt/nas') / disk)

    def test_missing_required_settings(self):
        for section in ('nas', 'containers', 'urls'):
            for key in self.data[section]:
                data = copy.deepcopy(self.data)
                del data[section][key]
                with self.subTest(section=section, key=key), self.assertRaises(ConfigError):
                    RuntimeConfig(data)
        del self.data['base_path']
        with self.assertRaises(ConfigError):
            RuntimeConfig(self.data)

    def test_storage_disk_count_is_not_fixed(self):
        # Disk count is not a Phase One contract (see docs/storage-targets.md gate 4);
        # removing one of the four still validates, but an empty storage block does not.
        data = copy.deepcopy(self.data)
        del data['storage']['media01']
        c = RuntimeConfig(data)
        self.assertEqual(set(c.storage), {'media02', 'media03', 'media04'})
        data = copy.deepcopy(self.data)
        data['storage'] = {}
        with self.assertRaises(ConfigError):
            RuntimeConfig(data)

    def test_fifth_disk_is_accepted(self):
        data = copy.deepcopy(self.data)
        data['storage']['media05'] = dict(local_path='/mnt/nas/media05', remote_path='/volume5/media05')
        c = RuntimeConfig(data)
        self.assertEqual(c.local('media05'), Path('/mnt/nas/media05'))
        self.assertEqual(c.remote_disks['media05'], '/volume5/media05')

    def test_malformed_and_unknown_fields(self):
        mutations = [lambda d: d.update(extra=True), lambda d: d.update(schema_version=True),
                     lambda d: d.update(base_path='/a/../b'), lambda d: d.update(base_path='relative'),
                     lambda d: d['nas'].update(host='-oProxyCommand=bad'),
                     lambda d: d['nas'].update(user='user@host'),
                     lambda d: d['nas'].update(ssh_key='relative'),
                     lambda d: d['containers'].update(radarr=''),
                     lambda d: d['urls'].update(radarr='http://user:secret@host'),
                     lambda d: d['urls'].update(sonarr='http://host:bad'),
                     lambda d: d['storage']['media01'].update(local_path='/different/root'),
                     lambda d: d['storage']['media01'].update(remote_path='/volume3/media04')]
        for mutate in mutations:
            data = copy.deepcopy(self.data)
            mutate(data)
            with self.subTest(data=data), self.assertRaises(ConfigError):
                RuntimeConfig(data)

    def test_unknown_disk(self):
        with self.assertRaisesRegex(ConfigError, 'unknown disk'):
            RuntimeConfig(self.data).local('media99')

    def test_overrides_and_expansion(self):
        c = load_config(EXAMPLE, environ={'MIGRATARR_BASE_PATH': '/srv/migratarr',
                        'MIGRATARR_NAS_HOST': 'nas.example', 'MIGRATARR_NAS_USER': 'operator',
                        'MIGRATARR_RADARR_CONTAINER': 'movies', 'MIGRATARR_SONARR_URL': 'https://tv:8443',
                        'MIGRATARR_NAS_SSH_KEY': '~/.ssh/other'})
        self.assertEqual(c.base_path, Path('/srv/migratarr'))
        self.assertEqual(c.ssh_target, 'operator@nas.example')
        self.assertEqual(c.ssh_key, str(Path.home() / '.ssh/other'))
        self.assertEqual(c.containers['radarr'], 'movies')
        self.assertEqual(c.urls['sonarr'], 'https://tv:8443')
        with self.assertRaises(ConfigError):
            load_config(EXAMPLE, environ={'MIGRATARR_NAS_HOST': ''})

    def test_missing_file_duplicate_and_malformed_json(self):
        with tempfile.TemporaryDirectory() as temp:
            p = Path(temp) / 'runtime.json'
            with self.assertRaises(ConfigError):
                load_config(p, environ={})
            for content in ('{', '{"schema_version":1,"schema_version":1}'):
                p.write_text(content)
                with self.assertRaises(ConfigError):
                    load_config(environ={'MIGRATARR_CONFIG': str(p)})


class RuntimeWiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = load_config(EXAMPLE, environ={})
        # Importing executors must not invoke NAS, Docker, or HTTP.
        with patch('runtime_config.get_config', return_value=cls.config), \
                patch('subprocess.run', side_effect=AssertionError('external command')), \
                patch('subprocess.check_output', side_effect=AssertionError('external command')):
            cls.modules = {name: importlib.import_module(name) for name in
                           ('execute_cross_movie', 'execute_cross_tv', 'execute_movie_nas', 'execute_tv_nas',
                            'execute_movie', 'recover_cross_0102', 'recover_cross_0116', 'batch_cross_movies',
                            'batch_cross_tv')}

    def test_config_driven_mapping_and_any_declared_disk(self):
        data = self.config.as_dict()
        for disk, entry in data['storage'].items():
            entry['local_path'] = '/srv/storage/' + disk
            entry['remote_path'] = '/newvol/' + disk
        c = RuntimeConfig(data)
        for name, folder in (('execute_cross_movie', 'Movies'), ('execute_cross_tv', 'TV')):
            cross = self.modules[name]
            with patch.object(cross, 'RUNTIME', c), patch.object(cross, 'DISKS', c.remote_disks):
                self.assertEqual(cross.remote_path(f'/srv/storage/media02/{folder}/Rare/Title'),
                                 f'/newvol/media02/{folder}/Rare/Title')
                for p in (f'/mnt/nas/media02/{folder}/Rare/Title', f'/srv/storage/media99/{folder}/Rare/Title',
                          f'/srv/storage/media02/{folder}/../Title'):
                    with self.assertRaises(cross.Refused):
                        cross.remote_path(p)
        # The same-disk NAS executors are no longer pinned to media04: any disk
        # declared in runtime.json now works, the same as the cross-disk executor.
        # An undeclared disk is still refused.
        for name, media in (('execute_movie_nas', 'Movies'), ('execute_tv_nas', 'TV')):
            m = self.modules[name]
            with patch.object(m, 'RUNTIME', c):
                for disk in ('media01', 'media04'):
                    self.assertEqual(m.remote_path('/srv/storage/' + disk + '/' + media + '/Library/Title'),
                                     '/newvol/' + disk + '/' + media + '/Library/Title')
                with self.assertRaises(m.Refused):
                    m.remote_path('/srv/storage/media99/' + media + '/Library/Title')

    def test_fifth_disk_works_end_to_end_through_same_disk_executors(self):
        data = self.config.as_dict()
        data['storage']['media05'] = dict(local_path='/mnt/nas/media05', remote_path='/volume5/media05')
        c = RuntimeConfig(data)
        for name, media in (('execute_movie_nas', 'Movies'), ('execute_tv_nas', 'TV')):
            m = self.modules[name]
            with patch.object(m, 'RUNTIME', c):
                self.assertEqual(m.remote_path('/mnt/nas/media05/' + media + '/Library/Title'),
                                 '/volume5/media05/' + media + '/Library/Title')
                code = m.remote_program('media05')
                self.assertIn('/volume5/media05', code)

    def test_generated_helpers_are_self_contained(self):
        for name, args in (('execute_cross_movie', ()), ('execute_cross_tv', ()),
                           ('execute_movie_nas', ('media01',)), ('execute_tv_nas', ('media01',))):
            code = self.modules[name].remote_program(*args)
            compile(code, '<NAS helper>', 'exec')
            self.assertNotIn('RUNTIME', code)
            self.assertNotIn('runtime_config', code)
            self.assertIn('LOCK_EX | fcntl.LOCK_NB', code)
            if args:
                self.assertIn(self.config.remote_disks[args[0]], code)
        recovery = self.modules['recover_cross_0102'].receipt_program()
        compile(recovery, '<recovery helper>', 'exec')
        self.assertIn('/volume3/media04/Movies/Library/Moneyball (2011)', recovery)
        self.assertIn('/volume4/media01/Movies/Common/Moneyball (2011)', recovery)

    def test_container_and_endpoint_wiring(self):
        c = load_config(EXAMPLE, environ={'MIGRATARR_RADARR_CONTAINER': 'movies',
                                         'MIGRATARR_RADARR_URL': 'http://arrhost:17878'})
        m = self.modules['execute_cross_movie']
        with patch.object(m, 'RUNTIME', c), patch.object(m.subprocess, 'check_output',
                return_value=b'<Config><ApiKey>test</ApiKey><UrlBase>/arr</UrlBase></Config>') as docker:
            arr = m.Radarr()
        self.assertEqual(docker.call_args.args[0][2], 'movies')
        response = Mock()
        response.read.return_value = b'{}'
        opener = Mock()
        opener.open.return_value.__enter__ = Mock(return_value=response)
        opener.open.return_value.__exit__ = Mock(return_value=False)
        arr.opener = opener
        with patch.object(m, 'RUNTIME', c):
            arr.api('system/status')
        self.assertEqual(opener.open.call_args.args[0].full_url, 'http://arrhost:17878/arr/api/v3/system/status')

    def test_capture_accepts_new_paths_without_running_scripts(self):
        from migratarr_validation.capture import checked_tree, redirect_destinations
        for kind in ('movie', 'tv'):
            _, tree = checked_tree(kind)
            tree = redirect_destinations(tree, Path('/test/output'), Path('/test/cache'))
            compile(tree, '<capture>', 'exec')

    def test_batch_propagates_config_and_order(self):
        for module_name, pending_name, executor_name in (
                ('batch_cross_movies', 'pending_movies', 'execute_cross_movie.py'),
                ('batch_cross_tv', 'pending_series', 'execute_cross_tv.py')):
            with self.subTest(batch=module_name):
                batch = self.modules[module_name]
                row = dict(execution_id='20260915T192959Z-0001', title='Title', size_gb='1')
                calls = []
                with tempfile.TemporaryDirectory() as temp:
                    base = Path(temp)
                    logs = base / 'execution_logs'
                    logs.mkdir()
                    def runner(command, **kwargs):
                        calls.append((command, kwargs))
                        if '--execute' in command:
                            (logs / (row['execution_id'] + '.jsonl')).write_text(
                                json.dumps(dict(event='SUCCESS', manifest_sha256='hash')) + '\n')
                        return Mock(returncode=0)
                    with patch.object(batch, pending_name, return_value=([row], 0, 'hash')):
                        self.assertEqual(batch.run_batch(base, '20260915T192959Z', True, runner), 0)
                    self.assertEqual(len(calls), 3)
                    self.assertTrue(calls[0][0][1].endswith('approve_execution.py'))
                    self.assertTrue(calls[1][0][1].endswith(executor_name))
                    self.assertNotIn('--execute', calls[1][0])
                    self.assertTrue(calls[2][0][1].endswith(executor_name))
                    self.assertIn('--execute', calls[2][0])
                    for _, kwargs in calls:
                        self.assertEqual(kwargs['env']['MIGRATARR_CONFIG'], str(EXAMPLE.resolve()))
                        self.assertEqual(kwargs['env']['MIGRATARR_BASE_PATH'], base.as_posix())

    def test_batch_limit_restricts_execution_to_first_n_pending(self):
        batch = self.modules['batch_cross_tv']
        rows = [dict(execution_id=f'20260915T192959Z-000{i}', title=f'Title{i}', size_gb=str(i))
                for i in (1, 2, 3)]
        calls = []
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            logs = base / 'execution_logs'
            logs.mkdir()
            def runner(command, **kwargs):
                calls.append(command)
                if '--execute' in command:
                    (logs / (command[2] + '.jsonl')).write_text(
                        json.dumps(dict(event='SUCCESS', manifest_sha256='hash')) + '\n')
                return Mock(returncode=0)
            with patch.object(batch, 'pending_series', return_value=(rows, 0, 'hash')):
                self.assertEqual(batch.run_batch(base, '20260915T192959Z', True, runner, limit=2), 0)
            self.assertEqual(len(calls), 6)
            executed = {command[2] for command in calls if command[1].endswith('execute_cross_tv.py')}
            self.assertEqual(executed, {'20260915T192959Z-0001', '20260915T192959Z-0002'})


if __name__ == '__main__':
    unittest.main()
