"""Phase 2: health checks, migratarr_doctor.py and the Settings & health page."""
import copy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from migratarr import health, settings
import migratarr_doctor
import migratarr_ui
from test_settings import Deployment, read, TARGETS

KEY = 'k3y-' + 'z' * 28
TOKEN = 'tmdb-' + 'q' * 40


class FakeHttp:
    """Answers like Radarr/Sonarr/Jellyfin/TMDB; records what was asked."""

    def __init__(self, roots=None, status=None, down=()):
        self.calls, self.status, self.down = [], status or {}, set(down)
        self.roots = roots

    def __call__(self, url, headers, local=True):
        self.calls.append((url, headers, local))
        for name in self.down:
            if name in url:
                raise OSError('connection refused')
        for name, code in self.status.items():
            if name in url:
                return code, None
        if url.endswith('/api/v3/system/status'):
            return 200, {'version': '4.0.9' if ':8989' in url else '5.2.6'}
        if url.endswith('/api/v3/rootfolder'):
            media = 'Movies' if ':7878' in url else 'TV'
            roots = self.roots if self.roots is not None else [
                f'/media/{media}/{c}' for c in ('Common', 'Current', 'Rare', 'Library', 'Archive')]
            return 200, [{'path': p, 'accessible': True} for p in roots]
        if url.endswith('/System/Info'):
            return 200, {'Version': '10.10.0'}
        if url.endswith('/3/configuration'):
            return 200, {'images': {}}
        return 404, None


def fake_docker(returncode=0, stderr=b'', raises=None):
    """`docker exec <container> test -d <path>`: records calls; answers returncode/stderr, or raises."""
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        if raises is not None:
            raise raises
        return subprocess.CompletedProcess(command, returncode, b'', stderr)
    run.calls = calls
    return run


class HealthTests(unittest.TestCase):
    def setUp(self):
        self.d = Deployment()
        self.addCleanup(self.d.close)
        # Disks as real folders, each a "mount", with category folders in them.
        targets = self.d.read('storage-targets.json')
        for t in targets['targets']:
            t['path'] = str(self.d.base / 'disks' / t['id'])
            for media in targets['category_paths'].values():
                for folder in media.values():
                    path = Path(t['path']) / folder
                    path.mkdir(parents=True, exist_ok=True)
                    os.chmod(path, 0o775)
        self.d.write('storage-targets.json', targets)
        runtime = self.d.read('runtime.json')
        runtime['storage'] = {t['id']: {'local_path': t['path'], 'remote_path': t['remote_path']}
                              for t in targets['targets']}
        self.d.write('runtime.json', runtime)
        self.secret = Path(self.d.temp.name) / 'tmdb'
        self.secret.write_text(TOKEN)
        os.chmod(self.secret, 0o600)
        runtime['secrets'] = {'radarr': {'source': 'env', 'variable': 'T_RADARR'},
                              'sonarr': {'source': 'env', 'variable': 'T_SONARR'},
                              'jellyfin': {'source': 'env', 'variable': 'T_JELLYFIN'},
                              'tmdb': {'source': 'file', 'path': str(self.secret)}}
        self.d.write('runtime.json', runtime)
        settings.migrate(environ=self.d.env)
        env = patch.dict(os.environ, {'T_RADARR': KEY, 'T_SONARR': KEY, 'T_JELLYFIN': KEY})
        env.start()
        self.addCleanup(env.stop)
        class Big:
            f_bavail, f_frsize = 900, health.GiB
        self.fs = dict(ismount=lambda p: True, statvfs=lambda p: Big())

    def nas(self, facts=None, returncode=0, stderr=''):
        calls = []

        def run(command, input=None, **kwargs):
            calls.append((command, input))
            roots = json.loads(input)
            out = facts if facts is not None else {
                'python': [3, 8, 15], 'roots': {d: {'exists': True, 'writable': True, 'free': 900 * health.GiB}
                                                for d in roots}}
            return subprocess.CompletedProcess(command, returncode, json.dumps(out) if out != '' else '', stderr)
        run.calls = calls
        return run

    def checks(self, http=None, run=None, docker=None, **fs):
        report = settings.load(environ=self.d.env)
        return health.run_checks(report, http=http or FakeHttp(), run=run or self.nas(),
                                 docker=docker or fake_docker(), **dict(self.fs, **fs))

    def by_id(self, checks):
        return {c.id: c for c in checks}

    def test_all_good(self):
        http = FakeHttp()
        checks = self.checks(http)
        self.assertEqual({c.status for c in checks}, {health.OK}, [c for c in checks if c.status != 'ok'])
        self.assertEqual(health.worst(checks), health.OK)
        ids = [c.id for c in checks]
        for expected in ('config.runtime', 'service.radarr', 'service.tmdb', 'arr_roots.sonarr',
                         'disk.media01', 'nas.ssh', 'nas.python', 'nas.disk.media04', 'permissions.group_write'):
            self.assertIn(expected, ids)
        tmdb = [c for c in http.calls if 'themoviedb' in c[0]][0]
        self.assertEqual(tmdb[1]['Authorization'], 'Bearer ' + TOKEN)
        self.assertFalse(tmdb[2])  # external: proxies allowed
        self.assertTrue(all(local for url, _, local in http.calls if 'themoviedb' not in url))

    def test_never_prints_credentials(self):
        http = FakeHttp(status={':7878': 401}, down={':8096'})
        checks = self.checks(http)
        text = json.dumps([c.as_dict() for c in checks])
        self.assertNotIn(KEY, text)
        self.assertNotIn(TOKEN, text)
        c = self.by_id(checks)
        self.assertEqual(c['service.radarr'].status, health.FAIL)
        self.assertIn('rejected the key (HTTP 401)', c['service.radarr'].detail)
        self.assertIn('cannot reach', c['service.jellyfin'].detail)
        self.assertEqual(c['arr_roots.radarr'].status, health.SKIP)

    def test_rejected_key_says_which_key_and_where(self):
        c = self.by_id(self.checks(FakeHttp(status={'themoviedb': 401, ':7878': 401})))
        tmdb, radarr = c['service.tmdb'], c['service.radarr']
        self.assertEqual(tmdb.status, health.FAIL)
        self.assertIn('API Read Access Token', tmdb.fix)
        self.assertIn('not the shorter "API Key"', tmdb.fix)
        self.assertIn('https://www.themoviedb.org/settings/api', tmdb.fix)
        self.assertNotIn('API Read Access Token', radarr.fix)  # the TMDB note is TMDB's only

    def test_hints_work_in_an_installed_copy(self):
        # Installed copies have no docs/ folder or migratarr_*.py scripts: hints use links and the command.
        self.assertTrue(health.DOCS_URL.startswith('https://github.com/'))
        for module in ('health', 'doctor', 'plan', 'reconcile', 'ui'):
            text = (Path(health.__file__).parent / (module + '.py')).read_text()
            with self.subTest(module=module):
                self.assertNotIn('python3 migratarr_', text)
                self.assertNotIn("(see docs/", text)

    def test_jellyfin_key_goes_in_the_authorization_header(self):
        # Current Jellyfin (12.4 tested 2026-10-29) refuses the legacy X-Emby-Token header.
        http = FakeHttp()
        self.checks(http)
        url, headers, _ = [c for c in http.calls if c[0].endswith('/System/Info')][0]
        self.assertNotIn('X-Emby-Token', headers)
        self.assertRegex(headers['Authorization'], r'^MediaBrowser Token="[^"]+"$')

    def test_missing_credential(self):
        with patch.dict(os.environ, {'T_SONARR': ''}):
            c = self.by_id(self.checks())
        self.assertEqual(c['service.sonarr'].status, health.FAIL)
        self.assertIn('T_SONARR', c['service.sonarr'].detail)

    def test_wrong_service_and_old_sonarr(self):
        class Http(FakeHttp):
            def __call__(self, url, headers, local=True):
                if ':8989' in url and url.endswith('status'):
                    return 200, {'version': '3.0.10'}
                if ':8096' in url:
                    return 200, {'unexpected': True}
                return super().__call__(url, headers, local)
        c = self.by_id(self.checks(Http()))
        self.assertEqual(c['service.sonarr'].status, health.WARN)
        self.assertEqual(c['service.jellyfin'].status, health.FAIL)

    def test_missing_arr_root_folder(self):
        c = self.by_id(self.checks(FakeHttp(roots=['/media/Movies/Common'])))
        self.assertEqual(c['arr_roots.radarr'].status, health.FAIL)
        self.assertIn('/media/Movies/Rare', c['arr_roots.radarr'].detail)

    def test_disks(self):
        targets = self.d.read('storage-targets.json')
        missing = Path(targets['targets'][3]['path'])
        for p in sorted(missing.rglob('*'), reverse=True):
            p.rmdir()
        missing.rmdir()
        c = self.by_id(self.checks(ismount=lambda p: not p.endswith('media02')))
        self.assertEqual(c['disk.media04'].status, health.FAIL)
        self.assertEqual(c['disk.media02'].status, health.WARN)
        self.assertIn('not a mount point', c['disk.media02'].detail)
        self.assertEqual(c['permissions.category_folders'].status, health.WARN)

    def test_reserve(self):
        class Small:
            f_bavail, f_frsize = 10, health.GiB
        c = self.by_id(self.checks(statvfs=lambda p: Small()))
        self.assertEqual(c['disk.media01'].status, health.WARN)
        self.assertIn('10 GiB free, reserve 50 GiB', c['disk.media01'].detail)

    def test_group_write(self):
        targets = self.d.read('storage-targets.json')
        item = Path(targets['targets'][0]['path']) / 'Movies/Common/Nimrods'
        item.mkdir()
        os.chmod(item, 0o2755)
        c = self.by_id(self.checks())
        self.assertEqual(c['permissions.group_write'].status, health.WARN)
        self.assertIn('Nimrods is drwxr-sr-x', c['permissions.group_write'].detail)

    def test_nas(self):
        run = self.nas()
        c = self.by_id(self.checks(run=run))
        command, payload = run.calls[0]
        self.assertEqual(command[:2], ['ssh', '-i'])
        self.assertIn('BatchMode=yes', command)
        self.assertIn('StrictHostKeyChecking=yes', command)
        self.assertEqual(json.loads(payload)['media01'], '/volume4/media01')
        self.assertEqual(c['nas.ssh'].status, health.OK)
        compile(health.NAS_PROBE, 'nas', 'exec')
        c = self.by_id(self.checks(run=self.nas('', 255, 'Host key verification failed.')))
        self.assertEqual(c['nas.ssh'].status, health.FAIL)
        self.assertIn('Host key verification failed', c['nas.ssh'].detail)
        facts = {'python': [3, 11, 2], 'roots': {'media01': {'exists': False},
                                                  'media02': {'exists': True, 'writable': False}}}
        c = self.by_id(self.checks(run=self.nas(facts)))
        self.assertEqual((c['nas.disk.media01'].status, c['nas.disk.media02'].status), (health.FAIL, health.FAIL))

    def test_nas_probe_runs(self):
        # The program shipped to the NAS, run here with a real Python against real folders.
        roots = {'a': str(self.d.base), 'b': str(self.d.base / 'nope')}
        out = subprocess.run(['python3', '-c', health.NAS_PROBE], input=json.dumps(roots), text=True,
                             capture_output=True, check=True).stdout
        facts = json.loads(out)
        self.assertTrue(facts['roots']['a']['exists'] and facts['roots']['a']['writable'])
        self.assertFalse(facts['roots']['b']['exists'])

    def test_local_topology_skips_nas_and_needs_writable_disks(self):
        report = settings.load(environ=self.d.env)
        runtime = copy.deepcopy(report.data['runtime'])
        runtime['topology'] = 'local'
        del runtime['nas']
        targets = copy.deepcopy(report.data['storage_targets'])
        for t in targets['targets']:
            del t['remote_path']
        settings.save({'runtime': runtime, 'storage_targets': targets}, report=report, environ=self.d.env)
        c = self.by_id(self.checks(ismount=lambda p: False, access=lambda p, mode: not p.endswith('media03')))
        self.assertEqual(c['nas'].status, health.SKIP)
        self.assertEqual(c['disk.media01'].status, health.OK)  # not a mount point is fine locally
        self.assertEqual(c['disk.media03'].status, health.FAIL)

    def set_arr_file_checks(self, checks, arr_root=None):
        runtime = self.d.read('runtime.json')
        runtime['arr_file_checks'] = checks
        self.d.write('runtime.json', runtime)
        if arr_root:
            targets = self.d.read('storage-targets.json')
            targets['arr_root'] = arr_root
            self.d.write('storage-targets.json', targets)

    def test_arr_file_checks_in_docker_mode(self):
        docker = fake_docker()
        c = self.by_id(self.checks(docker=docker))
        self.assertEqual((c['arr_files.radarr'].status, c['arr_files.sonarr'].status), (health.OK, health.OK))
        command, kwargs = docker.calls[0]
        self.assertEqual(command, ['docker', 'exec', 'radarr', 'test', '-d', '/media'])
        self.assertTrue(kwargs.get('capture_output'))  # Docker's own output never reaches our stderr
        c = self.by_id(self.checks(docker=fake_docker(1)))
        self.assertEqual(c['arr_files.radarr'].status, health.WARN)
        self.assertEqual(c['arr_files.radarr'].detail, 'container radarr does not see /media')
        # Docker exits 1 for its own failures too: the captured message decides.
        down = fake_docker(1, b'Cannot connect to the Docker daemon at unix:///var/run/docker.sock.')
        c = self.by_id(self.checks(docker=down))
        self.assertEqual(c['arr_files.sonarr'].status, health.WARN)
        self.assertIn('Docker is not running or not reachable', c['arr_files.sonarr'].detail)
        self.assertIn('installed directly on this machine', c['arr_files.sonarr'].fix)
        self.assertNotIn('unix://', c['arr_files.sonarr'].detail)
        c = self.by_id(self.checks(docker=fake_docker(raises=FileNotFoundError('docker'))))
        self.assertIn('Docker is not installed', c['arr_files.radarr'].detail)

    def test_host_mode_checks_this_machine_and_never_runs_docker(self):
        media, arr_root = self.d.base / 'arr-media', str(self.d.base / 'arr-root')
        self.set_arr_file_checks({'radarr': {'mode': 'host'}, 'sonarr': {'mode': 'host', 'host_root': str(media)}},
                                 arr_root)
        docker = fake_docker(raises=AssertionError('docker must not run in host mode'))
        c = self.by_id(self.checks(docker=docker))
        self.assertEqual(docker.calls, [])
        self.assertEqual(c['arr_files.radarr'].status, health.WARN)  # arr_root is not a folder here
        self.assertIn(arr_root + ' is not a folder here', c['arr_files.radarr'].detail)
        self.assertIn('runs in Docker, say so in Settings', c['arr_files.radarr'].fix)
        self.assertEqual(c['arr_files.sonarr'].status, health.WARN)
        media.mkdir()
        c = self.by_id(self.checks(docker=docker))
        self.assertEqual(c['arr_files.sonarr'].status, health.OK)
        self.assertEqual(c['arr_files.sonarr'].detail, 'on this machine: ' + str(media))

    def test_docker_credential_failure_suggests_host_mode(self):
        runtime = self.d.read('runtime.json')
        runtime['secrets']['radarr'] = {'source': 'docker_config_xml'}
        self.d.write('runtime.json', runtime)

        def missing(*args, **kwargs):
            raise FileNotFoundError('docker')
        with patch('subprocess.check_output', missing):
            c = self.by_id(self.checks())
        self.assertEqual(c['service.radarr'].status, health.FAIL)
        self.assertIn('Docker is not installed', c['service.radarr'].detail)
        self.assertIn('installed directly on this machine', c['service.radarr'].fix)

    def test_broken_settings_skip_the_rest(self):
        runtime = self.d.read('runtime.json')
        runtime['urls']['radarr'] = 'nope'
        self.d.write('runtime.json', runtime)
        http = FakeHttp()
        checks = self.checks(http)
        self.assertEqual(http.calls, [])
        c = self.by_id(checks)
        self.assertEqual(c['config.runtime'].status, health.FAIL)
        self.assertIn('urls.radarr', c['config.runtime'].detail)
        self.assertEqual(c['skipped'].status, health.SKIP)


class DoctorTests(unittest.TestCase):
    def setUp(self):
        self.d = Deployment()
        self.addCleanup(self.d.close)
        env = patch.dict(os.environ, self.d.env)
        env.start()
        self.addCleanup(env.stop)

    def test_offline_text_and_exit_status(self):
        out = io.StringIO()
        self.assertEqual(migratarr_doctor.main(['--offline'], out), 0)
        text = out.getvalue()
        self.assertIn('[warn] runtime.json: version 1', text)
        self.assertIn('--migrate', text)
        runtime = self.d.read('runtime.json')
        runtime['nas']['host'] = 'bad host'
        self.d.write('runtime.json', runtime)
        out = io.StringIO()
        self.assertEqual(migratarr_doctor.main(['--offline'], out), 1)
        self.assertIn('[FAIL] runtime.json: nas:', out.getvalue())

    def test_migrate_and_json(self):
        out = io.StringIO()
        self.assertEqual(migratarr_doctor.main(['--offline', '--migrate', '--json'], out), 0)
        data = json.loads(out.getvalue())
        self.assertIn('Migrated runtime.json to version 2', data['migration'])
        self.assertEqual(self.d.read('runtime.json')['schema_version'], 2)
        self.assertEqual([c['status'] for c in data['checks']], ['ok', 'ok', 'ok'])

    def test_no_docker_output_on_stderr(self):
        # migratarr doctor as a user runs it, on a machine whose docker can't reach its daemon.
        with tempfile.TemporaryDirectory() as tmp:
            shim = Path(tmp) / 'docker'
            shim.write_text('#!/bin/sh\necho "failed to connect to the docker API at unix:///x.sock" >&2\nexit 1\n')
            shim.chmod(0o755)
            root = Path(__file__).resolve().parents[1]
            env = dict(os.environ, **self.d.env, PATH=tmp + os.pathsep + os.environ.get('PATH', ''),
                       PYTHONPATH=str(root))
            env.pop('TMDB_TOKEN', None)
            out = subprocess.run([sys.executable, '-m', 'migratarr.cli', 'doctor', '--migrate',
                                  '--only', 'services,arr_files'], capture_output=True, text=True, env=env,
                                 timeout=120, cwd=tmp)
        self.assertEqual(out.stderr, '')
        self.assertIn('[FAIL] Radarr: cannot read radarr credential (docker_config_xml /config/config.xml in '
                      'container radarr): Docker is not running or not reachable', out.stdout)
        self.assertIn('[warn] Radarr file checks: in container radarr: Docker is not running or not reachable',
                      out.stdout)
        self.assertNotIn('docker API', out.stdout)

    def test_doctor_in_host_mode_runs_no_docker(self):
        runtime = self.d.read('runtime.json')
        runtime['arr_file_checks'] = {'radarr': {'mode': 'host'}, 'sonarr': {'mode': 'host'}}
        runtime['secrets'] = {'radarr': {'source': 'env', 'variable': 'NO_KEY'},
                              'sonarr': {'source': 'env', 'variable': 'NO_KEY'}}
        self.d.write('runtime.json', runtime)
        targets = self.d.read('storage-targets.json')
        targets['arr_root'] = str(self.d.base / 'arr-root')
        self.d.write('storage-targets.json', targets)
        out = io.StringIO()
        with patch('subprocess.run', side_effect=AssertionError('docker must not run')), \
                patch('subprocess.check_output', side_effect=AssertionError('docker must not run')), \
                patch('migratarr.health.subprocess.run', side_effect=AssertionError('docker must not run')):
            migratarr_doctor.main(['--migrate', '--only', 'arr_files'], out)
        self.assertIn(f'Radarr file checks: on this machine: {self.d.base}/arr-root is not a folder here',
                      out.getvalue())

    def test_unknown_group(self):
        with self.assertRaises(SystemExit), patch('sys.stderr', io.StringIO()):
            migratarr_doctor.main(['--only', 'everything'])


class SettingsPageTests(unittest.TestCase):
    def setUp(self):
        self.d = Deployment()
        self.addCleanup(self.d.close)
        settings.migrate(environ=self.d.env)
        self.calls = []

        def fake_health(report):
            self.calls.append(report)
            return [health.Check('service.radarr', 'services', 'Radarr', health.FAIL, 'cannot reach', 'Start it'),
                    health.Check('disk.media01', 'disks', 'Disk media01', health.OK, '/mnt/nas/media01')]
        self.health = fake_health
        self.load = lambda: settings.load(environ=self.d.env)

    def app(self, **kwargs):
        return migratarr_ui.App(self.d.base, health=self.health, load_settings=self.load, **kwargs)

    def test_page_without_and_with_checks(self):
        status, kind, body = self.app().respond('/settings', {})
        page = body.decode()
        self.assertEqual(status, 200)
        self.assertIn('Settings &amp; health', page)
        self.assertIn('Run health checks', page)
        self.assertIn('environment variable TMDB_TOKEN', page)
        self.assertIn('/volume4/media01', page)
        self.assertEqual(self.calls, [])  # opening the page contacts nothing
        status, _, body = self.app().respond('/settings', {'check': ['1']})
        self.assertIn('Start it', body.decode())
        self.assertEqual(len(self.calls), 1)
        status, kind, body = self.app().respond('/api/settings', {})
        data = json.loads(body)
        self.assertEqual(data['runtime']['topology'], 'nas_ssh')
        self.assertIsNone(data['checks'])

    def test_page_never_shows_values(self):
        with patch.dict(os.environ, {'TMDB_TOKEN': TOKEN}):
            for path in ('/settings', '/api/settings'):
                self.assertNotIn(TOKEN, self.app().respond(path, {'check': ['1']})[2].decode())

    def test_every_page_links_to_settings(self):
        self.assertIn('href="/settings"', self.app().respond('/', {})[2].decode())

    def test_setup_mode_when_settings_are_broken(self):
        runtime = self.d.read('runtime.json')
        runtime['topology'] = 'cloud'
        self.d.write('runtime.json', runtime)
        app = self.app(actions=True, setup=True)
        self.assertFalse(app.actions)
        status, _, body = app.respond('/', {})
        self.assertEqual(status, 200)
        self.assertIn("can't start normally", body.decode())
        self.assertIn('topology must be one of', body.decode())
        self.assertEqual(app.respond('/run/20261023T212605Z', {})[0], 404)
        self.assertEqual(app.respond_post('/plan', {'token': [app.token]})[0], 405)


if __name__ == '__main__':
    unittest.main()
