"""Settings & health pages for migratarr_ui.py (plan phase 4; phase 2's read-only page).

    GET  /settings                    every section, its problems, and health checks
    GET  /settings/<section>          that section's form (values only, when read-only)
    POST /settings/<section>/review   form -> typed values -> validate -> show the diff
    POST /settings/<section>/save     save the reviewed change (backup + atomic write)

Everything about the settings files goes through a settings_contract
SettingsBackend; this module only draws forms and turns form text into typed
values. A reviewed change is held here, in memory, under a one-time id, so
pasted keys never travel back to the browser in a hidden field (a form shown
again with a problem holds its pasted keys the same way); saving also
re-checks the files' revision so a change reviewed against older files is
refused. Secret values are never put in a page. Standard library only.
"""
import dataclasses
import secrets
import time
from urllib.parse import urlencode

from migratarr import settings_contract as sc
from migratarr.ui import e, form, page

PENDING_SECONDS = 15 * 60
PENDING_LIMIT = 20
STATUS_TEXT = {sc.OK: 'OK', sc.WARN: 'Check', sc.FAIL: 'Problem', sc.SKIPPED: 'Skipped'}

CSS = """
.sections{list-style:none;padding:0;margin:0 0 28px;border-top:1px solid var(--line)}
.sections li{display:flex;justify-content:space-between;gap:16px;padding:12px 4px;border-bottom:1px solid var(--line)}
.sections .ok{color:var(--teal)}.sections .bad{color:var(--red)}
fieldset{border:1px solid var(--line);border-radius:8px;padding:12px 16px 4px;margin:0 0 16px}
legend{padding:0 6px;color:var(--muted);font-size:14px}
.field{display:grid;grid-template-columns:220px 1fr;gap:4px 16px;margin:0 0 14px;align-items:start}
.field label{font-weight:600;padding-top:6px}
.field .help,.field .err{grid-column:2;font-size:13px;margin:0}
.field .help{color:var(--muted)}.field .err{color:var(--red)}.field .warn{color:var(--amber)}
input[type=text],input[type=password],input[type=number],select,textarea{width:100%;max-width:560px;
background:var(--bg);color:var(--text);border:1px solid var(--slate);border-radius:6px;padding:6px 8px;font:inherit}
input:focus-visible,select:focus-visible,textarea:focus-visible{outline:2px solid var(--teal);outline-offset:1px}
.field.bad input,.field.bad select,.field.bad textarea{border-color:var(--red)}
.checks td.ok{color:var(--teal)}.checks td.warn{color:var(--amber)}.checks td.fail{color:var(--red)}
.checks td.skipped{color:var(--muted)}
.diff td.before{color:var(--muted);text-decoration:line-through}
.actions{display:flex;gap:12px;align-items:center;margin-top:8px}
.wizard-steps{display:flex;flex-wrap:wrap;gap:6px 18px;list-style:none;padding:0;margin:0 0 12px;font-size:14px;color:var(--muted)}
.wizard-steps li.now{color:var(--text);font-weight:600}.wizard-steps li.done::before{content:"\\2713  ";color:var(--teal)}
.found{color:var(--muted);font-size:14px}.opt{margin-right:14px;font-weight:400}
@media (max-width:760px){.field{grid-template-columns:1fr}.field .help,.field .err{grid-column:1}}
"""


# ---------------------------------------------------------------- form text <-> typed values (pure)

def form_name(path):
    return 'f:' + path


def show(value, kind):
    """A value as form text. Secrets are never shown."""
    if kind == sc.SECRET or value is None:
        return ''
    if kind == sc.LINES:
        return '\n'.join(str(v) for v in value)
    if kind == sc.BOOL:
        return 'on' if value else ''
    return str(value)


def held_secret(value):
    """What a blank secret field keeps: a stored key (SECRET_KEPT), or a key pasted earlier in this
    session and held in memory (a non-empty str: the wizard's draft, a Settings form shown again)."""
    return value if value is sc.SECRET_KEPT or (isinstance(value, str) and value) else None


def parse_form(fields, form_data, current):
    """Turn posted text into typed values. Returns (values, problems).

    Read-only fields keep their current value. A blank secret keeps what is stored, or a key pasted
    earlier and held in memory (``current`` holds it; it is never put back into a page).
    """
    values, problems = {}, []
    for f in fields:
        if f.read_only:
            if f.path in current:
                values[f.path] = current[f.path]
            continue
        raw_list = form_data.get(form_name(f.path), [])
        raw = (raw_list[0] if raw_list else '').strip()
        if f.kind == sc.BOOL:
            values[f.path] = raw == 'on'
        elif f.kind == sc.MULTI:
            allowed = {v for v, _ in f.choices}
            picked = [v for v in raw_list if v in allowed]
            if len(picked) != len(raw_list):
                problems.append(sc.Problem(f.path, 'Choose from the list.'))
            values[f.path] = picked
        elif f.kind == sc.LINES:
            values[f.path] = [line.strip() for line in (raw_list[0] if raw_list else '').splitlines()
                              if line.strip()]
        elif f.kind == sc.SECRET:
            values[f.path] = raw if raw else held_secret(current.get(f.path))
        elif not raw:
            values[f.path] = None
        elif f.kind == sc.INTEGER:
            try:
                values[f.path] = int(raw)
            except ValueError:
                problems.append(sc.Problem(f.path, 'Enter a whole number.'))
                values[f.path] = raw
        elif f.kind == sc.NUMBER:
            try:
                number = float(raw)
                values[f.path] = int(number) if number.is_integer() and '.' not in raw else number
            except ValueError:
                problems.append(sc.Problem(f.path, 'Enter a number.'))
                values[f.path] = raw
        elif f.kind == sc.CHOICE:
            if raw not in {v for v, _ in f.choices}:
                problems.append(sc.Problem(f.path, 'Choose from the list.'))
            values[f.path] = raw
        else:
            values[f.path] = raw
        if f.required and values.get(f.path) in (None, '', []):
            problems.append(sc.Problem(f.path, 'Required.'))
    return values, problems


def render_input(f, value):
    name, ident = e(form_name(f.path)), e('f-' + f.path.replace('.', '-'))
    off = ' disabled' if f.read_only else ''
    req = ' required' if f.required and f.kind not in (sc.BOOL, sc.SECRET, sc.MULTI) else ''
    if f.kind == sc.BOOL:
        checked = ' checked' if value else ''
        return f'<input type="checkbox" id="{ident}" name="{name}" value="on"{checked}{off}>'
    if f.kind == sc.CHOICE:
        opts = ''.join(f'<option value="{e(v)}"{" selected" if v == value else ""}>{e(t)}</option>'
                       for v, t in f.choices)
        return f'<select id="{ident}" name="{name}"{off}{req}>{opts}</select>'
    if f.kind == sc.MULTI:
        picked = set(value or ())
        return ''.join(f'<label class="opt"><input type="checkbox" name="{name}" value="{e(v)}"'
                       f'{" checked" if v in picked else ""}{off}> {e(t)}</label> ' for v, t in f.choices)
    if f.kind == sc.LINES:
        return f'<textarea id="{ident}" name="{name}" rows="4"{off}>{e(show(value, f.kind))}</textarea>'
    if f.kind == sc.SECRET:
        # Never the value: a pasted key held in memory (not saved yet) shows only that it is kept.
        hint = ('A key is saved. Leave blank to keep it.' if value is sc.SECRET_KEPT else
                'Key kept (not saved yet). Enter a new one to replace it.' if held_secret(value) else
                'Paste the key.')
        return (f'<input type="password" id="{ident}" name="{name}" autocomplete="off" '
                f'placeholder="{e(hint)}"{off}>')
    kind = 'number' if f.kind in (sc.INTEGER, sc.NUMBER) else 'text'
    step = ' step="any"' if f.kind == sc.NUMBER else ''
    return f'<input type="{kind}"{step} id="{ident}" name="{name}" value="{e(show(value, f.kind))}"{off}{req}>'


def render_fields(fields, values, problems):
    by_path = {}
    for p in problems:
        by_path.setdefault(p.path, []).append(p)
    out, group = [], None
    for f in fields:
        if f.group != group:
            if group is not None:
                out.append('</fieldset>')
            group = f.group
            out.append(f'<fieldset><legend>{e(group)}</legend>' if group else '<fieldset>')
        mine = by_path.get(f.path, [])
        errs = ''.join(f'<p class="err{" warn" if p.severity == sc.WARN else ""}">{e(p.message)}</p>'
                       for p in mine)
        help_ = f'<p class="help">{e(f.help)}</p>' if f.help else ''
        bad = ' bad' if any(p.severity == sc.FAIL for p in mine) else ''
        out.append(f'<div class="field{bad}"><label for="{e("f-" + f.path.replace(".", "-"))}">{e(f.label)}</label>'
                   f'<div>{render_input(f, values.get(f.path))}</div>{help_}{errs}</div>')
    if group is not None:
        out.append('</fieldset>')
    known = {f.path for f in fields}
    loose = [p for p in problems if p.path not in known]
    if loose:
        out.insert(0, '<div class="alert">' + ''.join(f'<p>{e(p.message)}</p>' for p in loose) + '</div>')
    return ''.join(out)


def change_text(change, side):
    value = getattr(change, side)
    if change.secret:  # never the value itself
        return 'saved key' if value is sc.SECRET_KEPT else 'none' if value in (None, '') else 'new key'
    if value is None or value == '':
        return '(empty)'
    if isinstance(value, (list, tuple)):
        return ', '.join(str(v) for v in value) or '(empty)'
    if isinstance(value, bool):
        return 'Yes' if value else 'No'
    return str(value)


def render_checks(checks):
    if not checks:
        return '<p class="empty">Health checks are not available yet.</p>'
    def fix(c):
        text = getattr(c, 'fix', '')
        return f'<div class="hint">{e(text)}</div>' if text and c.status in (sc.WARN, sc.FAIL) else ''
    rows = ''.join(f'<tr><td class="{e(c.status)}">{e(STATUS_TEXT.get(c.status, c.status))}</td>'
                   f'<td>{e(c.label)}</td><td>{e(c.detail)}{fix(c)}</td></tr>' for c in checks)
    return f'<table class="checks"><thead><tr><th>Status</th><th>Check</th><th>Detail</th></tr></thead>' \
           f'<tbody>{rows}</tbody></table>'


# ---------------------------------------------------------------- pages

class SettingsPages:
    css = CSS

    def __init__(self, backend, health=None, clock=time.monotonic, on_saved=lambda result: None):
        self.backend, self.health, self.clock, self.on_saved = backend, health, clock, on_saved
        self.pending, self.held_keys = {}, {}
        self.last_checks = []

    def section(self, section_id):
        for spec in self.backend.sections():
            if spec.id == section_id:
                return spec
        raise sc.SettingsError('There is no settings section called ' + section_id + '.')

    # GET
    def index(self, ctx, notice='', run_checks=False, overview='', setup=False):
        """Sections with edit links, health checks (only when asked: they contact services), then
        ``overview`` (the effective settings, from the Phase 2 view)."""
        status = self.backend.status()
        by_section = {}
        for p in sc.blocking(status.problems):
            by_section.setdefault(p.path.split('.', 1)[0], []).append(p)
        items = []
        for spec in self.backend.sections():
            n = len(by_section.get(spec.id, []))
            state = (f'<span class="bad">{n} problem{"s" if n != 1 else ""}</span>' if n
                     else '<span class="ok">OK</span>')
            items.append(f'<li><span><a href="/settings/{e(spec.id)}">{e(spec.title)}</a>'
                         f'<br><span class="hint">{e(spec.summary)}</span></span>{state}</li>')
        note = f'<p class="notice">{e(notice)}</p>' if notice else ''
        if setup:
            note += ('<div class="alert"><p>Migratarr\'s settings have problems, so planning, approving and '
                     'moving are off. Fix them here; they take effect as soon as they are saved.</p></div>')
        if not self.health:
            health = render_checks([])
        elif run_checks:
            self.last_checks = list(self.health.run_checks())
            health = render_checks(self.last_checks) + '<p><a class="btn" href="/settings?check=1">Run again</a></p>'
        else:
            health = ('<p class="hint">Checks contact each service, list Radarr/Sonarr root folders and (NAS '
                      'topology) connect to the NAS over SSH. They change nothing. Planning needs every check '
                      'to pass; warnings are fine.</p><p><a class="btn" href="/settings?check=1">Run health '
                      'checks</a></p>')
        editable = bool(ctx and ctx.get('actions'))
        how = ('Edit a section, review the change, then save. Every save keeps a backup of the file it replaces.'
               if editable else 'Read-only view: start the UI without --read-only to change settings.')
        body = (f'{note}<h1>Settings &amp; health</h1><p class="meta">{how} '
                f'<code>migratarr doctor</code> runs the same checks from a terminal.</p>'
                f'<ul class="sections">{"".join(items)}</ul>'
                f'<h2>Health</h2>{health}{overview}')
        return page('Settings & health', body, ctx=ctx)

    def edit(self, section_id, ctx, values=None, problems=()):
        spec = self.section(section_id)
        current = self.backend.current(section_id)
        values = current if values is None else values
        fields = self.backend.fields(section_id, values)
        if problems == () and values is current:
            problems = [p for p in self.backend.status().problems
                        if p.path == section_id or p.path.startswith(section_id + '.')]
        crumbs = '<p class="crumbs"><a href="/settings">Settings</a></p>'
        editable = bool(ctx and ctx.get('actions')) and not all(f.read_only for f in fields)
        if not editable:
            fields = [dataclasses.replace(f, read_only=True) for f in fields]
        inner = render_fields(fields, values, problems)
        if values is current:  # opened afresh (or Cancel): keys held for an earlier form are dropped
            self.drop_keys(section_id)
        if editable:
            busy = ('<p class="hint">A job is running; you can review a change but not save it until it '
                    'finishes.</p>' if ctx.get('job') else '')
            # Shown again with a problem: keys pasted into it stay here, in memory, under a one-time id.
            pasted = {f.path: values[f.path] for f in fields
                      if f.kind == sc.SECRET and isinstance(values.get(f.path), str) and values[f.path]}
            hidden = [('keys', self.hold_keys(section_id, pasted))] if pasted else []
            inner = form(f'/settings/{section_id}/review', ctx, hidden, extra=inner + busy,
                         button='Review changes')
            inner = inner.replace('class="inline"', 'class="settings"', 1)
        return page(spec.title, f'{crumbs}<h1>{e(spec.title)}</h1>'
                                f'<p class="meta">{e(spec.summary)}</p>{inner}', ctx=ctx)

    # POST
    def review(self, section_id, form_data, ctx):
        self.section(section_id)
        current = self.backend.current(section_id)
        # Keys pasted into this form before it was shown again: a blank field keeps them.
        current = dict(current, **self.take_keys((form_data.get('keys') or [''])[0], section_id))
        # Fields can depend on values (one group per disk): use the shape of the form that was posted,
        # which may hold a disk added (or lack one removed) since the files were read.
        posted = {k[2:] for k in form_data if k.startswith('f:')}
        shape = {k: current.get(k) for k in posted} if posted else current
        fields = self.backend.fields(section_id, shape)
        values, problems = parse_form(fields, form_data, current)
        normalize = getattr(self.backend, 'normalize', None)
        if normalize and not sc.blocking(problems):
            values, notes = normalize(section_id, values)
            if notes:  # e.g. a disk was added: show it (and its placement choices) before reviewing
                return (400 if sc.blocking(notes) else 200), self.edit(section_id, ctx, values, notes)
        if not sc.blocking(problems):
            problems += self.backend.validate(section_id, values)
        if sc.blocking(problems):
            return 400, self.edit(section_id, ctx, values, problems)
        changes = self.backend.diff(section_id, values)
        if not changes:
            return 'redirect', '/settings?' + urlencode({'done': 'unchanged'})
        nonce = self.hold(section_id, values)
        rows = ''.join(f'<tr><td>{e(c.label)}</td><td class="before">{e(change_text(c, "before"))}</td>'
                       f'<td>{e(change_text(c, "after"))}</td></tr>' for c in changes)
        warns = ''.join(f'<p class="warn">{e(p.message)}</p>' for p in problems if p.severity == sc.WARN)
        spec = self.section(section_id)
        body = (f'<p class="crumbs"><a href="/settings">Settings</a> / '
                f'<a href="/settings/{e(section_id)}">{e(spec.title)}</a></p>'
                f'<h1>Review: {e(spec.title)}</h1><p class="meta">Nothing is saved until you press Save. '
                f'The current file is backed up first.</p>{warns}'
                f'<table class="diff"><thead><tr><th>Setting</th><th>Now</th><th>After saving</th></tr></thead>'
                f'<tbody>{rows}</tbody></table><div class="actions">'
                + form(f'/settings/{section_id}/save', ctx, [('change', nonce)], 'Save', 'btn primary')
                + f'<a href="/settings/{e(section_id)}">Cancel</a></div>')
        return 200, page('Review ' + spec.title, body, ctx=ctx)

    def save(self, section_id, form_data, ctx):
        nonce = (form_data.get('change') or [''])[0]
        held = self.take(nonce)
        if not held or held[0] != section_id:
            raise sc.SaveRefused('This review has expired or was already used. Open the section and review '
                                 'the change again.')
        _, values, revision = held
        if self.backend.status().revision != revision:
            raise sc.SaveRefused('The settings files changed since you reviewed this. Review the change again.')
        if sc.blocking(self.backend.validate(section_id, values)):
            raise sc.SaveRefused('The change no longer passes validation. Review it again.')
        result = self.backend.save(section_id, values, revision)
        self.on_saved(result)
        return 'redirect', '/settings?' + urlencode({'done': 'saved', 'n': len(result.files)})

    # pasted keys held between a form and the same form shown again (never put in a page)
    def hold_keys(self, section_id, keys):
        now = self.clock()
        self.held_keys = {k: v for k, v in self.held_keys.items() if now - v[0] < PENDING_SECONDS}
        while len(self.held_keys) >= PENDING_LIMIT:
            self.held_keys.pop(min(self.held_keys, key=lambda k: self.held_keys[k][0]))
        nonce = secrets.token_urlsafe(18)
        self.held_keys[nonce] = (now, section_id, dict(keys))
        return nonce

    def take_keys(self, nonce, section_id):
        held = self.held_keys.pop(nonce, None) if nonce else None
        if not held or held[1] != section_id or self.clock() - held[0] >= PENDING_SECONDS:
            return {}
        return held[2]

    def drop_keys(self, section_id):
        self.held_keys = {k: v for k, v in self.held_keys.items() if v[1] != section_id}

    # held changes
    def hold(self, section_id, values):
        now = self.clock()
        self.pending = {k: v for k, v in self.pending.items() if now - v[0] < PENDING_SECONDS}
        while len(self.pending) >= PENDING_LIMIT:
            self.pending.pop(min(self.pending, key=lambda k: self.pending[k][0]))
        nonce = secrets.token_urlsafe(18)
        self.pending[nonce] = (now, section_id, values, self.backend.status().revision)
        return nonce

    def take(self, nonce):
        held = self.pending.pop(nonce, None)
        if not held or self.clock() - held[0] >= PENDING_SECONDS:
            return None
        return held[1:]
