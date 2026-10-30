"""The "Schedule & notifications" settings page of the web UI (migratarr/ui.py).

Secrets (Discord and webhook URLs, the mail password) are never written back into a
page: a saved one shows as kept, a blank field keeps it, "Forget" clears it. When a
form comes back with a problem, secrets just typed are held in memory (15 minutes,
one-time id) instead of being lost. Standard library only.
"""
from datetime import datetime, timezone
import html
import secrets as random
import time

from migratarr import notify, schedule

HOLD_SECONDS = 15 * 60
SECRET_FIELDS = (('discord', 'url'), ('webhook', 'url'), ('email', 'password'))


def e(value):
    return html.escape('' if value is None else str(value), quote=True)


class NotificationsPage:
    def __init__(self, settings_file, state_file, clock=time.monotonic, send=notify.send):
        self.settings_file, self.state_file, self.clock, self.send = settings_file, state_file, clock, send
        self.held = {}  # id -> (expires, {(channel, key): secret})

    # ---- held secrets

    def _hold(self, values):
        now = self.clock()
        self.held = {k: v for k, v in self.held.items() if v[0] > now}
        if not values:
            return ''
        nonce = random.token_urlsafe(16)
        self.held[nonce] = (now + HOLD_SECONDS, values)
        return nonce

    def _take(self, nonce):
        found = self.held.pop(nonce or '', None)
        return found[1] if found and found[0] > self.clock() else {}

    # ---- form

    def parse(self, form, saved):
        """(settings, typed secrets, held secrets) from a posted form, keeping saved secrets left blank."""
        f = lambda name: (form.get(name) or [''])[0].strip()
        on = lambda name: f(name) == 'on'
        data = notify.defaults()
        data['ui_address'] = f('ui_address').rstrip('/')
        data['events'] = {ev: on('event.' + ev) for ev in notify.EVENTS}
        data['schedule'] = {'enabled': on('schedule.enabled'), 'days': [d for d in notify.DAYS if on('day.' + d)],
                            'time': f('schedule.time') or '03:00'}
        for channel in notify.CHANNELS:
            data[channel]['enabled'] = on(channel + '.enabled')
        email = data['email']
        email.update(host=f('email.host'), security=f('email.security') or 'starttls', username=f('email.username'),
                     **{'from': f('email.from')})
        port = f('email.port')
        email['port'] = int(port) if port.isdigit() else (port or 587)
        email['to'] = [a.strip() for a in f('email.to').replace(',', '\n').replace(';', '\n').splitlines() if a.strip()]
        held = self._take(f('held'))
        typed = {}
        for channel, key in SECRET_FIELDS:
            name = f'{channel}.{key}'
            value = f(name)
            if value:
                typed[(channel, key)] = value
                data[channel][key] = value
            elif on(name + '.forget'):
                data[channel][key] = ''
            else:
                data[channel][key] = held.get((channel, key)) or saved[channel].get(key) or ''
                if (channel, key) in held:
                    typed[(channel, key)] = held[(channel, key)]
        return data, typed

    def render(self, data, saved, problems=None, notice='', results=None, token='', host=None, held='',
               held_paths=()):
        """The page body (without the site frame)."""
        problems = problems or {}

        def bad(path):
            return f'<p class="field-error">{e(problems[path])}</p>' if path in problems else ''

        def text(path, label, value, help_text='', kind='text', extra=''):
            return (f'<div class="field{" bad" if path in problems else ""}"><label for="n-{e(path)}">{e(label)}'
                    f'</label><div><input type="{kind}" id="n-{e(path)}" name="{e(path)}" value="{e(value)}"{extra}>'
                    + (f'<p class="hint">{e(help_text)}</p>' if help_text else '') + bad(path) + '</div></div>')

        def check(path, label, value, help_text=''):
            return (f'<div class="field"><span></span><div><label class="check"><input type="checkbox" '
                    f'name="{e(path)}"{" checked" if value else ""}> {e(label)}</label>'
                    + (f'<p class="hint">{e(help_text)}</p>' if help_text else '') + '</div></div>')

        def secret(channel, key, label, help_text):
            path = f'{channel}.{key}'
            is_held = path in held_paths
            stored = bool(saved[channel].get(key))
            placeholder = ('Kept (not saved yet). Enter a new one to replace it.' if is_held else
                           'Saved. Leave blank to keep it.' if stored else '')
            forget = (f'<label class="check"><input type="checkbox" name="{e(path)}.forget"> Forget the saved one'
                      '</label>' if stored and not is_held else '')
            return (f'<div class="field{" bad" if path in problems else ""}"><label for="n-{e(path)}">{e(label)}'
                    f'</label><div><input type="password" id="n-{e(path)}" name="{e(path)}" value="" '
                    f'autocomplete="off" placeholder="{e(placeholder)}"><p class="hint">{e(help_text)}</p>{forget}'
                    + bad(path) + '</div></div>')

        address = data.get('ui_address') or (f'http://{host}' if host else '')
        sch = data['schedule']
        now = datetime.now(timezone.utc).astimezone()
        upcoming = ''
        if saved['schedule'].get('enabled') and saved['schedule'].get('days'):
            when = schedule.next_occurrence(now, saved['schedule'])
            upcoming = f'<p class="notice">Next scheduled plan: {e(when.strftime("%a %b %d, %H:%M"))}.</p>' if when \
                else ''
        days = ''.join(f'<label class="check day"><input type="checkbox" name="day.{d}"'
                       f'{" checked" if d in sch["days"] else ""}> {notify.DAY_LABELS[d]}</label>' for d in notify.DAYS)
        security = ''.join(f'<option value="{v}"{" selected" if data["email"]["security"] == v else ""}>{e(t)}</option>'
                           for v, t in notify.SECURITY)
        result_html = ''
        if results is not None:
            if not results:
                result_html = ('<div class="alert"><p>No channel is turned on. Turn one on and save, then send a '
                               'test.</p></div>')
            else:
                lines = ''.join(f'<li>{e(c.capitalize())}: ' + (f'not sent: {e(err)}' if err else 'sent') + '</li>'
                                for c, err in results)
                cls = 'alert' if any(err for _, err in results) else 'notice'
                result_html = f'<div class="{cls}"><p>Test notification:</p><ul>{lines}</ul></div>'
        token_input = f'<input type="hidden" name="token" value="{e(token)}">'
        body = (
            (f'<p class="notice">{e(notice)}</p>' if notice else '') + result_html +
            '<p class="crumbs"><a href="/settings">Settings &amp; health</a></p>'
            '<h1>Schedule &amp; notifications</h1>'
            '<p class="meta">Plan automatically, and get a message when a batch is ready, moves finish or something '
            'fails. Approving and moving always stay with you, in the UI.</p>'
            + (f'<div class="alert"><p>{len(problems)} thing{"s" if len(problems) != 1 else ""} to fix below; '
               'nothing was saved.</p></div>' if problems else '') +
            f'<form method="post" action="/settings/notifications" class="settings">{token_input}'
            f'<input type="hidden" name="held" value="{e(held)}">'
            '<fieldset><legend>Links</legend>'
            + text('ui_address', 'UI address', address, 'Where links in notifications point: how you open Migratarr '
                   'from your phone or another computer, for example http://192.168.1.20:8765. Blank: no links.')
            + '</fieldset><fieldset><legend>Schedule</legend>'
            + check('schedule.enabled', 'Plan automatically', sch['enabled'],
                    'Planning never approves or moves anything. While the last batch still waits for you, or a health '
                    'check fails, the scheduled plan is skipped and you get a message instead.')
            + f'<div class="field{" bad" if "schedule.days" in problems else ""}"><span>Days</span><div class="days">'
              f'{days}{bad("schedule.days")}</div></div>'
            + text('schedule.time', 'Time', sch['time'], 'Local time of the machine running Migratarr.', 'time')
            + upcoming
            + '</fieldset><fieldset><legend>Send a message when</legend>'
            + ''.join(check('event.' + ev, notify.EVENT_LABELS[ev], data['events'].get(ev)) for ev in notify.EVENTS)
            + '</fieldset><fieldset><legend>Discord</legend>'
            + check('discord.enabled', 'Send to Discord', data['discord']['enabled'])
            + secret('discord', 'url', 'Webhook URL', 'In Discord: channel settings > Integrations > Webhooks > '
                     'Copy Webhook URL.')
            + '</fieldset><fieldset><legend>Webhook</legend>'
            + check('webhook.enabled', 'Send to a webhook', data['webhook']['enabled'],
                    'POSTs JSON: event, title, text, url, details. Works with ntfy, Home Assistant, Gotify and '
                    'similar.')
            + secret('webhook', 'url', 'URL', 'Redirects are not followed.')
            + '</fieldset><fieldset><legend>Email</legend>'
            + check('email.enabled', 'Send email', data['email']['enabled'])
            + text('email.host', 'Mail server', data['email']['host'], 'For example smtp.gmail.com.')
            + text('email.port', 'Port', data['email']['port'], '587 for STARTTLS, 465 for SSL/TLS.', 'number',
                   ' min="1" max="65535"')
            + f'<div class="field"><label for="n-email.security">Connection</label><div><select id="n-email.security" '
              f'name="email.security">{security}</select></div></div>'
            + text('email.username', 'Username', data['email']['username'], 'Blank when the server needs no login.')
            + secret('email', 'password', 'Password', 'For Gmail and similar, an app password.')
            + text('email.from', 'From', data['email']['from'])
            + (f'<div class="field{" bad" if "email.to" in problems else ""}"><label for="n-email.to">To</label><div>'
               f'<textarea id="n-email.to" name="email.to" rows="2">{e(chr(10).join(data["email"]["to"]))}</textarea>'
               '<p class="hint">One or more addresses, one per line.</p>' + bad('email.to') + '</div></div>')
            + '</fieldset><div class="actions"><button type="submit" class="btn primary">Save</button></div></form>'
            f'<form method="post" action="/settings/notifications/test" class="settings">{token_input}'
            '<div class="actions"><button type="submit" class="btn">Send a test</button>'
            '<span class="hint">Sends to every channel turned on in the saved settings.</span></div></form>')
        return body

    # ---- requests

    def get(self, notice='', token='', host=None):
        saved = notify.load(self.settings_file)
        return self.render(saved, saved, notice=notice, token=token, host=host)

    def post(self, form, token='', host=None, now=None):
        """('redirect', url) after a save, or (status, body)."""
        saved = notify.load(self.settings_file)
        data, typed = self.parse(form, saved)
        found = notify.problems(data)
        if found:
            nonce = self._hold(typed)
            paths = {f'{c}.{k}' for c, k in typed} if nonce else set()
            return 400, self.render(data, saved, found, token=token, host=host, held=nonce, held_paths=paths)
        if data == saved:
            return 'redirect', '/settings/notifications?done=unchanged'
        notify.save(data, self.settings_file)
        if data['schedule'] != saved['schedule'] and data['schedule']['enabled']:
            # Times before this change never count (no plan right after switching on or moving the time).
            schedule.schedule_switched_on(self.state_file, now or datetime.now(timezone.utc).astimezone())
        return 'redirect', '/settings/notifications?done=saved'

    def test(self, token=''):
        saved = notify.load(self.settings_file)
        results = self.send(saved, notify.test_message()) if notify.any_channel(saved) else []
        return 200, self.render(saved, saved, results=results, token=token)

    def summary(self):
        """(summary, state HTML) for the Settings & health list."""
        try:
            saved = notify.load(self.settings_file)
        except notify.NotifySettingsError:
            return 'Plan automatically; Discord, webhook, email', '<span class="bad">Unreadable</span>'
        channels = [c.capitalize() for c in notify.CHANNELS if saved[c].get('enabled')]
        parts = ['plans automatically' if saved['schedule'].get('enabled') else 'no schedule',
                 ', '.join(channels) if channels else 'no notifications']
        return ('Plan automatically; Discord, webhook, email', f'<span class="hint">{e("; ".join(parts))}</span>')
