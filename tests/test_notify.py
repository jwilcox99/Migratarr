"""Schedule & notifications (0.3.0): notify.py, schedule.py, notify_ui.py and their place in the UI and jobs."""
from datetime import datetime, timedelta, timezone
import http.server
import json
import os
from pathlib import Path
import shutil
import socketserver
import stat
import tempfile
import threading
import unittest

from migratarr import notify, notify_ui, schedule
import migratarr_ui
from test_read_api import RUN, Fixture, eid
from test_ui import STORAGE, fake_usage

DISCORD = 'https://discord.com/api/webhooks/123/SECRET-TOKEN'
UTC = timezone.utc


def enabled(**changes):
    data = notify.defaults()
    data['discord'].update(enabled=True, url=DISCORD)
    for key, value in changes.items():
        data[key] = value if not isinstance(value, dict) else dict(data[key], **value)
    return data


class Temp(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.settings_file = self.root / 'config' / 'notifications.json'
        self.state_file = self.root / 'config' / 'schedule-state.json'


# ---------------------------------------------------------------- settings file and checks

class SettingsTests(Temp):
    def test_defaults_when_missing_and_owner_only_file(self):
        self.assertEqual(notify.load(self.settings_file), notify.defaults())
        notify.save(enabled(), self.settings_file)
        self.assertEqual(stat.S_IMODE(self.settings_file.stat().st_mode), 0o600)
        self.assertEqual(notify.load(self.settings_file)['discord']['url'], DISCORD)
        self.assertFalse(list(self.settings_file.parent.glob('.*.tmp')))

    def test_damaged_file(self):
        self.settings_file.parent.mkdir(parents=True)
        self.settings_file.write_text('[1]')
        with self.assertRaises(notify.NotifySettingsError):
            notify.load(self.settings_file)

    def test_problems(self):
        self.assertEqual(notify.problems(notify.defaults()), {})
        data = notify.defaults()
        data['ui_address'] = 'http://user:pw@host:8765'
        data['schedule'].update(enabled=True, days=[], time='25:00')
        data['discord'].update(enabled=True, url='https://evil.example/api/webhooks/1/x')
        data['webhook'].update(enabled=True, url='ftp://x')
        data['email'].update(enabled=True, host='', port=0, security='x', **{'from': 'nope'}, to=['a@b.co', 'bad'])
        self.assertEqual(set(notify.problems(data)), {'ui_address', 'schedule.days', 'schedule.time', 'discord.url',
                                                      'webhook.url', 'email.host', 'email.port', 'email.security',
                                                      'email.from', 'email.to'})
        self.assertIn('bad', notify.problems(data)['email.to'])
        for good in ('http://192.168.1.20:8765', 'https://migratarr.lan', ''):
            self.assertEqual(notify.check_ui_address(good), '', good)
        for host in ('discord.com', 'discordapp.com', 'ptb.discord.com'):
            self.assertEqual(notify.check_url(f'https://{host}/api/webhooks/1/x', discord=True), '', host)
        self.assertTrue(notify.check_url('http://discord.com/api/webhooks/1/x', discord=True))


# ---------------------------------------------------------------- messages from jobs

class JobMessageTests(Temp):
    def setUp(self):
        super().setUp()
        Fixture(self.root)

    def test_plan_with_moves_is_batch_ready_with_a_link(self):
        m = notify.job_message(self.root, dict(kind='plan', state='FINISHED', job_id='J', run_id=RUN))
        self.assertEqual(m.event, notify.BATCH_READY)
        self.assertIn('11 moves', m.title)
        self.assertEqual(m.link('http://host:8765/'), f'http://host:8765/run/{RUN}')
        self.assertEqual(m.link(''), '')
        self.assertEqual(m.details['moves'], 11)

    def test_plan_with_nothing_to_move_sends_nothing(self):
        csv = self.root / 'manifests' / RUN / 'execution_manifest.csv'
        header = csv.read_text().splitlines()[0]
        empty = self.root / 'empty'
        shutil.copytree(self.root, empty)
        (empty / 'manifests' / RUN / 'execution_manifest.csv').write_text(header + '\n')
        from unittest.mock import patch
        with patch('migratarr.read_api.get_manifest', return_value=dict(rows=[])):
            self.assertIsNone(notify.job_message(empty, dict(kind='plan', state='FINISHED', run_id=RUN)))

    def test_failed_finished_and_stopped(self):
        m = notify.job_message(self.root, dict(kind='run', state='FAILED', job_id='J-run', message='disk full'))
        self.assertEqual((m.event, m.path), (notify.FAILED, '/job/J-run'))
        self.assertIn('disk full', m.text)
        m = notify.job_message(self.root, dict(kind='run', state='FINISHED', run_id=RUN, job_id='J',
                                               done=[dict(size_gb='1.5'), dict(size_gb='2')], skipped=[{}]))
        self.assertEqual(m.event, notify.RUN_FINISHED)
        self.assertEqual(m.title, 'Migratarr: 2 moves done (3.5 GB), 1 skipped or held')
        self.assertIsNone(notify.job_message(self.root, dict(kind='run', state='STOPPED')))


# ---------------------------------------------------------------- delivery

class Capture(http.server.BaseHTTPRequestHandler):
    received = []
    status = 204
    location = None

    def do_POST(self):
        body = self.rfile.read(int(self.headers['Content-Length']))
        Capture.received.append((self.path, self.headers.get('Content-Type'), json.loads(body)))
        self.send_response(Capture.status)
        if Capture.location:
            self.send_header('Location', Capture.location)
        self.end_headers()

    def log_message(self, *args):
        pass


class SmtpHandler(socketserver.StreamRequestHandler):
    """Just enough SMTP for one message: records the envelope and the data."""
    received = []

    def handle(self):
        say = lambda line: self.wfile.write((line + '\r\n').encode())
        say('220 test')
        mail = dict(rcpt=[])
        while True:
            line = self.rfile.readline().decode().rstrip('\r\n')
            if not line:
                return
            verb = line.split(' ', 1)[0].upper()
            if verb in ('EHLO', 'HELO'):
                say('250 test')
            elif verb == 'MAIL':
                mail['from'] = line
                say('250 ok')
            elif verb == 'RCPT':
                mail['rcpt'].append(line)
                say('250 ok')
            elif verb == 'DATA':
                say('354 go')
                data = []
                while True:
                    part = self.rfile.readline().decode()
                    if part in ('.\r\n', '.\n'):
                        break
                    data.append(part)
                mail['data'] = ''.join(data)
                SmtpHandler.received.append(mail)
                say('250 queued')
            elif verb == 'QUIT':
                say('221 bye')
                return
            else:
                say('502 no')


def serve(handler):
    server = socketserver.ThreadingTCPServer(('127.0.0.1', 0), handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        Capture.received, Capture.status, Capture.location = [], 204, None
        SmtpHandler.received = []
        self.http = serve(Capture)
        self.addCleanup(self.http.shutdown)
        self.addCleanup(self.http.server_close)
        self.url = f'http://127.0.0.1:{self.http.server_address[1]}'

    def message(self):
        return notify.Message(notify.BATCH_READY, 'Migratarr: 3 moves ready', '3 moves planned.', '/run/X',
                              dict(moves=3))

    def test_webhook_posts_json_over_http(self):
        data = notify.defaults()
        data.update(ui_address='http://media:8765')
        data['webhook'].update(enabled=True, url=self.url + '/hook')
        self.assertEqual(notify.send(data, self.message()), [('webhook', '')])
        path, kind, payload = Capture.received[0]
        self.assertEqual((path, kind), ('/hook', 'application/json'))
        self.assertEqual(payload['event'], 'batch_ready')
        self.assertEqual(payload['url'], 'http://media:8765/run/X')
        self.assertEqual(payload['details'], {'moves': 3})

    def test_redirects_are_not_followed_and_errors_hide_the_url(self):
        Capture.status, Capture.location = 302, self.url + '/elsewhere'
        data = notify.defaults()
        data['webhook'].update(enabled=True, url=self.url + '/hook?token=SECRET')
        [(channel, error)] = notify.send(data, self.message())
        self.assertEqual(channel, 'webhook')
        self.assertIn('302', error)
        self.assertNotIn('SECRET', error)
        self.assertEqual([r[0] for r in Capture.received], ['/hook?token=SECRET'])  # never /elsewhere

    def test_discord_payload(self):
        sent = []
        notify.send_discord(enabled(ui_address='http://media:8765'), self.message(),
                            post=lambda url, payload: sent.append((url, payload)))
        url, payload = sent[0]
        self.assertEqual(url, DISCORD)
        self.assertIn('**Migratarr: 3 moves ready**', payload['content'])
        self.assertIn('http://media:8765/run/X', payload['content'])
        self.assertEqual(payload['allowed_mentions'], {'parse': []})  # never pings @everyone

    def test_email_over_a_real_smtp_conversation(self):
        smtp = serve(SmtpHandler)
        self.addCleanup(smtp.shutdown)
        self.addCleanup(smtp.server_close)
        data = notify.defaults()
        data['email'].update(enabled=True, host='127.0.0.1', port=smtp.server_address[1], security='none',
                             **{'from': 'migratarr@home.lan'}, to=['josh@home.lan', 'friend@home.lan'])
        self.assertEqual(notify.send(data, self.message()), [('email', '')])
        mail = SmtpHandler.received[0]
        self.assertEqual(len(mail['rcpt']), 2)
        self.assertIn('Subject: Migratarr: 3 moves ready', mail['data'])
        self.assertIn('3 moves planned.', mail['data'])

    def test_email_starttls_and_login(self):
        calls = []

        class FakeSMTP:
            def __init__(self, host, port, **kwargs):
                calls.append(('connect', host, port, 'context' in kwargs))

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def starttls(self, context):
                calls.append(('starttls',))

            def login(self, user, password):
                calls.append(('login', user))

            def send_message(self, mail):
                calls.append(('send', mail['To']))
        data = notify.defaults()
        data['email'].update(enabled=True, host='smtp.example', port=587, security='starttls', username='me',
                             password='pw', **{'from': 'a@b.co'}, to=['c@d.co'])
        notify.send_email(data, self.message(), smtp=FakeSMTP)
        self.assertEqual(calls, [('connect', 'smtp.example', 587, False), ('starttls',), ('login', 'me'),
                                 ('send', 'c@d.co')])

    def test_events_off_channel_failures_and_tests(self):
        data = enabled()
        data['events'][notify.BATCH_READY] = False
        sent = []
        senders = dict(discord=lambda s, m: sent.append(m), webhook=lambda s, m: 1 / 0,
                       email=lambda s, m: sent.append(m))
        self.assertEqual(notify.send(data, self.message(), senders), [])
        data['webhook'].update(enabled=True)
        data['email'].update(enabled=True)
        results = notify.send(data, notify.test_message(), senders)  # a test always goes
        self.assertEqual(results, [('discord', ''), ('webhook', 'ZeroDivisionError'), ('email', '')])
        self.assertEqual(len(sent), 2)

    def test_job_finished_logs_and_never_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Path(tmp, 'config', 'notifications.json')
            lines = []
            env = dict(MIGRATARR_HOME=tmp)
            self.assertEqual(notify.job_finished(tmp, dict(kind='run', state='FAILED', message='x'), env,
                                                 lines.append), [])  # no channel: nothing, no log
            data = notify.defaults()
            data['webhook'].update(enabled=True, url='http://127.0.0.1:9/never')  # nothing listens on port 9
            notify.save(data, settings)
            results = notify.job_finished(tmp, dict(kind='run', state='FAILED', message='x', job_id='J'), env,
                                          lines.append)
            self.assertEqual(results[0][0], 'webhook')
            self.assertTrue(results[0][1])
            self.assertEqual(len(lines), 1)
            self.assertTrue(lines[0].startswith('Notification (webhook): not sent: '))
            self.assertNotIn('never', lines[0])


# ---------------------------------------------------------------- schedule

MST = timezone(timedelta(hours=-7))


def at(*args):
    return datetime(*args, tzinfo=MST)


class ScheduleTimeTests(unittest.TestCase):
    weekly = dict(enabled=True, days=['mon', 'thu'], time='03:00')

    def test_occurrences(self):
        thursday_noon = at(2026, 11, 5, 12, 0)
        self.assertEqual(schedule.latest_occurrence(thursday_noon, self.weekly, MST), at(2026, 11, 5, 3, 0))
        self.assertEqual(schedule.latest_occurrence(at(2026, 11, 5, 2, 59), self.weekly, MST), at(2026, 11, 2, 3, 0))
        self.assertEqual(schedule.next_occurrence(thursday_noon, self.weekly, MST), at(2026, 11, 9, 3, 0))
        self.assertIsNone(schedule.latest_occurrence(thursday_noon, dict(self.weekly, days=[]), MST))

    def test_due_catches_up_once_and_never_before_switching_on(self):
        state = {}
        self.assertEqual(schedule.due(at(2026, 11, 5, 3, 0), self.weekly, state, MST), at(2026, 11, 5, 3, 0))
        self.assertEqual(schedule.due(at(2026, 11, 5, 14, 0), self.weekly, state, MST), at(2026, 11, 5, 3, 0))
        self.assertIsNone(schedule.due(at(2026, 11, 5, 15, 1), self.weekly, state, MST))  # > 12 h late
        self.assertIsNone(schedule.due(at(2026, 11, 5, 4, 0), dict(self.weekly, enabled=False), state, MST))
        state = {'last_due': at(2026, 11, 5, 3, 0).isoformat()}
        self.assertIsNone(schedule.due(at(2026, 11, 5, 4, 0), self.weekly, state, MST))
        state = {'enabled_at': at(2026, 11, 5, 10, 0).isoformat()}
        self.assertIsNone(schedule.due(at(2026, 11, 5, 11, 0), self.weekly, state, MST))
        self.assertEqual(schedule.due(at(2026, 11, 9, 3, 0), self.weekly, state, MST), at(2026, 11, 9, 3, 0))

    def test_local_time_follows_daylight_saving(self):
        old = os.environ.get('TZ')
        os.environ['TZ'] = 'America/Denver'
        import time
        time.tzset()
        try:
            from datetime import date
            self.assertEqual(schedule.local_time(date(2026, 10, 31), 3, 0).utcoffset(), timedelta(hours=-6))
            self.assertEqual(schedule.local_time(date(2026, 11, 1), 3, 0).utcoffset(), timedelta(hours=-7))
        finally:
            if old is None:
                os.environ.pop('TZ')
            else:
                os.environ['TZ'] = old
            time.tzset()


class BatchWaitingTests(Temp):
    def setUp(self):
        super().setUp()
        Fixture(self.root)

    def clear(self, approvals=True, journals=True):
        if approvals:
            shutil.rmtree(self.root / 'approvals')
        if journals:
            shutil.rmtree(self.root / 'execution_logs')

    def test_untouched_batch_is_unreviewed(self):
        self.clear()
        self.assertEqual(schedule.batch_waiting(self.root, {}),
                         dict(run_id=RUN, kind='unreviewed', count=11, size_gb=66.0))

    def test_approved_but_not_run(self):
        self.clear(approvals=False)
        from migratarr import read_api
        approved = [r for r in read_api.run_status(self.root, RUN)['rows'] if r['approval'] == read_api.APPROVED]
        waiting = schedule.batch_waiting(self.root, {})
        self.assertEqual((waiting['kind'], waiting['count']), ('approved', len(approved)))
        self.assertGreater(len(approved), 0)

    def test_looked_at_and_nothing_left_waiting(self):
        waiting = schedule.batch_waiting(self.root, {})  # the fixture: approvals and journals in many states
        self.assertTrue(waiting is None or waiting['kind'] == 'approved')
        self.clear(approvals=False, journals=True)
        self.assertIsNotNone(schedule.batch_waiting(self.root, {}))
        self.assertIsNone(schedule.batch_waiting(self.root, {'reviewed': [RUN]}))

    def test_mark_reviewed_keeps_a_short_list(self):
        for n in range(schedule.KEEP_REVIEWED + 5):
            schedule.mark_reviewed(self.state_file, f'20260101T0000{n:02d}Z')
        reviewed = schedule.load_state(self.state_file)['reviewed']
        self.assertEqual(len(reviewed), schedule.KEEP_REVIEWED)
        self.assertEqual(reviewed[-1], f'20260101T0000{schedule.KEEP_REVIEWED + 4:02d}Z')


class TickTests(Temp):
    def setUp(self):
        super().setUp()
        Fixture(self.root)
        shutil.rmtree(self.root / 'approvals')
        shutil.rmtree(self.root / 'execution_logs')  # an untouched batch waits
        data = enabled()
        data['schedule'].update(enabled=True, days=list(notify.DAYS), time='03:00')
        notify.save(data, self.settings_file)
        self.sent, self.started, self.job, self.failing = [], [], None, []

    def tick(self, now):
        return schedule.tick(self.root, now, settings_file=self.settings_file, state_file=self.state_file,
                             active_job=lambda: self.job, start_plan=lambda: self.started.append(1) or 'J',
                             gate=lambda: self.failing, send=lambda s, m: self.sent.append(m), log=lambda x: None,
                             tzinfo=MST)

    def test_waits_for_a_job_then_reminds_once(self):
        self.job = {'job_id': 'running'}
        self.assertEqual(self.tick(at(2026, 11, 5, 3, 0)), 'waiting-job')
        self.assertNotIn('last_due', schedule.load_state(self.state_file))
        self.job = None
        self.assertEqual(self.tick(at(2026, 11, 5, 5, 0)), 'reminded')
        self.assertEqual(self.tick(at(2026, 11, 5, 5, 1)), '')  # handled: not again
        self.assertEqual(len(self.sent), 1)
        self.assertIn('still waiting', self.sent[0].title)
        self.assertIn('11 planned moves not reviewed', self.sent[0].text)
        self.assertEqual(self.started, [])

    def test_plans_once_the_batch_is_done(self):
        schedule.mark_reviewed(self.state_file, RUN)
        self.assertEqual(self.tick(at(2026, 11, 5, 3, 0)), 'planned')
        self.assertEqual((self.started, self.sent), ([1], []))  # batch-ready comes from the job itself

    def test_health_gate(self):
        schedule.mark_reviewed(self.state_file, RUN)

        class Check:
            label = 'Radarr'
        self.failing = [Check()]
        self.assertEqual(self.tick(at(2026, 11, 5, 3, 0)), 'gate')
        self.assertEqual(self.started, [])
        self.assertEqual(self.sent[0].event, notify.FAILED)
        self.assertIn('Radarr', self.sent[0].text)

    def test_run_forever_survives_errors(self):
        stop, calls = threading.Event(), []

        def boom(now):
            calls.append(now)
            if len(calls) >= 2:
                stop.set()
            raise RuntimeError('x')
        logged = []
        schedule.run_forever(stop, boom, seconds=0.01, log=logged.append)
        self.assertEqual(len(calls), 2)
        self.assertTrue(all('RuntimeError' in line for line in logged))


# ---------------------------------------------------------------- the settings page

class PageTests(Temp):
    def page(self):
        sent = []
        page = notify_ui.NotificationsPage(self.settings_file, self.state_file,
                                           send=lambda s, m: sent.append(m) or [('discord', '')])
        return page, sent

    def form(self, **values):
        base = {'ui_address': 'http://media:8765', 'schedule.time': '03:00', 'event.batch_ready': 'on',
                'event.run_finished': 'on', 'event.failed': 'on', 'email.port': '587',
                'email.security': 'starttls'}
        base.update(values)
        return {k: [v] for k, v in base.items()}

    def test_save_never_shows_secrets_and_blank_keeps_them(self):
        page, _ = self.page()
        self.assertEqual(page.post(self.form(**{'discord.enabled': 'on', 'discord.url': DISCORD})),
                         ('redirect', '/settings/notifications?done=saved'))
        body = page.get()
        self.assertNotIn('SECRET-TOKEN', body)
        self.assertIn('Saved. Leave blank to keep it.', body)
        self.assertEqual(page.post(self.form(**{'discord.enabled': 'on'})),
                         ('redirect', '/settings/notifications?done=unchanged'))
        self.assertEqual(notify.load(self.settings_file)['discord']['url'], DISCORD)
        page.post(self.form(**{'discord.url.forget': 'on'}))
        self.assertEqual(notify.load(self.settings_file)['discord']['url'], '')

    def test_a_form_with_a_problem_holds_typed_secrets(self):
        page, _ = self.page()
        status, body = page.post(self.form(**{'discord.enabled': 'on', 'discord.url': DISCORD,
                                              'schedule.time': 'later'}))
        self.assertEqual(status, 400)
        self.assertNotIn('SECRET-TOKEN', body)
        self.assertIn('Kept (not saved yet)', body)
        self.assertFalse(self.settings_file.exists())
        held = body.split('name="held" value="')[1].split('"')[0]
        self.assertTrue(held)
        self.assertEqual(page.post(self.form(**{'discord.enabled': 'on', 'held': held}))[0], 'redirect')
        self.assertEqual(notify.load(self.settings_file)['discord']['url'], DISCORD)
        self.assertEqual(page.held, {})  # one-time

    def test_switching_the_schedule_on_never_fires_for_earlier_times(self):
        page, _ = self.page()
        now = datetime(2026, 11, 5, 10, 0, tzinfo=UTC)
        days = {'day.' + d: 'on' for d in notify.DAYS}
        page.post(self.form(**{'schedule.enabled': 'on', **days}), now=now)
        self.assertEqual(schedule.load_state(self.state_file)['enabled_at'], now.isoformat())
        self.assertIsNone(schedule.due(now + timedelta(minutes=1), notify.load(self.settings_file)['schedule'],
                                       schedule.load_state(self.state_file), UTC))

    def test_ui_address_is_prefilled_from_the_browser(self):
        page, _ = self.page()
        self.assertIn('value="http://192.168.1.20:8765"', page.get(host='192.168.1.20:8765'))

    def test_send_a_test(self):
        page, sent = self.page()
        status, body = page.test()
        self.assertIn('No channel is turned on', body)
        self.assertEqual(sent, [])
        page.post(self.form(**{'discord.enabled': 'on', 'discord.url': DISCORD}))
        status, body = page.test()
        self.assertEqual(sent[-1].event, notify.TEST)
        self.assertIn('Discord: sent', body)


class UiTests(Temp):
    def setUp(self):
        super().setUp()
        Fixture(self.root)
        shutil.rmtree(self.root / 'approvals')
        shutil.rmtree(self.root / 'execution_logs')
        self.notifications = notify_ui.NotificationsPage(self.settings_file, self.state_file)
        self.app = migratarr_ui.App(self.root, STORAGE, {}, fake_usage, actions=True, token='T',
                                    notifications=self.notifications)

    def test_page_and_save(self):
        status, _, body = self.app.respond('/settings/notifications', {}, host='media:8765')
        self.assertEqual(status, 200)
        self.assertIn('Schedule &amp; notifications', body.decode())
        result = self.app.respond_post('/settings/notifications', {'token': ['T'], 'schedule.time': ['04:00']})
        self.assertEqual(result, ('redirect', '/settings/notifications?done=saved'))
        self.assertEqual(self.app.respond_post('/settings/notifications', {'token': ['x']})[0], 403)

    def test_done_with_this_batch(self):
        body = self.app.respond('/run/' + RUN, {})[2].decode()
        self.assertNotIn('Done with this batch', body)  # schedule off: no note
        data = notify.defaults()
        data['schedule']['enabled'] = True
        notify.save(data, self.settings_file)
        body = self.app.respond('/run/' + RUN, {})[2].decode()
        self.assertIn('Scheduled planning waits for this batch', body)
        self.assertIn(f'action="/run/{RUN}/reviewed"', body)
        self.assertEqual(self.app.respond_post(f'/run/{RUN}/reviewed', {'token': ['T']}),
                         ('redirect', f'/run/{RUN}?done=reviewed'))
        self.assertEqual(schedule.load_state(self.state_file)['reviewed'], [RUN])
        self.assertNotIn('Scheduled planning waits', self.app.respond('/run/' + RUN, {})[2].decode())

    def test_read_only_view_cannot_save(self):
        app = migratarr_ui.App(self.root, STORAGE, {}, fake_usage, notifications=self.notifications)
        self.assertIn('Read-only view', app.respond('/settings/notifications', {})[2].decode())
        self.assertEqual(app.respond_post('/settings/notifications', {'token': ['T']})[0], 405)


if __name__ == '__main__':
    unittest.main()
