"""Settings pages (phase 4) and setup mode (phase 5), against the in-memory settings_contract fake."""
import http.client
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

import migratarr_ui
import settings_contract as sc
import settings_ui
from fake_settings import FakeSettings
from setup_wizard import SetupWizard
from test_ui import STORAGE, fake_usage

TOKEN = 'test-token'
SECRET = 'RADARR-SECRET-1234'
PASTED = 'PASTED-NEW-KEY-9876'


class Base(unittest.TestCase):
    configured = True
    actions = True

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.fake = FakeSettings(configured=self.configured)
        self.started = []
        self.created = []
        self.app = migratarr_ui.App(
            Path(tmp.name), STORAGE, {}, fake_usage, actions=self.actions, token=TOKEN,
            start_job=lambda base, kind, *a: self.started.append(kind) or '20261026T120000Z-' + kind,
            settings=settings_ui.SettingsPages(self.fake, self.fake),
            wizard=SetupWizard(self.fake, self.fake, on_created=self.created.append))

    def get(self, path, query=None):
        result = self.app.respond(path, query)
        if result[0] == 'redirect':
            return 303, result[1]
        return result[0], result[2].decode()

    def post(self, path, **fields):
        fields.setdefault('token', TOKEN)
        data = {k: v if isinstance(v, list) else [v] for k, v in fields.items()}
        result = self.app.respond_post(path, data)
        if result[0] == 'redirect':
            return 303, result[1]
        return result[0], result[2].decode()

    def f(self, **values):
        return {'f:' + k.replace('__', '.'): v for k, v in values.items()}


class ParseFormTests(unittest.TestCase):
    FIELDS = [sc.Field('a', 'A', sc.INTEGER), sc.Field('b', 'B', sc.NUMBER), sc.Field('c', 'C', sc.SECRET),
              sc.Field('d', 'D', sc.MULTI, choices=(('x', 'X'), ('y', 'Y'))), sc.Field('e', 'E', sc.LINES),
              sc.Field('g', 'G', sc.BOOL), sc.Field('h', 'H', sc.TEXT, required=True),
              sc.Field('r', 'R', sc.TEXT, read_only=True)]

    def test_types(self):
        values, problems = settings_ui.parse_form(self.FIELDS, {
            'f:a': ['12'], 'f:b': ['0.5'], 'f:c': [''], 'f:d': ['x', 'y'], 'f:e': ['one\n\n two \n'],
            'f:g': ['on'], 'f:h': ['  hi '], 'f:r': ['ignored']}, {'c': sc.SECRET_KEPT, 'r': 'kept'})
        self.assertEqual(problems, [])
        self.assertEqual(values, {'a': 12, 'b': 0.5, 'c': sc.SECRET_KEPT, 'd': ['x', 'y'], 'e': ['one', 'two'],
                                  'g': True, 'h': 'hi', 'r': 'kept'})

    def test_problems(self):
        values, problems = settings_ui.parse_form(self.FIELDS, {'f:a': ['1.5'], 'f:b': ['lots'], 'f:d': ['z']}, {})
        self.assertEqual({p.path for p in problems}, {'a', 'b', 'd', 'h'})
        self.assertIsNone(values['c'])  # blank secret with nothing stored
        self.assertFalse(values['g'])

    def test_whole_number_stays_int_for_number_fields(self):
        values, _ = settings_ui.parse_form([sc.Field('b', 'B', sc.NUMBER)], {'f:b': ['50']}, {})
        self.assertIs(type(values['b']), int)


class SettingsPageTests(Base):
    def test_index_lists_sections_problems_and_checks(self):
        self.fake.problems = [sc.Problem('services.radarr.url', 'Unreachable.')]
        status, body = self.get('/settings')
        self.assertEqual(status, 200)
        for text in ('Services', 'Disks and categories', '1 problem', 'Run health checks'):
            self.assertIn(text, body)
        self.assertNotIn('Radarr reachable', body)  # probes contact services: only with ?check=1
        status, body = self.get('/settings', {'check': ['1']})
        for text in ('Radarr reachable', 'Python 3.8 found', 'Effective settings'):
            self.assertIn(text, body)

    def test_edit_never_shows_secret(self):
        status, body = self.get('/settings/services')
        self.assertEqual(status, 200)
        self.assertNotIn(SECRET, body)
        self.assertIn('A key is saved', body)
        self.assertIn('http://localhost:7878', body)

    def test_unknown_section(self):
        self.assertEqual(self.get('/settings/nope')[0], 404)

    def test_review_shows_problems_and_keeps_typed_values(self):
        status, body = self.post('/settings/services/review', **self.f(
            services__radarr__url='ftp://radarr', services__radarr__source='file',
            services__sonarr__url='http://localhost:8989', services__sonarr__source='docker_config_xml'))
        self.assertEqual(status, 400)
        self.assertIn('Use an http:// or https:// address.', body)
        self.assertIn('ftp://radarr', body)
        self.assertEqual(self.fake.saves, [])

    def test_review_then_save_with_pasted_key(self):
        status, body = self.post('/settings/services/review', **self.f(
            services__radarr__url='http://radarr:7878', services__radarr__source='file',
            services__radarr__key=PASTED,
            services__sonarr__url='http://localhost:8989', services__sonarr__source='docker_config_xml'))
        self.assertEqual(status, 200, body)
        self.assertNotIn(PASTED, body)  # not echoed, not even in a hidden field
        self.assertNotIn(SECRET, body)
        self.assertIn('http://radarr:7878', body)
        self.assertIn('new key', body)
        self.assertEqual(self.fake.saves, [])
        nonce = body.split('name="change" value="')[1].split('"')[0]
        status, where = self.post('/settings/services/save', change=nonce)
        self.assertEqual((status, where.split('?')[0]), (303, '/settings'))
        section, saved = self.fake.saves[0]
        self.assertEqual(saved['services.radarr.key'], PASTED)
        self.assertEqual(saved['services.radarr.url'], 'http://radarr:7878')
        self.assertIn('Saved.', self.get('/settings', {'done': ['saved']})[1])
        # one-time: the same review cannot be saved twice
        status, body = self.post('/settings/services/save', change=nonce)
        self.assertEqual(status, 409)
        self.assertEqual(len(self.fake.saves), 1)

    def review_streaming(self, region='GB'):
        status, body = self.post('/settings/streaming/review',
                                 **{'f:streaming.region': region, 'f:streaming.subscribed': ['Hulu', 'Netflix']})
        self.assertEqual(status, 200, body)
        return body.split('name="change" value="')[1].split('"')[0]

    def test_save_refused_when_files_changed_since_review(self):
        nonce = self.review_streaming()
        self.fake.store[sc.OVERRIDES]['overrides.lock_tag'] = 'edited-elsewhere'
        status, body = self.post('/settings/streaming/save', change=nonce)
        self.assertEqual(status, 409)
        self.assertIn('changed since you reviewed', body)
        self.assertEqual(self.fake.saves, [])

    def test_save_refused_while_job_runs(self):
        nonce = self.review_streaming()
        self.fake.job_running = True
        status, body = self.post('/settings/streaming/save', change=nonce)
        self.assertEqual(status, 409)
        self.assertIn('A job is running', body)

    def test_nonce_is_bound_to_its_section(self):
        nonce = self.review_streaming()
        self.assertEqual(self.post('/settings/services/save', change=nonce)[0], 409)

    def test_review_expires(self):
        now = [1000.0]
        self.app.settings.clock = lambda: now[0]
        nonce = self.review_streaming()
        now[0] += settings_ui.PENDING_SECONDS + 1
        self.assertEqual(self.post('/settings/streaming/save', change=nonce)[0], 409)

    def test_no_change_saves_nothing(self):
        status, where = self.post('/settings/streaming/review',
                                  **{'f:streaming.region': 'US', 'f:streaming.subscribed': ['Hulu']})
        self.assertEqual((status, where), (303, '/settings?done=unchanged'))
        self.assertIn('Nothing changed', self.get('/settings', {'done': ['unchanged']})[1])

    def test_warnings_do_not_block(self):
        status, body = self.post('/settings/scoring/review', **{
            'f:scoring.weights.replacement': '0.5', 'f:scoring.weights.streaming': '0.3',
            'f:scoring.weights.usage': '0.3'})
        self.assertEqual(status, 200)
        self.assertIn('not 1', body)

    def test_bad_token(self):
        self.assertEqual(self.post('/settings/streaming/review', token='nope')[0], 403)


class ReadOnlySettingsTests(Base):
    actions = False

    def test_read_only_shows_values_without_forms(self):
        status, body = self.get('/settings/streaming')
        self.assertEqual(status, 200)
        self.assertNotIn('<form', body)
        self.assertIn('disabled', body)
        self.assertEqual(self.post('/settings/streaming/review')[0], 405)


class SetupModeTests(Base):
    configured = False

    def test_everything_redirects_to_setup(self):
        for path in ('/', '/settings', '/run/20261023T120000Z'):
            self.assertEqual(self.get(path), (303, '/setup'), path)
        self.assertEqual(self.get('/setup'), (303, '/setup/services'))
        self.assertEqual(self.get('/static/app.css')[0], 200)

    def test_actions_refused_until_set_up(self):
        status, _ = self.post('/plan')
        self.assertEqual(status, 409)
        self.assertEqual(self.started, [])
        self.assertEqual(self.post('/settings/streaming/review')[0], 409)

    def test_cannot_skip_ahead(self):
        self.assertEqual(self.get('/setup/disks'), (303, '/setup/services'))
        self.assertEqual(self.get('/setup/review')[0], 200)  # review shows, but saving checks every step
        status, body = self.post('/setup/review', action='save')
        self.assertEqual(status, 400)
        self.assertIn('Finish these steps first', body)
        self.assertEqual(self.fake.created, [])

    def test_detect_fills_blanks_only(self):
        status, body = self.post('/setup/services', action='detect', **self.f(
            services__radarr__url='http://mine:7878', services__radarr__source='docker_config_xml',
            services__sonarr__url='', services__sonarr__source='docker_config_xml'))
        self.assertEqual(status, 200)
        self.assertIn('http://mine:7878', body)
        self.assertIn('http://localhost:8989', body)
        self.assertIn('Found Radarr and Sonarr', body)

    def test_local_topology_refused_until_supported(self):
        self.walk_services()
        status, body = self.post('/setup/topology', action='next', **{'f:topology.mode': 'local'})
        self.assertEqual(status, 400)
        self.assertIn('cannot move files that way yet', body)
        self.fake.capabilities_.topologies = ('nas_ssh', 'local')
        self.assertEqual(self.post('/setup/topology', action='next', **{'f:topology.mode': 'local'}),
                         (303, '/setup/disks'))

    def walk_services(self):
        result = self.post('/setup/services', action='next', **self.f(
            services__radarr__url='http://localhost:7878', services__radarr__source='file',
            services__radarr__key=PASTED,
            services__sonarr__url='http://localhost:8989', services__sonarr__source='docker_config_xml'))
        self.assertEqual(result, (303, '/setup/topology'))

    def test_full_setup(self):
        self.walk_services()
        self.assertEqual(self.post('/setup/topology', action='next',
                                   **{'f:topology.mode': 'nas_ssh', 'f:topology.nas.host': 'nas.example'}),
                         (303, '/setup/disks'))
        self.assertEqual(self.post('/setup/disks', action='next',
                                   **{'f:disks.media01.path': '/mnt/a', 'f:disks.media01.min_free_gb': '50'}),
                         (303, '/setup/streaming'))
        status, body = self.post('/setup/streaming', action='next', **{'f:streaming.region': 'us'})
        self.assertEqual(status, 400)  # validation runs per step
        self.assertEqual(self.post('/setup/streaming', action='next', **{'f:streaming.region': 'US'}),
                         (303, '/setup/review'))
        status, body = self.get('/setup/review')
        self.assertEqual(status, 200)
        self.assertNotIn(PASTED, body)
        self.assertIn('new key', body)
        self.assertIn('owner only', body.replace('your user only', 'owner only'))
        self.assertIsNotNone(self.fake.draft_checks)  # checks ran against the draft
        self.assertEqual(self.fake.created, [])  # nothing written before Save
        self.assertEqual(self.post('/setup/review', action='save'), (303, '/setup/done'))
        draft = self.fake.created[0]
        self.assertEqual(draft['services']['services.radarr.key'], PASTED)
        self.assertEqual(draft['disks']['disks.media01.min_free_gb'], 50)
        self.assertEqual(len(self.created), 1)
        self.assertNotIn(PASTED, str(vars(self.app.wizard)))  # draft dropped from memory
        status, body = self.get('/setup/done')
        self.assertIn('Make a first plan', body)
        self.assertEqual(self.get('/')[0], 200)  # setup mode is over
        self.assertEqual(self.get('/setup/services'), (303, '/settings'))
        self.assertEqual(self.post('/setup/services', action='next')[0], 409)

    def test_done_withholds_plan_when_checks_fail(self):
        self.fake.configured = True
        self.fake.checks = [sc.CheckResult('nas', 'NAS over SSH', sc.FAIL, 'Permission denied (publickey)')]
        status, body = self.get('/setup/done')
        self.assertNotIn('Make a first plan', body)
        self.assertIn('Permission denied', body)

    def test_back_keeps_values(self):
        self.walk_services()
        self.assertEqual(self.post('/setup/topology', action='back', **{'f:topology.nas.host': 'typed'}),
                         (303, '/setup/services'))
        self.assertIn('value="typed"', self.get('/setup/topology')[1])


class SetupReadOnlyTests(Base):
    configured = False
    actions = False

    def test_explains_how_to_set_up(self):
        status, body = self.get('/setup')
        self.assertEqual(status, 200)
        self.assertIn('--read-only', body)


class RedirectOverHttpTests(Base):
    configured = False

    def test_get_redirect_is_a_303(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), migratarr_ui.make_handler(self.app))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
        conn.request('GET', '/', headers={'Host': 'localhost'})
        response = conn.getresponse()
        self.assertEqual((response.status, response.getheader('Location')), (303, '/setup'))


if __name__ == '__main__':
    unittest.main()
