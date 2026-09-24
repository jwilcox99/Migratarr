"""Service base URLs: runtime.json urls may carry a base path; UrlBase fills in otherwise."""
import copy
import importlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from planner_settings import load_settings
from runtime_config import ConfigError, RuntimeConfig, load_config
from service_keys import service_endpoint
from storage_targets import load_targets


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = json.loads((ROOT / 'config/runtime.example.json').read_text(encoding='utf-8'))
EXECUTORS = {'execute_movie': ('Radarr', 'radarr'), 'execute_movie_nas': ('Radarr', 'radarr'),
             'execute_cross_movie': ('Radarr', 'radarr'), 'execute_cross_tv': ('Sonarr', 'sonarr'),
             'execute_tv_nas': ('Sonarr', 'sonarr')}


def runtime(**urls):
    data = copy.deepcopy(EXAMPLE)
    data['urls'].update(urls)
    return RuntimeConfig(data)


def config_xml(url_base=''):
    return f'<Config><ApiKey>k</ApiKey><UrlBase>{url_base}</UrlBase></Config>'.encode()


class UrlValidationTests(unittest.TestCase):
    def test_base_paths_accepted(self):
        for url in ('http://localhost:7878', 'http://host:7878/radarr', 'https://proxy.example/media/radarr'):
            with self.subTest(url):
                self.assertEqual(runtime(radarr=url).urls['radarr'], url)

    def test_noncanonical_urls_refused(self):
        for url in ('http://host/', 'http://host/radarr/', 'http://host//radarr', 'http://host/a/./b',
                    'http://host/a/../b', 'http://host/radarr?x=1', 'http://host/radarr#x',
                    'http://user:pw@host/radarr', 'ftp://host/radarr', 'http://host/ra dar'):
            with self.subTest(url), self.assertRaises(ConfigError):
                runtime(radarr=url)


class EndpointRuleTests(unittest.TestCase):
    def test_path_in_urls_wins_else_url_base(self):
        for url, url_base, expected in (
                ('http://h:7878', '', 'http://h:7878'),
                ('http://h:7878', '/radarr', 'http://h:7878/radarr'),
                ('http://h:7878/radarr', '', 'http://h:7878/radarr'),
                ('http://proxy/movies', '/radarr', 'http://proxy/movies')):
            with self.subTest(url=url, url_base=url_base), \
                    patch('subprocess.check_output', return_value=config_xml(url_base)):
                self.assertEqual(service_endpoint('radarr', runtime(radarr=url)), (expected, 'k'))

    def test_jellyfin_uses_its_url_as_given(self):
        c = runtime(jellyfin='http://h:8096/jellyfin')
        with patch('subprocess.check_output', return_value=b'jf\n'):
            self.assertEqual(service_endpoint('jellyfin', c), ('http://h:8096/jellyfin', 'jf'))


class ExecutorClientTests(unittest.TestCase):
    """Every executor's real Radarr/Sonarr client builds its API URL the same way."""

    @classmethod
    def setUpClass(cls):
        config = load_config(ROOT / 'config/runtime.example.json', environ={})
        with patch('runtime_config.get_config', return_value=config), \
                patch('planner_settings.get_settings', return_value=load_settings(ROOT / 'config/planner.example.json')), \
                patch('media_layout.get_targets', return_value=load_targets(ROOT / 'config/storage-targets.example.json')):
            cls.modules = {name: importlib.import_module(name) for name in EXECUTORS}

    def request_url(self, name, c, url_base):
        m = self.modules[name]
        cls, _ = EXECUTORS[name]
        with patch.object(m, 'RUNTIME', c), patch('subprocess.check_output', return_value=config_xml(url_base)):
            client = getattr(m, cls)()
        response = Mock()
        response.read.return_value = b'{}'
        opener = Mock()
        opener.open.return_value.__enter__ = Mock(return_value=response)
        opener.open.return_value.__exit__ = Mock(return_value=False)
        client.opener = opener
        with patch.object(m, 'RUNTIME', c):
            client.api('system/status')
        return opener.open.call_args.args[0].full_url

    def test_all_executors_agree(self):
        for name, (_, service) in EXECUTORS.items():
            port = '7878' if service == 'radarr' else '8989'
            for url, url_base, expected in (
                    # This deployment's case: no base path anywhere; unchanged for every executor.
                    (f'http://localhost:{port}', '', f'http://localhost:{port}/api/v3/system/status'),
                    # UrlBase in config.xml: already honored by the cross-disk and TV NAS
                    # executors; execute_movie.py and execute_movie_nas.py used to drop it.
                    (f'http://localhost:{port}', f'/{service}', f'http://localhost:{port}/{service}/api/v3/system/status'),
                    # Base path written in runtime.json urls (previously refused by validation).
                    (f'http://proxy/{service}', '', f'http://proxy/{service}/api/v3/system/status')):
                with self.subTest(executor=name, url=url, url_base=url_base):
                    self.assertEqual(self.request_url(name, runtime(**{service: url}), url_base), expected)


class PlannerUrlTests(unittest.TestCase):
    """Both dry-run planners send every Arr/Jellyfin request under the configured base."""

    def test_planners_use_base_paths(self):
        from migratarr_validation.dry_run_parity import record
        from synthetic_library import NOW, offline_world
        import synthetic_library
        data = copy.deepcopy(EXAMPLE)
        data['urls'] = {'radarr': 'http://localhost:7878/radarr', 'sonarr': 'http://localhost:8989',
                        'jellyfin': 'http://localhost:8096/jellyfin'}
        c = RuntimeConfig(data)
        seen = []
        route = synthetic_library.route

        def routed(url):
            seen.append(url)
            for prefix in ('/radarr/', '/sonarr/', '/jellyfin/'):
                url = url.replace(prefix, '/', 1) if '//localhost' in url else url
            return route(url)

        def docker(command, text=False, **_):
            data = (config_xml('/sonarr').decode() if 'sonarr' in command and command[-1] == '/config/config.xml'
                    else config_xml().decode() if command[-1] == '/config/config.xml' else 'fake-key\n')
            return data if text else data.encode()

        with tempfile.TemporaryDirectory() as temp, offline_world(), \
                patch('runtime_config.get_config', return_value=c), \
                patch.object(synthetic_library, 'route', routed), \
                patch('subprocess.check_output', docker), \
                patch('sys.stderr', io.StringIO()):
            for kind in ('movie', 'tv'):
                record(kind, Path(temp) / f'{kind}.json', cache_dir=Path(temp) / 'cache', now=NOW)
                recording = json.loads((Path(temp) / f'{kind}.json').read_text(encoding='utf-8'))
                service = 'radarr' if kind == 'movie' else 'sonarr'
                expected = {'radarr': 'http://localhost:7878/radarr', 'sonarr': 'http://localhost:8989/sonarr'}
                self.assertEqual(recording['endpoints'][service], expected[service])
                self.assertEqual(recording['endpoints']['jellyfin'], 'http://localhost:8096/jellyfin')
        local = [u for u in seen if '//localhost' in u]
        self.assertTrue(local)
        for url in local:
            self.assertTrue(url.startswith(('http://localhost:7878/radarr/api/v3/', 'http://localhost:8989/sonarr/api/v3/',
                                            'http://localhost:8096/jellyfin/')), url)


if __name__ == '__main__':
    unittest.main()
