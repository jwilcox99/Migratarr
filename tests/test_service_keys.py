import copy
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from migratarr_validation import secrets_parity as old
from runtime_config import ConfigError, RuntimeConfig, load_config
from service_keys import SecretError, parse_secrets, read_credential, read_key


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = json.loads((ROOT / 'config/runtime.example.json').read_text(encoding='utf-8'))
KEY = 'a1b2c3d4e5f60718293a4b5c6d7e8f90'

RADARR_XML = f'''<Config>
  <LogLevel>info</LogLevel>
  <UrlBase></UrlBase>
  <Port>7878</Port>
  <ApiKey>{KEY}</ApiKey>
  <AuthenticationMethod>Forms</AuthenticationMethod>
</Config>
'''


class FakeDocker:
    """`docker exec <container> cat <path>` and the planners' `sh -c` sed/cat forms."""

    def __init__(self, files):
        self.files = files  # {(container, path): bytes}
        self.calls = []

    def __call__(self, command, text=False, timeout=None, **_):
        self.calls.append(command)
        assert command[:2] == ['docker', 'exec'], command
        container, rest = command[2], command[3:]
        if rest[:2] == ['sh', '-c']:
            script = rest[2]
            if script.startswith('cat '):
                data = self.read(container, script[4:])
            else:
                assert script == old.SED_API_KEY, script
                content = self.read(container, '/config/config.xml').decode()
                data = ''.join(m.group(1) + '\n' for line in content.splitlines()
                               if (m := re.match(r'.*<ApiKey>(.*)</ApiKey>.*', line))).encode()
        else:
            assert rest[0] == 'cat' and len(rest) == 2, command
            data = self.read(container, rest[1])
        return data.decode() if text else data

    def read(self, container, path):
        if (container, path) not in self.files:
            raise subprocess.CalledProcessError(1, ['docker', 'exec', container, 'cat', path])
        return self.files[(container, path)]


def runtime(secrets=None, containers=None):
    data = copy.deepcopy(EXAMPLE)
    if secrets is not None:
        data['secrets'] = secrets
    if containers is not None:
        data['containers'] = containers
    return RuntimeConfig(data)


class DefaultSourceParityTests(unittest.TestCase):
    """With no "secrets" section, every service resolves exactly as before."""

    def world(self, **files):
        return patch('subprocess.check_output', FakeDocker(files))

    def test_arr_keys_match_planner_and_executor_lookups(self):
        variants = {
            'standard': RADARR_XML,
            'url base': RADARR_XML.replace('<UrlBase></UrlBase>', '<UrlBase>/radarr/</UrlBase>'),
            'single line': f'<Config><Port>7878</Port><ApiKey>{KEY}</ApiKey><UrlBase>/r</UrlBase></Config>',
            'crlf': RADARR_XML.replace('\n', '\r\n'),
        }
        for label, xml in variants.items():
            for service in ('radarr', 'sonarr'):
                files = {(service, '/config/config.xml'): xml.encode()}
                with self.subTest(label, service=service), self.world(**{}) as _:
                    with patch('subprocess.check_output', FakeDocker(files)):
                        key, url_base = read_credential(service, runtime())
                        self.assertEqual(key, old.old_planner_arr_key(service))
                        self.assertEqual((key, url_base), old.old_executor_arr(service))

    def test_jellyfin_key_matches_both_planners(self):
        for label, files in {
            'api key file': {('homepage', '/run/secrets/jellyfin_api_key'): b'jf-key\n'},
            'both files': {('homepage', '/run/secrets/jellyfin_api_key'): b'jf-key\n',
                           ('homepage', '/run/secrets/jellyfin_key'): b'other\n'},
        }.items():
            with self.subTest(label), patch('subprocess.check_output', FakeDocker(files)):
                key = read_key('jellyfin', runtime())
                self.assertEqual(key, old.old_movie_jellyfin_key('homepage'))
                self.assertEqual(key, old.old_tv_jellyfin_key('homepage'))

    def test_tmdb_matches_environment(self):
        with patch.dict(os.environ, {'TMDB_TOKEN': 'tmdb-token'}):
            self.assertEqual(read_key('tmdb', runtime()), os.environ['TMDB_TOKEN'])

    def test_documented_differences_fail_loud_or_follow_the_movie_planner(self):
        # Old planners silently used an empty Arr key (so every API call failed
        # later); the new lookup refuses up front, like the executors did.
        files = {('radarr', '/config/config.xml'): b'<Config><Port>7878</Port></Config>'}
        with patch('subprocess.check_output', FakeDocker(files)):
            self.assertEqual(old.old_planner_arr_key('radarr'), '')
            with self.assertRaises(ValueError):
                old.old_executor_arr('radarr')
            with self.assertRaisesRegex(SecretError, 'missing or empty'):
                read_key('radarr', runtime())
        # The TV planner read only jellyfin_api_key; now it falls back to
        # jellyfin_key as the movie planner always did.
        files = {('homepage', '/run/secrets/jellyfin_key'): b'jf-key\n'}
        with patch('subprocess.check_output', FakeDocker(files)):
            with self.assertRaises(subprocess.CalledProcessError):
                old.old_tv_jellyfin_key('homepage')
            self.assertEqual(read_key('jellyfin', runtime()), old.old_movie_jellyfin_key('homepage'))

    def test_no_shell_is_used(self):
        docker = FakeDocker({('radarr', '/config/config.xml'): RADARR_XML.encode(),
                             ('homepage', '/run/secrets/jellyfin_api_key'): b'jf\n'})
        with patch('subprocess.check_output', docker):
            read_key('radarr', runtime())
            read_key('jellyfin', runtime())
        self.assertTrue(all('sh' not in call for call in docker.calls), docker.calls)


class ConfiguredSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)

    def owner_only(self, name, content):
        path = self.dir / name
        path.write_text(content, encoding='utf-8')
        path.chmod(0o600)
        return str(path).replace('\\', '/') if os.name != 'posix' else str(path)

    def test_every_source_type(self):
        xml = self.dir / 'config.xml'
        xml.write_text(RADARR_XML.replace('<UrlBase></UrlBase>', '<UrlBase>/sonarr</UrlBase>'))
        secrets = {'radarr': {'source': 'env', 'variable': 'RADARR_KEY', 'url_base': '/radarr/'},
                   'sonarr': {'source': 'config_xml', 'path': xml.as_posix() if os.name == 'posix'
                              else '/' + xml.as_posix().split(':', 1)[1]},
                   'jellyfin': {'source': 'file', 'path': self.owner_only('jellyfin', 'jf-key\n')},
                   'tmdb': {'source': 'docker_file', 'container': 'vault', 'paths': ['/secrets/tmdb']}}
        if os.name != 'posix':
            self.skipTest('config_xml needs a POSIX absolute path')
        c = runtime(secrets, containers={'radarr': 'radarr', 'sonarr': 'sonarr'})
        with patch.dict(os.environ, {'RADARR_KEY': ' env-key '}), \
                patch('subprocess.check_output', FakeDocker({('vault', '/secrets/tmdb'): b'tmdb\n'})):
            self.assertEqual(read_credential('radarr', c), ('env-key', '/radarr'))
            self.assertEqual(read_credential('sonarr', c), (KEY, '/sonarr'))
            self.assertEqual(read_key('jellyfin', c), 'jf-key')
            self.assertEqual(read_key('tmdb', c), 'tmdb')

    def test_file_source_must_be_owner_only_and_outside_the_repository(self):
        path = self.owner_only('key', 'secret-value\n')
        c = runtime({'tmdb': {'source': 'file', 'path': path}})
        self.assertEqual(read_key('tmdb', c), 'secret-value')
        if os.name == 'posix':
            Path(path).chmod(0o640)
            with self.assertRaisesRegex(SecretError, 'owner only') as caught:
                read_key('tmdb', c)
            self.assertNotIn('secret-value', str(caught.exception))
        inside = ROOT / 'tests' / 'fixtures'
        c = runtime({'tmdb': {'source': 'file', 'path': str(inside)}})
        with self.assertRaises(SecretError):
            read_key('tmdb', c)

    def test_errors_never_contain_secret_values(self):
        leaky = subprocess.CalledProcessError(1, 'cat', output=b'secret-value')
        def fail(*args, **kwargs):
            raise leaky
        c = runtime({'radarr': {'source': 'docker_config_xml'}})
        with patch('subprocess.check_output', fail), self.assertRaises(SecretError) as caught:
            read_key('radarr', c)
        self.assertNotIn('secret-value', str(caught.exception))
        self.assertIn('/config/config.xml in container radarr', str(caught.exception))
        c = runtime()
        with patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(SecretError, 'TMDB_TOKEN'):
            read_key('tmdb', c)

    def test_schema_validation(self):
        for label, secrets, containers in [
            ('unknown service', {'plex': {'source': 'env', 'variable': 'X'}}, None),
            ('unknown source', {'radarr': {'source': 'vault'}}, None),
            ('missing field', {'radarr': {'source': 'file'}}, None),
            ('extra field', {'tmdb': {'source': 'env', 'variable': 'X', 'path': '/x'}}, None),
            ('xml for jellyfin', {'jellyfin': {'source': 'config_xml', 'path': '/x.xml'}}, None),
            ('url_base for tmdb', {'tmdb': {'source': 'env', 'variable': 'X', 'url_base': '/x'}}, None),
            ('bad url_base', {'radarr': {'source': 'env', 'variable': 'X', 'url_base': 'radarr'}}, None),
            ('relative xml', {'sonarr': {'source': 'config_xml', 'path': 'config.xml'}}, None),
            ('bad variable', {'tmdb': {'source': 'env', 'variable': 'TMDB-TOKEN'}}, None),
            ('bad container', {'tmdb': {'source': 'docker_file', 'container': '-it', 'paths': ['/x']}}, None),
            ('empty paths', {'jellyfin': {'source': 'docker_file', 'paths': []}}, None),
            ('no homepage for default jellyfin', {}, {'radarr': 'radarr', 'sonarr': 'sonarr'}),
        ]:
            with self.subTest(label), self.assertRaises(ConfigError):
                runtime(secrets, containers)

    def test_homepage_optional_when_jellyfin_is_configured_elsewhere(self):
        c = runtime({'jellyfin': {'source': 'env', 'variable': 'JELLYFIN_KEY'}},
                    containers={'radarr': 'radarr', 'sonarr': 'sonarr'})
        self.assertNotIn('homepage', c.containers)
        self.assertEqual(RuntimeConfig(c.as_dict()).secrets, c.secrets)

    def test_default_sources_are_the_documented_ones(self):
        c = load_config(ROOT / 'config/runtime.example.json', environ={})
        self.assertEqual(c.secrets, parse_secrets({}, c.containers))
        self.assertEqual(c.secrets['radarr'], {'source': 'docker_config_xml', 'container': 'radarr',
                                               'path': '/config/config.xml'})
        self.assertEqual(c.secrets['jellyfin']['container'], 'homepage')
        self.assertNotIn('secrets', c.as_dict())


if __name__ == '__main__':
    unittest.main()
