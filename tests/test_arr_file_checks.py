"""arr_file_checks: docker (default, unchanged) vs host-side Arr file checks."""
import copy
import hashlib
import importlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from arr_files import host_path
from planner_settings import load_settings
from runtime_config import ConfigError, RuntimeConfig, load_config
from storage_targets import load_targets


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = json.loads((ROOT / 'config/runtime.example.json').read_text(encoding='utf-8'))
TARGETS = load_targets(ROOT / 'config/storage-targets.example.json')
CLIENTS = {'execute_movie': ('Radarr', 'radarr'), 'execute_movie_nas': ('Radarr', 'radarr'),
           'execute_cross_movie': ('Radarr', 'radarr'), 'execute_cross_tv': ('Sonarr', 'sonarr'),
           'execute_tv_nas': ('Sonarr', 'sonarr'), 'recover_cross_0102': ('RecoveryRadarr', 'radarr')}
FILE = '/media/Movies/Common/Film (2001)/film.mkv'
CONTENT = b'offline test media'
DIGEST = hashlib.sha256(CONTENT).hexdigest()


def runtime(checks=None, containers=None, secrets=None):
    data = copy.deepcopy(EXAMPLE)
    if checks is not None:
        data['arr_file_checks'] = checks
    if containers is not None:
        data['containers'] = containers
    if secrets is not None:
        data['secrets'] = secrets
    return RuntimeConfig(data)


def import_clients():
    config = load_config(ROOT / 'config/runtime.example.json', environ={})
    with patch('runtime_config.get_config', return_value=config), \
            patch('planner_settings.get_settings', return_value=load_settings(ROOT / 'config/planner.example.json')), \
            patch('media_layout.get_targets', return_value=TARGETS):
        return {name: importlib.import_module(name) for name in CLIENTS}


def client(module, name):
    # visible()/verify_file() need no API key; skip __init__'s credential lookup.
    cls = getattr(module, CLIENTS[name][0])
    return object.__new__(cls)


def refused(module):
    return getattr(module, 'Refused', None) or module.m.Refused


class ConfigTests(unittest.TestCase):
    def test_default_is_docker_in_the_named_containers(self):
        c = runtime()
        self.assertEqual(c.arr_file_checks, {'radarr': {'mode': 'docker', 'container': 'radarr'},
                                             'sonarr': {'mode': 'docker', 'container': 'sonarr'}})
        self.assertNotIn('arr_file_checks', c.as_dict())

    def test_fully_non_docker_deployment_needs_no_containers(self):
        c = runtime(checks={'radarr': {'mode': 'host'}, 'sonarr': {'mode': 'host', 'host_root': os.path.abspath(os.sep + 'srv')}},
                    containers={},
                    secrets={'radarr': {'source': 'env', 'variable': 'RADARR_KEY'},
                             'sonarr': {'source': 'env', 'variable': 'SONARR_KEY'},
                             'jellyfin': {'source': 'env', 'variable': 'JELLYFIN_KEY'}})
        self.assertEqual(c.containers, {})
        self.assertEqual(c.arr_file_checks['radarr'], {'mode': 'host', 'host_root': None})
        self.assertEqual(RuntimeConfig(c.as_dict()).arr_file_checks, c.arr_file_checks)

    def test_rejects_malformed_checks(self):
        for label, checks, containers in [
            ('unknown service', {'jellyfin': {'mode': 'host'}}, None),
            ('unknown mode', {'radarr': {'mode': 'ssh'}}, None),
            ('missing mode', {'radarr': {}}, None),
            ('host_root on docker', {'radarr': {'mode': 'docker', 'host_root': '/srv'}}, None),
            ('container on host', {'radarr': {'mode': 'host', 'container': 'radarr'}}, None),
            ('relative host_root', {'radarr': {'mode': 'host', 'host_root': 'srv/media'}}, None),
            ('trailing slash', {'radarr': {'mode': 'host', 'host_root': os.path.abspath(os.sep + 'srv') + os.sep}}, None),
            ('bad container', {'radarr': {'mode': 'docker', 'container': '-it'}}, None),
            ('docker without container', {}, {'sonarr': 'sonarr', 'homepage': 'homepage'}),
        ]:
            with self.subTest(label), self.assertRaises(ConfigError):
                runtime(checks, containers)

    def test_host_path_mapping(self):
        mapped = {'mode': 'host', 'host_root': '/srv/pool'}
        native = {'mode': 'host', 'host_root': None}
        self.assertEqual(host_path(mapped, '/media', '/media/Movies/Common/X/f.mkv'), '/srv/pool/Movies/Common/X/f.mkv')
        self.assertEqual(host_path(mapped, '/media', '/media'), '/srv/pool')
        self.assertEqual(host_path(native, '/media', '/media/Movies/Common/X'), '/media/Movies/Common/X')
        for bad in ('/mediax/Movies/X', '/other/X', 'media/Movies/X', '/media/../etc/passwd', '/media//Movies'):
            with self.subTest(bad):
                self.assertIsNone(host_path(mapped, '/media', bad))


class DockerModeTests(unittest.TestCase):
    """The default issues exactly the commands the executors always have."""

    @classmethod
    def setUpClass(cls):
        cls.modules = import_clients()

    def test_commands_unchanged(self):
        for name, (_, service) in CLIENTS.items():
            m = self.modules[name]
            base = m.m if name.startswith('recover') else m
            runs, hashes = [], []

            def run(command, **kwargs):
                runs.append(command)
                return type('R', (), {'returncode': 0})()

            def run_progress(command, *args, **kwargs):
                hashes.append(command)
                return DIGEST + '  ' + command[-1]

            def check_output(command, **kwargs):
                hashes.append(command)
                return DIGEST + '  ' + command[-1]

            with self.subTest(executor=name), patch.object(base.subprocess, 'run', run), \
                    patch.object(base, 'run_progress', run_progress, create=True), \
                    patch.object(base.subprocess, 'check_output', check_output):
                arr = client(m, name)
                arr.visible('/media/Movies/Common', '-d')
                arr.verify_file(FILE, DIGEST)
                self.assertEqual(runs, [['docker', 'exec', service, 'test', '-d', '/media/Movies/Common'],
                                        ['docker', 'exec', service, 'test', '-f', FILE]])
                self.assertEqual(hashes, [['docker', 'exec', service, 'sha256sum', '--', FILE]])
                with self.assertRaises(refused(m)):
                    arr.verify_file(FILE, '0' * 64)


class HostModeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.modules = import_clients()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = self.temp.name
        media = Path(self.root, 'Movies', 'Common', 'Film (2001)')
        media.mkdir(parents=True)
        (media / 'film.mkv').write_bytes(CONTENT)

    def test_host_root_checks_and_hashes_without_docker(self):
        for name, (_, service) in CLIENTS.items():
            m = self.modules[name]
            base = m.m if name.startswith('recover') else m
            c = runtime({service: {'mode': 'host', 'host_root': self.root}})
            forbidden = AssertionError('docker used in host mode')
            with self.subTest(executor=name), patch.object(base, 'RUNTIME', c), \
                    patch.object(m, 'RUNTIME', c), \
                    patch.object(base.subprocess, 'run', side_effect=forbidden), \
                    patch.object(base.subprocess, 'check_output', side_effect=forbidden), \
                    patch.object(base, 'run_progress', side_effect=forbidden, create=True):
                arr = client(m, name)
                arr.visible('/media/Movies/Common', '-d')
                arr.verify_file(FILE, DIGEST)
                for path, digest in ((FILE, '0' * 64), ('/media/Movies/Common/Missing/f.mkv', DIGEST),
                                     ('/elsewhere/Movies/Common/Film (2001)/film.mkv', DIGEST)):
                    with self.assertRaises(refused(m)):
                        arr.verify_file(path, digest)
                with self.assertRaises(refused(m)):
                    arr.visible('/media/Movies/Common/Film (2001)', '-f')

    @unittest.skipUnless(os.name == 'posix', 'Arr paths are host paths only on a POSIX host')
    def test_native_host_mode_checks_arr_paths_as_given(self):
        m = self.modules['execute_cross_movie']
        c = runtime({'radarr': {'mode': 'host'}})
        path = str(Path(self.root, 'Movies', 'Common', 'Film (2001)', 'film.mkv'))
        with patch.object(m, 'RUNTIME', c), patch.object(m.subprocess, 'run', side_effect=AssertionError):
            arr = client(m, 'execute_cross_movie')
            arr.verify_file(path, DIGEST)
            with self.assertRaises(m.Refused):
                arr.verify_file(path, '0' * 64)


if __name__ == '__main__':
    unittest.main()


class ParityToolTests(unittest.TestCase):
    def test_reports_agreement_and_disagreement(self):
        from migratarr_validation import arr_files_parity as tool
        with tempfile.TemporaryDirectory() as temp:
            media = Path(temp, 'Movies', 'Common', 'Film (2001)')
            media.mkdir(parents=True)
            (media / 'film.mkv').write_bytes(CONTENT)
            files = [(FILE, 10), ('/media/Movies/Common/Gone/g.mkv', 20)]

            def check_output(command, **kwargs):
                return DIGEST + '  ' + command[-1]

            def run(command, **kwargs):
                return type('R', (), {'returncode': 0 if command[-1] == FILE else 1})()

            with patch.object(tool, 'arr_files', return_value=files), \
                    patch.object(tool.subprocess, 'run', run), \
                    patch.object(tool.subprocess, 'check_output', check_output):
                report = tool.compare(runtime(), TARGETS, {'radarr': temp}, sample=5)
                self.assertTrue(report['identical'], report)
                self.assertEqual(report['radarr']['files'], 2)
                with patch.object(tool, 'host_sha256', return_value='0' * 64):
                    self.assertFalse(tool.compare(runtime(), TARGETS, {'radarr': temp}, sample=5)['identical'])
