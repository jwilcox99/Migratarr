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


def no_docker(*args, **kwargs):
    raise FileNotFoundError('docker')


class Env:
    def make(self, configured=True, topology='nas_ssh'):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.env = {'MIGRATARR_CONFIG': str(self.root / 'config/runtime.json'),
                    'MIGRATARR_PLANNER_CONFIG': str(self.root / 'config/planner.json'),
                    'MIGRATARR_SECRETS_DIR': str(self.root / 'secrets'), 'HOME': str(self.root)}
        self.base = self.root / 'base'
        self.backend = UiBackend(self.env, base=self.base, http=offline_http, run=no_ssh, docker=no_docker)
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
                                                     http=offline_http, run=no_ssh, docker=no_docker)}
        self.assertEqual(set(ours), set(theirs))  # one check list for UI, doctor and plan
        for cid, c in theirs.items():
            self.assertEqual(ours[cid].status, {'skip': 'skipped'}.get(c.status, c.status))
            self.assertEqual(ours[cid].fix, c.fix)


class WizardFeedbackTests(Env, unittest.TestCase):
    """From the first real wizard run (2026-10-27)."""

    def test_blank_nas_fields_are_named_on_their_fields_in_plain_words(self):
        b = self.make(configured=False)
        draft = self.draft()
        draft[sc.TOPOLOGY]['topology.nas.ssh_key'] = None
        problems = b.validate(sc.TOPOLOGY, draft[sc.TOPOLOGY], draft)
        on_key = [p for p in problems if p.path == 'topology.nas.ssh_key']
        self.assertEqual(len(on_key), 1)  # ours, not also the validator's
        self.assertIn('full path to the SSH private key', on_key[0].message)
        self.assertFalse([p for p in problems if p.path == sc.TOPOLOGY])  # nothing left in the top box

    def test_local_topology_needs_no_nas_fields(self):
        b = self.make(configured=False)
        draft = self.draft('local')
        self.assertEqual(sc.blocking(b.validate(sc.TOPOLOGY, draft[sc.TOPOLOGY], draft)), [])
        self.assertFalse(any(f.required for f in b.fields(sc.TOPOLOGY, {}) if f.path.startswith('topology.nas.')))

    def test_an_ip_address_as_the_nas_user_is_caught(self):
        b = self.make(configured=False)
        draft = self.draft()
        draft[sc.TOPOLOGY].update({'topology.nas.host': 'migratarr', 'topology.nas.user': '10.0.0.5'})
        problems = b.validate(sc.TOPOLOGY, draft[sc.TOPOLOGY], draft)
        self.assertIn('looks like an IP address', ' '.join(p.message for p in problems
                                                            if p.path == 'topology.nas.user'))

    def test_validator_messages_naming_a_nas_field_land_on_it(self):
        self.assertEqual(problem_path(store.Problem('runtime', 'nas', 'nas.ssh_key must be absolute'))[0],
                         'topology.nas.ssh_key')

    def test_detect_only_on_steps_that_can_detect(self):
        b = self.make(configured=False)
        wizard = setup_wizard.SetupWizard(b, b)
        ctx = {'actions': True, 'token': TOKEN}
        for step, shown in ((sc.SERVICES, True), (sc.TOPOLOGY, False), (sc.DISKS, True), (sc.STREAMING, False)):
            wizard.done = set(setup_wizard.STEPS[:setup_wizard.STEPS.index(step)])
            body = wizard.get(step, ctx)[1]
            self.assertEqual('value="detect"' in body, shown, step)


class PortInUseTests(unittest.TestCase):
    def test_busy_port_is_a_message_not_a_traceback(self):
        import contextlib
        import io
        import socket
        from unittest.mock import patch
        busy = socket.socket()
        busy.bind(('127.0.0.1', 0))
        busy.listen()
        self.addCleanup(busy.close)
        port = busy.getsockname()[1]
        err = io.StringIO()
        with patch.object(migratarr_ui, 'build_app', return_value=None), \
                patch('migratarr.settings.migrate_if_needed'), contextlib.redirect_stderr(err):
            code = migratarr_ui.main(['--port', str(port)])
        self.assertEqual(code, 2)
        self.assertIn(f'--port {port + 1}', err.getvalue())


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


class ArrInstallTests(Env, unittest.TestCase):
    """Radarr and Sonarr in Docker (the default) or installed directly on this machine."""

    def host_draft(self):
        d = self.draft()
        xml = self.root / 'radarr.xml'
        xml.write_text('<Config><ApiKey>HOST-RADARR-KEY</ApiKey><UrlBase>/radarr</UrlBase></Config>')
        d[sc.SERVICES].update({'services.arr_install': 'host', 'services.radarr.source': 'config_xml',
                               'services.radarr.paths': [str(xml)], 'services.radarr.key': None})
        return d

    def test_docker_is_the_default_and_writes_nothing_new(self):
        b = self.make(configured=False)
        self.assertEqual(b.defaults(sc.SERVICES)['services.arr_install'], 'docker')
        paths = [f.path for f in b.fields(sc.SERVICES, b.defaults(sc.SERVICES))]
        self.assertEqual(paths[0], 'services.arr_install')
        self.assertIn('services.radarr.container', paths)
        b.create(self.draft())
        runtime = store.load(environ=self.env).data['runtime']
        self.assertNotIn('arr_file_checks', runtime)
        self.assertEqual(runtime['containers'], {'radarr': 'radarr', 'sonarr': 'sonarr', 'homepage': 'homepage'})

    def test_host_hides_containers_and_container_sources(self):
        b = self.make(configured=False)
        fields = {f.path: f for f in b.fields(sc.SERVICES, {'services.arr_install': 'host'})}
        for arr in ('radarr', 'sonarr'):
            self.assertNotIn(f'services.{arr}.container', fields)
            self.assertFalse([v for v, _ in fields[f'services.{arr}.source'].choices if v.startswith('docker')])
        self.assertIn('services.jellyfin.container', fields)  # Jellyfin's key may still come from a container
        self.assertEqual([v for v, _ in fields['services.arr_install'].choices], ['docker', 'host'])

    def test_host_writes_host_file_checks_and_loads(self):
        b = self.make(configured=False)
        draft = self.host_draft()
        self.assertEqual(sc.blocking(b.validate(sc.SERVICES, draft[sc.SERVICES], draft)), [])
        b.create(draft)
        report = store.load(environ=self.env)
        self.assertEqual(report.errors, [])
        runtime = report.data['runtime']
        self.assertEqual(runtime['arr_file_checks'], {'radarr': {'mode': 'host'}, 'sonarr': {'mode': 'host'}})
        self.assertEqual(report.runtime.arr_file_checks['radarr'], {'mode': 'host', 'host_root': None})
        self.assertEqual(runtime['containers'], {'homepage': 'homepage'})  # no `docker exec` into Arr
        self.assertEqual(runtime['secrets']['radarr'], {'source': 'config_xml', 'path': str(self.root / 'radarr.xml')})
        self.assertEqual(b.current(sc.SERVICES)['services.arr_install'], 'host')

    def test_host_refuses_a_container_key_source(self):
        b = self.make(configured=False)
        draft = self.host_draft()
        draft[sc.SERVICES]['services.radarr.source'] = 'docker_config_xml'
        problems = sc.blocking(b.validate(sc.SERVICES, draft[sc.SERVICES], draft))
        self.assertEqual([p.path for p in problems], ['services.radarr.source'])
        self.assertIn('installed directly on this machine', problems[0].message)

    def test_switching_to_host_moves_container_keys_to_config_xml(self):
        b = self.make(configured=False)
        values = dict(b.defaults(sc.SERVICES), **{'services.arr_install': 'host'})
        values, notes = b.normalize(sc.SERVICES, values)
        self.assertEqual(values['services.radarr.source'], 'config_xml')
        self.assertIsNone(values['services.radarr.container'])
        self.assertEqual({n.path for n in notes}, {'services.radarr.paths', 'services.sonarr.paths'})
        self.assertEqual({n.severity for n in notes}, {sc.WARN})
        # A config.xml path is then required.
        self.assertIn('services.radarr.paths', {p.path for p in sc.blocking(b.validate(sc.SERVICES, values))})

    def test_settings_switch_both_ways_with_a_reviewed_diff(self):
        b = self.make()
        xml = self.root / 'sonarr.xml'
        xml.write_text('<Config><ApiKey>K</ApiKey></Config>')
        values = dict(b.current(sc.SERVICES), **{'services.arr_install': 'host', 'services.sonarr.source':
                                                 'config_xml', 'services.sonarr.paths': [str(xml)]})
        shape = {k: v for k, v in values.items() if k not in ('services.radarr.container', 'services.sonarr.container')}
        values, notes = b.normalize(sc.SERVICES, shape)
        self.assertEqual(notes, [])  # radarr reads a pasted key, sonarr a config.xml: nothing to fix
        self.assertEqual(sc.blocking(b.validate(sc.SERVICES, values)), [])
        changes = {c.path: (c.before, c.after) for c in b.diff(sc.SERVICES, values)}
        self.assertEqual(changes['services.arr_install'], ('docker', 'host'))
        self.assertEqual(changes['services.radarr.container'], ('radarr', None))
        b.save(sc.SERVICES, values, b.status().revision)
        runtime = store.load(environ=self.env).data['runtime']
        self.assertEqual(runtime['arr_file_checks']['sonarr'], {'mode': 'host'})
        # And back: the hidden container fields come back with their default names.
        values = {k: v for k, v in b.current(sc.SERVICES).items() if not k.endswith('.container')
                  or k.startswith(('services.jellyfin', 'services.tmdb'))}
        values['services.arr_install'] = 'docker'
        values, notes = b.normalize(sc.SERVICES, values)
        self.assertEqual(values['services.radarr.container'], 'radarr')
        b.save(sc.SERVICES, values, b.status().revision)
        runtime = store.load(environ=self.env).data['runtime']
        self.assertNotIn('arr_file_checks', runtime)
        self.assertEqual(runtime['containers']['radarr'], 'radarr')
        self.assertEqual(runtime['secrets']['sonarr']['source'], 'config_xml')  # a host config.xml is kept

    def test_hand_written_file_checks_are_kept(self):
        b = self.make()
        path = Path(self.env['MIGRATARR_CONFIG'])
        runtime = json.loads(path.read_text())
        runtime['arr_file_checks'] = {'radarr': {'mode': 'host', 'host_root': '/srv/pool/media'}}
        path.write_text(json.dumps(runtime))
        values = b.current(sc.SERVICES)
        self.assertEqual(values['services.arr_install'], 'custom')
        field = b.fields(sc.SERVICES, values)[0]
        self.assertIn('custom', [v for v, _ in field.choices])
        self.assertIn('services.radarr.container', {f.path for f in b.fields(sc.SERVICES, values)})
        values['services.sonarr.url'] = 'http://sonarr.lan:8989'
        b.save(sc.SERVICES, values, b.status().revision)
        self.assertEqual(json.loads(path.read_text())['arr_file_checks'],
                         {'radarr': {'mode': 'host', 'host_root': '/srv/pool/media'}})

    def test_detect_reads_config_xml_on_this_machine_and_never_docker(self):
        b = self.make(configured=False)
        b.docker_cat = lambda *a: self.fail('Detect must not use docker in host mode')
        config = self.root / '.config/Sonarr/config.xml'  # where Sonarr keeps it for a user install
        config.parent.mkdir(parents=True)
        config.write_text('<Config><ApiKey>HOST-SECRET</ApiKey><UrlBase>/sonarr</UrlBase></Config>')
        found = b.discover(sc.SERVICES, {sc.SERVICES: {'services.arr_install': 'host'}})
        self.assertNotIn('HOST-SECRET', repr(found))
        self.assertEqual(found.values['services.sonarr.source'], 'config_xml')
        self.assertEqual(found.values['services.sonarr.paths'], [str(config)])
        self.assertNotIn('services.radarr.source', found.values)
        notes = ' '.join(found.notes)
        self.assertIn('URL base /sonarr', notes)
        self.assertIn('Radarr: no readable config.xml', notes)

    def test_detect_in_docker_mode_says_when_docker_itself_is_missing(self):
        b = self.make(configured=False)
        b.docker_cat = no_docker
        notes = ' '.join(b.discover(sc.SERVICES, {sc.SERVICES: {}}).notes)
        self.assertIn('Radarr: Docker is not installed on this machine', notes)
        self.assertIn('installed directly on this machine, choose that above', notes)

    def test_draft_checks_respect_the_mode(self):
        b = self.make(configured=False)
        calls = []
        b.docker = lambda command, **kw: calls.append(command) or no_docker()
        checks = {c.id: c for c in b.run_checks(self.host_draft())}
        self.assertEqual(calls, [])
        self.assertIn('on this machine', checks['arr_files.radarr'].detail)
        self.assertEqual(checks['arr_files.radarr'].paths, ('services.arr_install',))
        checks = {c.id: c for c in b.run_checks(self.draft())}
        self.assertEqual(calls[0][:3], ['docker', 'exec', 'radarr'])
        self.assertIn('Docker is not installed', checks['arr_files.radarr'].detail)


class ContainerTests(Env, unittest.TestCase):
    """Migratarr in its Docker image (MIGRATARR_IN_CONTAINER=1): host-mode checks, pasted keys, container URLs."""

    def make(self, configured=True, topology='nas_ssh'):
        backend = super().make(configured=False, topology=topology)
        self.env['MIGRATARR_IN_CONTAINER'] = '1'
        self.backend = UiBackend(self.env, base=self.base, http=offline_http, run=no_ssh, docker=no_docker)
        if configured:
            self.backend.create(self.draft(topology))
        return self.backend

    def test_fresh_install_defaults(self):
        b = self.make(configured=False)
        values = b.defaults(sc.SERVICES)
        self.assertEqual(values['services.arr_install'], 'host')
        self.assertEqual((values['services.radarr.url'], values['services.sonarr.url'], values['services.jellyfin.url']),
                         ('http://radarr:7878', 'http://sonarr:8989', 'http://jellyfin:8096'))
        for service in ('radarr', 'sonarr', 'jellyfin'):
            self.assertEqual(values[f'services.{service}.source'], 'paste', service)

    def test_only_choices_that_work_in_a_container(self):
        b = self.make(configured=False)
        fields = {f.path: f for f in b.fields(sc.SERVICES, b.defaults(sc.SERVICES))}
        self.assertEqual([v for v, _ in fields['services.arr_install'].choices], ['host'])
        self.assertIn('same path', fields['services.arr_install'].help)
        for service in ('radarr', 'sonarr', 'jellyfin', 'tmdb'):
            self.assertFalse([v for v, _ in fields[f'services.{service}.source'].choices if v.startswith('docker')],
                             service)
            self.assertNotIn(f'services.{service}.container', fields)

    def test_a_docker_choice_from_an_old_form_becomes_host_and_paste(self):
        b = self.make(configured=False)
        values = dict(b.defaults(sc.SERVICES), **{'services.arr_install': 'docker',
                                                  'services.sonarr.source': 'docker_config_xml',
                                                  'services.jellyfin.source': 'docker_file'})
        values, _ = b.normalize(sc.SERVICES, values)
        self.assertEqual(values['services.arr_install'], 'host')
        self.assertEqual((values['services.sonarr.source'], values['services.jellyfin.source']), ('paste', 'paste'))

    def test_created_settings_check_through_the_own_mount(self):
        b = self.make()
        runtime = store.load(environ=self.env).data['runtime']
        self.assertEqual(runtime['arr_file_checks'], {'radarr': {'mode': 'host'}, 'sonarr': {'mode': 'host'}})
        self.assertNotIn('radarr', runtime.get('containers', {}))
        self.assertEqual(runtime['urls']['radarr'], 'http://radarr:7878')

    def test_detect_says_to_paste(self):
        b = self.make(configured=False)
        found = b.discover(sc.SERVICES, {sc.SERVICES: b.defaults(sc.SERVICES)})
        notes = ' '.join(found.notes)
        self.assertIn("Radarr: paste its API key (Settings > General > API Key)", notes)
        self.assertNotIn('docker', notes.lower().replace("can't read other containers", ''))

    def test_wizard_text_and_examples_fit_the_container(self):
        b = self.make(configured=False)
        self.assertIn('paste its key', b.intro(sc.SERVICES))
        self.assertIsNone(b.intro(sc.DISKS))
        fields = {f.path: f for f in b.fields(sc.SERVICES, b.defaults(sc.SERVICES))}
        self.assertEqual(fields['services.radarr.url'].help, 'For example http://radarr:7878')

    def test_outside_a_container_nothing_changes(self):
        b = super().make(configured=False)
        self.assertEqual(b.defaults(sc.SERVICES)['services.arr_install'], 'docker')
        self.assertEqual(b.defaults(sc.SERVICES)['services.radarr.url'], 'http://localhost:7878')


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

    def test_wizard_round_trip_in_host_mode(self):
        app = self.app(configured=False)
        xml = self.root / 'radarr.xml'
        xml.write_text('<Config><ApiKey>HOST-RADARR-KEY</ApiKey></Config>')
        draft = self.draft()
        f = lambda values: {'f:' + k: (v if isinstance(v, list) else ['on'] if v is True else [] if v is False
                                       else ['' if v is None else str(v)]) for k, v in values.items()
                            if not k.startswith('disks._') or k.startswith(('disks._folders', 'disks._placement',
                                                                            'disks._defaults', 'disks._arr_root'))}
        # The Docker form as first shown, with "installed directly" picked and Next pressed.
        services = dict(draft[sc.SERVICES], **{'services.arr_install': 'host',
                                               'services.sonarr.source': 'docker_config_xml'})
        status, body = self.post(app, '/setup/services', action='next', **f(services))
        self.assertEqual(status, 200)  # shown again, for Sonarr's config.xml path
        self.assertIn('Sonarr runs on this machine', body)
        self.assertNotIn('f-services-radarr-container', body)
        self.assertNotIn('value="docker_config_xml"', body.split('f:services.sonarr.source')[1].split('</select>')[0])
        services.update({'services.sonarr.source': 'config_xml', 'services.sonarr.paths': [str(xml)]})
        del services['services.radarr.container'], services['services.sonarr.container']
        self.assertEqual(self.post(app, '/setup/services', action='next', **f(services)), (303, '/setup/topology'))
        status, body = self.get(app, '/setup/services')  # Back: the choice is kept
        self.assertIn('<option value="host" selected>', body)
        for step in (sc.TOPOLOGY, sc.DISKS, sc.STREAMING):
            self.assertEqual(self.post(app, '/setup/' + step, action='next', **f(draft[step]))[0], 303)
        status, body = self.get(app, '/setup/review')
        self.assertIn('installed directly on this machine', body)
        self.assertIn('Radarr file checks', body)
        self.assertEqual(self.post(app, '/setup/review', action='save'), (303, '/setup/done'))
        report = store.load(environ=self.env)
        self.assertEqual(report.errors, [])
        self.assertEqual(report.data['runtime']['arr_file_checks'],
                         {'radarr': {'mode': 'host'}, 'sonarr': {'mode': 'host'}})
        self.assertNotIn('radarr', report.data['runtime']['containers'])

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
