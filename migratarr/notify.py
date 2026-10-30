"""Notifications and the planning schedule: settings, messages and delivery.

Settings live in the data folder as config/notifications.json, readable by its
owner only (webhook URLs and the mail password are secrets). They hold:

- ui_address: where links in notifications point (http://<server>:8765);
- events: which kinds of message are sent (batch ready, run finished, failed);
- schedule: plan automatically on some weekdays at a time (migratarr/schedule.py);
- the channels: Discord webhook, a generic webhook (JSON POST), and email (SMTP).

Jobs call job_finished() when they end (migratarr/jobs.py); the scheduler calls
send() for reminders. Delivery never raises and never follows redirects; error
texts name the channel and the reason, never a URL or password. Standard library only.
"""
from dataclasses import dataclass, field
from email.message import EmailMessage
import json
import os
from pathlib import Path
import re
import smtplib
import socket
import ssl
import time
from urllib.parse import urlsplit
import urllib.error
import urllib.request

FILE_NAME = 'notifications.json'
TIMEOUT = 10
BATCH_READY, RUN_FINISHED, FAILED, TEST = 'batch_ready', 'run_finished', 'failed', 'test'
EVENTS = (BATCH_READY, RUN_FINISHED, FAILED)
EVENT_LABELS = {BATCH_READY: 'A batch is ready to review (or still waiting)',
                RUN_FINISHED: 'Moves finished',
                FAILED: 'Planning or moving failed, or a scheduled plan was skipped'}
DAYS = ('mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun')
DAY_LABELS = dict(zip(DAYS, ('Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun')))
SECURITY = (('starttls', 'STARTTLS (usually port 587)'), ('ssl', 'SSL/TLS (usually port 465)'),
            ('none', 'None (local relay only)'))
CHANNELS = ('discord', 'webhook', 'email')
SECRETS = {'discord': ('url',), 'webhook': ('url',), 'email': ('password',)}
DISCORD_HOSTS = {'discord.com', 'discordapp.com', 'canary.discord.com', 'ptb.discord.com'}


def defaults():
    return {'version': 1, 'ui_address': '',
            'events': {e: True for e in EVENTS},
            'schedule': {'enabled': False, 'days': list(DAYS), 'time': '03:00'},
            'discord': {'enabled': False, 'url': ''},
            'webhook': {'enabled': False, 'url': ''},
            'email': {'enabled': False, 'host': '', 'port': 587, 'security': 'starttls', 'username': '',
                      'password': '', 'from': '', 'to': []}}


def settings_path(environ=None):
    from migratarr.paths import config_dir
    return config_dir(environ) / FILE_NAME


class NotifySettingsError(ValueError):
    """A problem with the settings file; the message is safe to show."""


def load(path=None):
    """The settings (defaults where the file is missing or a value is absent)."""
    path = Path(path) if path else settings_path()
    data = defaults()
    try:
        stored = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return data
    except (OSError, ValueError) as exc:
        raise NotifySettingsError(f'Cannot read {path}: {exc}') from None
    if not isinstance(stored, dict):
        raise NotifySettingsError(f'{path} is damaged; save the notification settings again.')
    for key, value in stored.items():
        if isinstance(data.get(key), dict) and isinstance(value, dict):
            data[key].update(value)
        elif key in data:
            data[key] = value
    return data


def save(data, path=None):
    """Write the settings owner-only, atomically."""
    path = Path(path) if path else settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name('.' + path.name + '.tmp')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)
            f.write('\n')
            f.flush()
            os.fsync(f.fileno())
        os.chmod(temp, 0o600)
        os.replace(temp, path)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


# ---------------------------------------------------------------- checking

def check_url(value, discord=False):
    """'' when fine, else what's wrong (for a form)."""
    try:
        parts = urlsplit((value or '').strip())
    except ValueError:
        return 'Not a valid address.'
    if parts.scheme not in ('http', 'https') or not parts.hostname:
        return 'Use an address starting with http:// or https://.'
    if any(ord(c) < 33 for c in value.strip()):
        return 'The address contains spaces or control characters.'
    if discord and (parts.scheme != 'https' or parts.hostname not in DISCORD_HOSTS
                    or not parts.path.startswith('/api/webhooks/')):
        return 'Use the Discord webhook URL (https://discord.com/api/webhooks/…): channel settings > Integrations.'
    return ''


def check_ui_address(value):
    value = (value or '').strip()
    if not value:
        return ''
    problem = check_url(value)
    if problem:
        return problem
    parts = urlsplit(value)
    if parts.username or parts.password or parts.query or parts.fragment:
        return 'Just the address, for example http://192.168.1.20:8765, without a login, ? or #.'
    return ''


EMAIL = re.compile(r'[^@\s<>,;]+@[^@\s<>,;]+\.[^@\s<>,;]+')


def problems(data):
    """{field: message} for settings that can't be saved."""
    found = {}
    if check_ui_address(data.get('ui_address')):
        found['ui_address'] = check_ui_address(data.get('ui_address'))
    schedule = data['schedule']
    if not re.fullmatch(r'([01]\d|2[0-3]):[0-5]\d', schedule.get('time') or ''):
        found['schedule.time'] = 'Use a time like 03:00 (24-hour).'
    if schedule.get('enabled') and not schedule.get('days'):
        found['schedule.days'] = 'Pick at least one day.'
    if data['discord'].get('enabled') or data['discord'].get('url'):
        problem = check_url(data['discord'].get('url'), discord=True) if data['discord'].get('url') else \
            'Paste the Discord webhook URL.'
        if problem:
            found['discord.url'] = problem
    if data['webhook'].get('enabled') or data['webhook'].get('url'):
        problem = check_url(data['webhook'].get('url')) if data['webhook'].get('url') else 'Give the webhook URL.'
        if problem:
            found['webhook.url'] = problem
    email = data['email']
    if email.get('enabled'):
        if not (email.get('host') or '').strip():
            found['email.host'] = 'Give the mail server, for example smtp.gmail.com.'
        if not isinstance(email.get('port'), int) or not 0 < email['port'] < 65536:
            found['email.port'] = 'Use a port number, usually 587 or 465.'
        if email.get('security') not in dict(SECURITY):
            found['email.security'] = 'Pick how to connect.'
        if not EMAIL.fullmatch((email.get('from') or '').strip()):
            found['email.from'] = 'Give the address messages come from.'
        bad = [a for a in email.get('to') or [] if not EMAIL.fullmatch(a)]
        if not email.get('to') or bad:
            found['email.to'] = 'Give one or more addresses to send to' + (f' ({bad[0]} looks wrong).' if bad else '.')
    return found


def any_channel(data):
    return any(data[c].get('enabled') for c in CHANNELS)


# ---------------------------------------------------------------- messages

@dataclass
class Message:
    event: str
    title: str
    text: str
    path: str = ''              # a page of the UI, linked when ui_address is set
    details: dict = field(default_factory=dict)

    def link(self, ui_address):
        if not (ui_address and self.path):
            return ''
        return ui_address.rstrip('/') + self.path


def gb(value):
    return f'{value:,.1f} GB' if value >= 0.05 else f'{value * 1024:,.0f} MB'


def job_message(base, record):
    """The Message for a finished job record (migratarr/jobs.py), or None when nothing is worth sending."""
    from migratarr import read_api
    kind, state, job_id = record.get('kind'), record.get('state'), record.get('job_id') or ''
    run_id = record.get('run_id')
    if state == 'FAILED':
        what = 'Planning' if kind == 'plan' else 'Moving'
        return Message(FAILED, f'Migratarr: {what.lower()} failed', f'{what} stopped: {record.get("message")}',
                       f'/job/{job_id}', dict(job_id=job_id, run_id=run_id, kind=kind))
    if state != 'FINISHED':
        return None  # stopped on request: you asked for it
    if kind == 'plan':
        try:
            rows = read_api.get_manifest(base, run_id)['rows'] if run_id else []
        except Exception:  # the plan finished; the message still goes out without the counts
            rows = None
        if rows == []:
            return None  # nothing to move
        size = sum(float(r.get('size_gb') or 0) for r in rows or [])
        count = f'{len(rows)} move{"s" if len(rows) != 1 else ""} ({gb(size)})' if rows else 'A new plan'
        return Message(BATCH_READY, f'Migratarr: {count} ready to review',
                       f'{count} planned in run {run_id}. Nothing moves until you approve.',
                       f'/run/{run_id}', dict(run_id=run_id, moves=len(rows or []), size_gb=round(size, 2)))
    done, skipped = record.get('done') or [], record.get('skipped') or []
    size = sum(float(d.get('size_gb') or 0) for d in done)
    text = f'{len(done)} move{"s" if len(done) != 1 else ""} done ({gb(size)})'
    if skipped:
        text += f', {len(skipped)} skipped or held'
    return Message(RUN_FINISHED, 'Migratarr: ' + text, text + '.', f'/run/{run_id}' if run_id else f'/job/{job_id}',
                   dict(run_id=run_id, moved=len(done), skipped=len(skipped), size_gb=round(size, 2)))


# ---------------------------------------------------------------- delivery

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _post_json(url, payload, opener=None):
    opener = opener or urllib.request.build_opener(_NoRedirect)
    request = urllib.request.Request(url, data=json.dumps(payload).encode(), method='POST',
                                     headers={'Content-Type': 'application/json', 'User-Agent': 'Migratarr'})
    with opener.open(request, timeout=TIMEOUT) as response:
        if not 200 <= response.status < 300:
            raise OSError(f'HTTP {response.status}')


def _reason(exc):
    """Why a delivery failed, without the URL or any credential."""
    if isinstance(exc, urllib.error.HTTPError):
        return f'HTTP {exc.code}' + (' (redirects are not followed)' if 300 <= exc.code < 400 else '')
    if isinstance(exc, urllib.error.URLError):
        exc = exc.reason if isinstance(exc.reason, BaseException) else OSError(str(exc.reason))
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return 'timed out'
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return f'the mail server refused the login ({exc.smtp_code})'
    if isinstance(exc, smtplib.SMTPException):
        return 'mail server: ' + type(exc).__name__
    if isinstance(exc, ssl.SSLError):
        return 'TLS: ' + (exc.reason or 'handshake failed')
    if isinstance(exc, OSError):
        return exc.strerror or str(exc) or type(exc).__name__
    return type(exc).__name__


def plain_text(message, ui_address):
    link = message.link(ui_address)
    return message.text + (f'\n{link}' if link else '')


def send_discord(settings, message, post=_post_json):
    link = message.link(settings.get('ui_address'))
    content = f'**{message.title}**\n{message.text}' + (f'\n{link}' if link else '')
    post(settings['discord']['url'], {'content': content[:1900], 'username': 'Migratarr',
                                      'allowed_mentions': {'parse': []}})


def send_webhook(settings, message, post=_post_json):
    post(settings['webhook']['url'], {'event': message.event, 'title': message.title, 'text': message.text,
                                      'url': message.link(settings.get('ui_address')) or None,
                                      'details': message.details, 'sent_utc': time.strftime(
                                          '%Y-%m-%dT%H:%M:%SZ', time.gmtime())})


def send_email(settings, message, smtp=None):
    e = settings['email']
    mail = EmailMessage()
    mail['Subject'], mail['From'], mail['To'] = message.title, e['from'], ', '.join(e['to'])
    mail.set_content(plain_text(message, settings.get('ui_address')) + '\n\n-- \nMigratarr')
    context = ssl.create_default_context()
    if smtp is None:
        smtp = smtplib.SMTP_SSL if e['security'] == 'ssl' else smtplib.SMTP
    kwargs = dict(timeout=TIMEOUT)
    if e['security'] == 'ssl':
        kwargs['context'] = context
    with smtp(e['host'], int(e['port']), **kwargs) as server:
        if e['security'] == 'starttls':
            server.starttls(context=context)
        if e.get('username'):
            server.login(e['username'], e.get('password') or '')
        server.send_message(mail)


SENDERS = {'discord': send_discord, 'webhook': send_webhook, 'email': send_email}


def send(settings, message, senders=None):
    """Deliver to every enabled channel when the message's event is switched on (a test always goes).

    Returns [(channel, error or '')]; never raises.
    """
    senders = senders or SENDERS
    if message.event != TEST and not (settings.get('events') or {}).get(message.event):
        return []
    results = []
    for channel in CHANNELS:
        if not settings[channel].get('enabled'):
            continue
        try:
            senders[channel](settings, message)
            results.append((channel, ''))
        except Exception as exc:  # one channel failing doesn't stop the others
            results.append((channel, _reason(exc)))
    return results


def test_message():
    return Message(TEST, 'Migratarr: test notification',
                   'This is a test from Migratarr. Notifications are working.', '/settings/notifications')


def job_finished(base, record, environ=None, log=lambda line: None):
    """Called by a job when it ends. Logs one line per channel; never raises."""
    try:
        settings = load(settings_path(environ))
        if not any_channel(settings):
            return []
        message = job_message(base, record)
        if message is None:
            return []
        results = send(settings, message)
        for channel, error in results:
            log(f'Notification ({channel}): ' + (f'not sent: {error}' if error else 'sent'))
        return results
    except Exception as exc:
        log('Notifications not sent: ' + _reason(exc))
        return []
