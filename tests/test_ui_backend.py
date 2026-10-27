"""migratarr.settings.ui_backend: the settings pages and the wizard over the real settings layer.

Each test gets its own config folder, secrets folder and base path (environment
overrides only; nothing outside the temporary folder is read or written).
"""
import fcntl
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

import migratarr_ui
import settings_contract as sc
import settings_ui
import setup_wizard
from migratarr import health
from migratarr import settings as store
from migratarr.settings.ui_backend import UiBackend, problem_path, read_mounts
from test_ui import fake_usage

TOKEN = 'test-token'
KEY = 'PASTED-RADARR-KEY-0123456789'


def offline_http(url, headers, local=True, timeout=10):
    raise ConnectionRefusedError('offline in tests')


def no_ssh(*args, **kwargs):
    raise FileNotFoundError('ssh')


class Env:
    def make(self, configured=True, topology='nas_ssh'):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.env = {'MIGRATARR_CONFIG': str(self.root / 'config/runtime.json'),
                    'MIGRATARR_PLANNER_CONFIG': str(self.root / 'config/planner.json'),
                    'MIGRATARR_SECRETS_DIR': str(self.root / 'secrets'), 'HOME': str(self.root)}
        self.base = self.root / 'base'
        self.backend = UiBackend(self.env, base=self.base, http=offline_http, run=no_ssh)
        if configured:
            self.backend.create(self.draft(topology))
        return self.backend

    def draft(self, topology='nas_ssh', key=KEY):
        b, d = self.backend, {}
        for s in setup_wizard.STEPS:
            d[s] = b.defaults(s)
        d[sc.SERVICES].update({'services.radarr.source': 'paste', 'services.radarr.key': key,
                               # environment sources fail fast offline (no docker in tests)
                               'services.sonarr.source': 'env', 'services.sonarr.variable': 'NO_SONARR_KEY',
                               'services.jellyfin.source': 'env', 'services.jellyfin.variable': 'NO_JF_KEY',
                               'services.tmdb.variable': 'NO_TMDB_TOKEN'})
        d[sc.TOPOLOGY].update({'topology.mode': topology, 'topology.base_path': str(self.base)})
        if topology == 'nas_ssh':
            d[sc.TOPOLOGY].update({'topology.nas.host': 'nas.example', 'topology.nas.user': 'mig',
                                   'topology.nas.ssh_key': str(self.root / 'nas_key')})
        d[sc.DISKS], _ = b.normalize(sc.DISKS, d[sc.DISKS], d)
        d[sc.DISKS].update({'disks.media01.path': '/mnt/nas/media01', 'disks.media01.remote_path': '/volume1/m1'})
        d[sc.STREAMING]['streaming.region'] = 'US'
        return d

    def files_text(self):
        return ''.join(p.read_text() for p in (self.root / 'config').glob('*.json'))


class FreshInstallTests(Env, unittest.TestCase):
    def test_not_configured_until_created(self):
        b = self.make(configured=False)
        self.assertFalse(b.status().configured)
        self.assertEqual(b.status().problems, ())
        self.assertEqual(b.defaults(sc.TOPOLOGY)['topology.mode'], 'nas_ssh')
        self.assertEqual(b.defaults(sc.STREAMING)['streaming.region'], '')  # asked, not assumed

    def test_create_writes_three_valid_files_and_an_owner_only_key(self):
        b = self.make(configured=False)
        draft = self.draft()
        for step in setup_wizard.STEPS:
            self.assertEqual(sc.blocking(b.validate(step, draft[step], draft)), [], step)
        result = b.create(draft)
        self.assertEqual(len(result.files), 3)
        report = store.load(environ=self.env)
        self.assertEqual(report.missing, [])
        self.assertEqual(report.errors, [])
        self.assertTrue(b.status().configured)
        key_file = self.root / 'secrets/radarr'
        self.assertEqual(stat.S_IMODE(key_file.stat().st_mode), 0o600)
        self.assertEqual(key_file.read_text().strip(), KEY)
        self.assertNotIn(KEY, self.files_text())
        self.assertEqual(report.data['runtime']['secrets']['radarr'], {'source': 'file', 'path': str(key_file)})
        self.assertIs(b.current(sc.SERVICES)['services.radarr.key'], sc.SECRET_KEPT)
        self.assertEqual(b.current(sc.SERVICES)['services.radarr.source'], 'paste')
        with self.assertRaises(sc.SaveRefused):
            b.create(draft)

    def test_local_topology_has_no_nas_and_no_remote_paths(self):
        b = self.make(configured=False)
        draft = self.draft('local')
        self.assertNotIn('disks.media01.remote_path', {f.path for f in b.fields(sc.DISKS, draft[sc.DISKS])})
        b.create(draft)
        report = store.load(environ=self.env)
        self.assertEqual(report.errors, [])
        self.assertEqual(report.runtime.topology, 'local')
        self.assertNotIn('nas', report.data['runtime'])
        self.assertNotIn('remote_path', report.data['storage_targets']['targets'][0])
        self.assertIn('local', b.capabilities().topologies)
        self.assertIn('local', b.capabilities().experimental)

    def test_missing_paste_and_bad_values_are_reported_on_their_fields(self):
        b = self.make(configured=False)
        draft = self.draft(key=None)
        problems = b.validate(sc.SERVICES, draft[sc.SERVICES], draft)
        self.assertIn('services.radarr.key', {p.path for p in problems})
        draft[sc.STREAMING]['streaming.region'] = 'usa'
        self.assertEqual({p.path for p in b.validate(sc.STREAMING, draft[sc.STREAMING], draft)},
                         {'streaming.region'})

    def test_draft_checks_skip_a_service_whose_key_is_not_stored_yet(self):
        b = self.make(configured=False)
        checks = {c.id: c for c in b.run_checks(self.draft())}
        self.assertEqual(checks['service.radarr'].status, sc.SKIPPED)
        self.assertEqual(checks['service.sonarr'].status, sc.FAIL)  # offline
        self.assertFalse((self.root / 'config').exists())  # checking a draft writes nothing


class SettingsEditTests(Env, unittest.TestCase):
    def setUp(self):
        self.make()

    def test_save_changes_one_file_with_a_backup(self):
        b = self.backend
        values = dict(b.current(sc.SERVICES), **{'services.sonarr.url': 'http://sonarr.lan:8989'})
        self.assertEqual(sc.blocking(b.validate(sc.SERVICES, values)), [])
        changes = b.diff(sc.SERVICES, values)
        self.assertEqual([c.path for c in changes], ['services.sonarr.url'])
        result = b.save(sc.SERVICES, values, b.status().revision)
        self.assertEqual(result.files, (self.env['MIGRATARR_CONFIG'],))
        self.assertEqual(store.load(environ=self.env).data['runtime']['urls']['sonarr'], 'http://sonarr.lan:8989')
        self.assertTrue(list((self.root / 'config/backups').glob('*/runtime.json')))

    def test_new_pasted_key_replaces_the_file_and_never_reaches_diff_or_files(self):
        b = self.backend
        values = dict(b.current(sc.SERVICES), **{'services.radarr.key': 'NEW-KEY-9876543210'})
        changes = b.diff(sc.SERVICES, values)
        self.assertEqual([(c.path, c.after) for c in changes], [('services.radarr.key', sc.SECRET_NEW)])
        self.assertNotIn('NEW-KEY', repr(changes))
        b.save(sc.SERVICES, values, b.status().revision)
        self.assertEqual((self.root / 'secrets/radarr').read_text().strip(), 'NEW-KEY-9876543210')
        self.assertNotIn('NEW-KEY', self.files_text())

    def test_refused_when_files_changed_or_a_job_runs(self):
        b = self.backend
        values = dict(b.current(sc.STREAMING), **{'streaming.region': 'GB'})
        stale = b.status().revision
        path = Path(self.env['MIGRATARR_PLANNER_CONFIG'])
        path.write_text(path.read_text().replace('"US"', '"CA"'))
        with self.assertRaisesRegex(sc.SaveRefused, 'changed since you reviewed'):
            b.save(sc.STREAMING, values, stale)
        lock = self.base / 'jobs/runner.lock'
        lock.parent.mkdir(parents=True, exist_ok=True)
        with open(lock, 'a') as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            self.assertTrue(b.status().job_running)
            with self.assertRaisesRegex(sc.SaveRefused, 'job'):
                b.save(sc.STREAMING, values, b.status().revision)
        self.assertIn('"CA"', path.read_text())

    def test_add_and_remove_a_disk(self):
        b = self.backend
        values = dict(b.current(sc.DISKS), **{'disks._new.id': 'media02', 'disks._new.path': '/mnt/nas/media02',
                                               'disks._new.remote_path': '/volume2/m2'})
        values, notes = b.normalize(sc.DISKS, values)
        self.assertEqual([n.severity for n in notes], [sc.WARN])
        self.assertIn('disks.media02.path', {f.path for f in b.fields(sc.DISKS, values)})
        values['disks._placement.Movie.Archive'] = ['media01', 'media02']
        b.save(sc.DISKS, values, b.status().revision)
        targets = store.load(environ=self.env).data['storage_targets']
        self.assertEqual([t['id'] for t in targets['targets']], ['media01', 'media02'])
        self.assertEqual(targets['placement']['Movie']['Archive'], ['media01', 'media02'])
        values = dict(b.current(sc.DISKS), **{'disks.media02.remove': True})
        values, notes = b.normalize(sc.DISKS, values)
        self.assertEqual(notes, [])
        self.assertIn(('disks.media02.path', '/mnt/nas/media02', None),
                      [(c.path, c.before, c.after) for c in b.diff(sc.DISKS, values)])
        b.save(sc.DISKS, values, b.status().revision)
        targets = store.load(environ=self.env).data['storage_targets']
        self.assertEqual([t['id'] for t in targets['targets']], ['media01'])
        self.assertEqual(targets['placement']['Movie']['Archive'], ['media01'])

    def test_disk_problems_point_at_the_disk(self):
        b = self.backend
        values = dict(b.current(sc.DISKS), **{'disks.media01.media_types': ['TV']})  # Movie categories use it
        paths = {p.path for p in sc.blocking(b.validate(sc.DISKS, values))}
        self.assertTrue(paths and all(p.startswith('disks') for p in paths), paths)
        with self.assertRaises(sc.SaveRefused):
            b.save(sc.DISKS, values, b.status().revision)

    def test_topology_is_read_only_once_set_up(self):
        mode = [f for f in self.backend.fields(sc.TOPOLOGY, {}) if f.path == 'topology.mode'][0]
        self.assertTrue(mode.read_only)

    def test_unreadable_json_is_not_overwritten(self):
        Path(self.env['MIGRATARR_PLANNER_CONFIG']).write_text('{not json')
        b = self.backend
        problems = b.status().problems
        self.assertTrue(any(p.path.startswith('streaming') for p in problems))
        with self.assertRaisesRegex(sc.SaveRefused, 'not readable JSON'):
            b.save(sc.STREAMING, {'streaming.region': 'US'}, b.status().revision)

    def test_checks_map_doctor_statuses_and_fixes(self):
        checks = self.backend.run_checks()
        ours = {c.id: c for c in checks}
        theirs = {c.id: c for c in health.run_checks(store.load(environ=self.env), environ=self.env,
                                                     http=offline_http, run=no_ssh)}
        self.assertEqual(set(ours), set(theirs))  # one check list for UI, doctor and plan
        for cid, c in theirs.items():
            self.assertEqual(ours[cid].status, {'skip': 'skipped'}.get(c.status, c.status))
            self.assertEqual(ours[cid].fix, c.fix)


class ProblemPathTests(unittest.TestCase):
    def test_paths(self):
        P = store.Problem
        cases = {P('runtime', 'urls.radarr', 'm'): 'services.radarr.url',
                 P('runtime', 'secrets.sonarr', 'm'): 'services.sonarr.source',
                 P('runtime', 'containers.homepage', 'm'): 'services.jellyfin.container',
                 P('runtime', 'nas.host', 'm'): 'topology.nas.host',
                 P('runtime', 'base_path', 'm'): 'topology.base_path',
                 P('storage_targets', 'targets[2] (media03)', 'm'): 'disks.media03',
                 P('storage_targets', 'placement.Movie', 'm'): 'disks',
                 P('planner', 'streaming.region', 'm'): 'streaming.region',
                 P('planner', 'scoring.weights', 'm'): 'scoring',
                 P('planner', 'file', 'm'): 'streaming'}
        for problem, path in cases.items():
            self.assertEqual(problem_path(problem)[0], path, problem)


class DiscoveryTests(Env, unittest.TestCase):
    def test_services_report_a_key_without_returning_it(self):
        b = self.make(configured=False)
        xml = b'<Config><ApiKey>SECRET-FROM-XML</ApiKey><UrlBase>/radarr</UrlBase></Config>'

        def cat(container, path):
            if container == 'radarr':
                return xml
            raise FileNotFoundError('docker')
        b.docker_cat = cat
        b.environ['TMDB_TOKEN'] = 'TMDB-SECRET'
        found = b.discover(sc.SERVICES, {sc.SERVICES: {}})
        text = repr(found)
        self.assertNotIn('SECRET-FROM-XML', text)
        self.assertNotIn('TMDB-SECRET', text)
        self.assertEqual(found.values['services.radarr.source'], 'docker_config_xml')
        self.assertIn('URL base /radarr', ' '.join(found.notes))
        self.assertIn('sonarr', ' '.join(found.notes).lower())

    def test_disks_suggest_mounts_for_empty_rows(self):
        b = self.make(configured=False)
        b.read_mounts = lambda: ['/mnt/nas/media01', '/mnt/nas/media02']
        draft = {sc.DISKS: b.defaults(sc.DISKS)}
        found = b.discover(sc.DISKS, draft)
        self.assertEqual(found.values, {'disks.media01.path': '/mnt/nas/media01'})
        self.assertIn('/mnt/nas/media02', ' '.join(found.notes))

    def test_read_mounts_filters_system_filesystems(self):
        with tempfile.NamedTemporaryFile('w', delete=False) as f:
            f.write('proc /proc proc rw 0 0\n/dev/sdb1 /mnt/media01 ext4 rw 0 0\n'
                    'tmpfs /mnt/tmp tmpfs rw 0 0\nnas:/v1 /media/my\\040disk nfs rw 0 0\n/dev/sda1 / ext4 rw 0 0\n')
        self.addCleanup(os.unlink, f.name)
        self.assertEqual(read_mounts(f.name), ['/mnt/media01', '/media/my disk'])


class AppTests(Env, unittest.TestCase):
    """The UI as main() builds it, against real files."""

    def app(self, configured=True, gate=None):
        self.make(configured=configured)
        self.started = []
        app = migratarr_ui.build_app(self.base, actions=True, backend=self.backend)
        app.token, app.usage = TOKEN, fake_usage
        app.start_job = lambda base, kind, *a: self.started.append(kind) or '20261026T120000Z-' + kind
        if gate is not None:
            app.plan_gate = gate
        return app

    def get(self, app, path, query=None):
        result = app.respond(path, query)
        return (303, result[1]) if result[0] == 'redirect' else (result[0], result[2].decode())

    def post(self, app, path, **fields):
        fields.setdefault('token', TOKEN)
        result = app.respond_post(path, {k: v if isinstance(v, list) else [v] for k, v in fields.items()})
        return (303, result[1]) if result[0] == 'redirect' else (result[0], result[2].decode())

    def test_fresh_install_runs_the_wizard_then_the_ui(self):
        app = self.app(configured=False)
        self.assertEqual(self.get(app, '/'), (303, '/setup'))
        draft = self.draft()
        f = lambda values: {'f:' + k: (v if isinstance(v, list) else ['on'] if v is True else [] if v is False
                                       else ['' if v is None else str(v)]) for k, v in values.items()
                            if not k.startswith('disks._') or k.startswith(('disks._folders', 'disks._placement',
                                                                            'disks._defaults', 'disks._arr_root'))}
        services = dict(draft[sc.SERVICES])
        self.assertEqual(self.post(app, '/setup/services', action='next', **f(services)), (303, '/setup/topology'))
        status, body = self.get(app, '/setup/topology')
        self.assertIn('experimental', body)
        self.assertEqual(self.post(app, '/setup/topology', action='next', **f(draft[sc.TOPOLOGY])),
                         (303, '/setup/disks'))
        status, body = self.get(app, '/setup/disks')
        self.assertIn('Path on the NAS', body)
        self.assertEqual(self.post(app, '/setup/disks', action='next', **f(draft[sc.DISKS])), (303, '/setup/streaming'))
        self.assertEqual(self.post(app, '/setup/streaming', action='next', **f(draft[sc.STREAMING])),
                         (303, '/setup/review'))
        status, body = self.get(app, '/setup/review')
        self.assertEqual(status, 200, body)
        self.assertNotIn(KEY, body)
        self.assertEqual(self.post(app, '/setup/review', action='save'), (303, '/setup/done'))
        self.assertEqual(store.load(environ=self.env).errors, [])
        self.assertNotIn(KEY, self.files_text())
        self.assertFalse(app.setup_mode())
        self.assertEqual(app.base, self.base)

    def test_plan_refused_while_a_check_fails_and_allowed_with_warnings(self):
        failing = [sc.CheckResult('nas.ssh', 'NAS over SSH', sc.FAIL, 'Permission denied', fix='Check the key')]
        app = self.app(gate=lambda: list(failing))
        status, body = self.post(app, '/plan')
        self.assertEqual(status, 409)
        self.assertIn('NAS over SSH', body)
        self.assertIn('/settings?check=1', body)
        self.assertEqual(self.started, [])
        self.assertIn('Planning is unavailable', self.get(app, '/')[1])  # the runs page says why
        failing.clear()  # only warnings left
        self.assertEqual(self.post(app, '/plan'), (303, '/job/20261026T120000Z-plan'))
        self.assertEqual(self.started, ['plan'])
        self.assertNotIn('Planning is unavailable', self.get(app, '/')[1])

    def test_default_gate_uses_the_backend_checks(self):
        app = self.app()  # offline: every service check fails
        status, body = self.post(app, '/plan')
        self.assertEqual(status, 409)
        self.assertIn('Radarr', body)
        self.assertEqual(self.started, [])

    def test_saving_still_works_while_checks_fail(self):
        app = self.app(gate=lambda: [sc.CheckResult('x', 'X', sc.FAIL)])
        current = self.backend.current(sc.STREAMING)
        status, body = self.post(app, '/settings/streaming/review', **{'f:streaming.region': 'GB',
                                                                       'f:streaming.subscribed': 'Hulu'})
        self.assertEqual(status, 200, body)
        nonce = body.split('name="change" value="')[1].split('"')[0]
        self.assertEqual(self.post(app, '/settings/streaming/save', change=nonce)[0], 303)
        self.assertEqual(store.load(environ=self.env).data['planner']['streaming']['region'], 'GB')
        self.assertNotEqual(current['streaming.region'], 'GB')

    def test_settings_page_probes_only_on_request(self):
        calls = []
        app = self.app()
        self.backend.http = lambda *a, **k: calls.append(a) or offline_http(*a, **k)
        status, body = self.get(app, '/settings')
        self.assertEqual((status, calls), (200, []))
        for text in ('Services', 'Disks and categories', 'Effective settings', 'Run health checks'):
            self.assertIn(text, body)
        status, body = self.get(app, '/settings', {'check': ['1']})
        self.assertTrue(calls)
        self.assertIn('Radarr', body)
        self.assertNotIn(KEY, body)
        self.assertEqual(json.loads(self.get(app, '/api/settings')[1])['report']['missing'], [])

    def test_invalid_files_allow_settings_but_refuse_plan(self):
        self.make()
        path = Path(self.env['MIGRATARR_PLANNER_CONFIG'])
        path.write_text(path.read_text().replace('"US"', '"usa"'))
        app = migratarr_ui.App(self.base, setup=True, actions=True, token=TOKEN,
                               settings=settings_ui.SettingsPages(self.backend, self.backend),
                               wizard=setup_wizard.SetupWizard(self.backend, self.backend),
                               load_settings=lambda: store.load(environ=self.env))
        self.assertEqual(self.get(app, '/'), (303, '/settings'))
        status, body = self.get(app, '/settings')
        self.assertIn('planning, approving and moving are off', body)
        self.assertIn('1 problem', body)
        self.assertEqual(self.post(app, '/plan')[0], 409)
        status, body = self.post(app, '/settings/streaming/review', **{'f:streaming.region': 'US'})
        self.assertEqual(status, 200, body)
        nonce = body.split('name="change" value="')[1].split('"')[0]
        self.assertEqual(self.post(app, '/settings/streaming/save', change=nonce)[0], 303)
        self.assertEqual(store.load(environ=self.env).errors, [])


if __name__ == '__main__':
    unittest.main()
