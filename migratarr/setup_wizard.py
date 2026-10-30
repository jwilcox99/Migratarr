"""First-run setup for migratarr_ui.py (plan phase 5).

When the settings backend reports no configuration, the UI starts in setup
mode: every page redirects here and every other action is refused until
setup is saved.

    GET  /setup               the first step not yet done
    GET  /setup/<step>        services, topology, disks, streaming, review
    POST /setup/<step>        action=detect | back | next (review: action=save)
    GET  /setup/done          health checks, then "make a first plan" (nothing moves)

Steps are settings sections (settings_contract), so the wizard and the
settings pages share fields, validation and parsing; only the source of
values differs (an in-memory draft here, the files there). Nothing is
written until the review step's Save, which hands the whole draft to the
backend's create() (atomic, owner-only secrets file). Pasted keys live only
in this process's memory until then and are never put back into a page: a
step shown again (a problem, Back, Detect) says the key is kept, and leaving
the field blank keeps it. Save, or a restart, drops them.
Standard library only.
"""
from migratarr import settings_contract as sc
from migratarr.ui import e, page
from migratarr.settings_ui import change_text, parse_form, render_checks, render_fields

STEPS = (sc.SERVICES, sc.TOPOLOGY, sc.DISKS, sc.STREAMING)
REVIEW, DONE = 'review', 'done'
TITLES = {sc.SERVICES: 'Connect your services', sc.TOPOLOGY: 'Where your disks are',
          sc.DISKS: 'Disks and categories', sc.STREAMING: 'Streaming services', REVIEW: 'Review and save'}
INTRO = {
    sc.SERVICES: 'Radarr, Sonarr, Jellyfin and TMDB. First say whether Radarr and Sonarr run in Docker or are '
                 'installed directly on this machine. "Detect" then looks for their config.xml (key and URL '
                 'base): in the Docker containers named below, or on this machine. You can paste a key instead.',
    sc.TOPOLOGY: 'Moves run either on a NAS over SSH, or on disks attached to this machine.',
    sc.DISKS: '"Detect" lists mounted disks and the root folders Radarr and Sonarr use, and suggests '
              'which folder holds each category.',
    sc.STREAMING: 'Which streaming services you subscribe to, and your TMDB region.',
}


def review_rows(fields, values):
    """(label, text) for what the user actually set: blank, unticked and "add a disk" fields are left
    out, choices show their labels, and each label carries its group so repeated names stay clear."""
    rows = []
    for f in fields:
        value = values.get(f.path)
        if f.kind == sc.SECRET:
            if value in (None, ''):
                continue
        elif value in (None, '', []) or (f.kind == sc.BOOL and not value and f.path.endswith('.remove')):
            continue
        if f.path.startswith('disks._new.'):
            continue
        if f.kind == sc.CHOICE:
            value = dict(f.choices).get(value, value)
        elif f.kind == sc.MULTI:
            names = dict(f.choices)
            value = [names.get(v, v) for v in value]
        text = change_text(sc.Change(f.path, f.label, None, value, f.kind == sc.SECRET), 'after')
        rows.append(((f.group + ': ' if f.group else '') + f.label, text))
    return rows


def index_of(step):
    return (STEPS + (REVIEW,)).index(step)


class SetupWizard:
    css = ''

    def __init__(self, backend, health=None, on_created=lambda result: None):
        self.backend, self.health, self.on_created = backend, health, on_created
        self.draft, self.done, self.notes = {}, set(), {}

    def active(self):
        return not self.backend.status().configured

    def next_step(self):
        for step in STEPS:
            if step not in self.done:
                return step
        return REVIEW

    def values(self, step):
        if step not in self.draft:
            self.draft[step] = dict(self.backend.defaults(step))
        normalize = getattr(self.backend, 'normalize', None)
        if normalize:  # e.g. the disks step follows the topology chosen earlier
            self.draft[step] = normalize(step, self.draft[step], dict(self.draft))[0]
        return self.draft[step]

    # ------------------------------------------------------------ pages

    def frame(self, step, inner, ctx):
        dots = ''.join(
            f'<li class="{"now" if s == step else "done" if s in self.done else ""}">'
            f'{e(TITLES[s])}</li>' for s in STEPS + (REVIEW,))
        body = (f'<p class="crumbs">Set up Migratarr: step {index_of(step) + 1} of {len(STEPS) + 1}</p>'
                f'<ol class="wizard-steps">{dots}</ol><h1>{e(TITLES[step])}</h1>{inner}')
        return page('Setup', body, ctx=ctx)

    def get(self, step, ctx):
        if not (ctx and ctx.get('actions')):
            return 200, page('Setup', '<h1>Migratarr is not set up yet</h1><p>Start the UI without '
                                      '<code>--read-only</code> to set it up from the browser.</p>')
        if step is None:
            return 'redirect', '/setup/' + self.next_step()
        if step == DONE:
            return 200, self.render_done(ctx)
        if not self.active():
            return 'redirect', '/settings'
        if step == REVIEW:
            return 200, self.render_review(ctx)
        if step not in STEPS:
            raise sc.SettingsError('There is no setup step called ' + step + '.')
        if index_of(step) > index_of(self.next_step()):
            return 'redirect', '/setup/' + self.next_step()
        return 200, self.render_step(step, self.values(step), (), ctx)

    def render_step(self, step, values, problems, ctx):
        fields = self.backend.fields(step, values)
        notes = ''.join(f'<li>{e(n)}</li>' for n in self.notes.get(step, ()))
        found = f'<ul class="found">{notes}</ul>' if notes else ''
        can = getattr(self.backend, 'can_discover', None)
        detect = ('<button type="submit" name="action" value="detect" class="btn" formnovalidate>Detect</button>'
                  if can is None or can(step) else '')
        back = ('<button type="submit" name="action" value="back" class="btn" formnovalidate>Back</button>'
                if index_of(step) else '')
        intro = getattr(self.backend, 'intro', lambda step: None)(step) or INTRO.get(step, '')
        inner = (f'<p class="meta">{e(intro)}</p>{found}'
                 f'<form method="post" action="/setup/{e(step)}" class="settings">'
                 f'<input type="hidden" name="token" value="{e(ctx["token"])}">'
                 f'{render_fields(fields, values, problems)}<div class="actions">{back}'
                 f'{detect}'
                 f'<button type="submit" name="action" value="next" class="btn primary">Next</button>'
                 f'</div></form>')
        return self.frame(step, inner, ctx)

    def render_review(self, ctx, problems=()):
        parts = []
        for step in STEPS:
            values = self.draft.get(step, {})
            rows = ''.join(f'<tr><td>{e(label)}</td><td>{e(text)}</td></tr>'
                           for label, text in review_rows(self.backend.fields(step, values), values))
            parts.append(f'<h2>{e(TITLES[step])} <a class="hint" href="/setup/{e(step)}">Change</a></h2>'
                         f'<table class="diff"><tbody>{rows}</tbody></table>')
        checks = self.health.run_checks(self.draft) if self.health else []
        failing = [c for c in checks if c.status == sc.FAIL]
        warn = ('<div class="alert"><p>Some checks failed. You can still save and fix them later in '
                'Settings, but Migratarr will not plan until they pass.</p></div>' if failing else '')
        caps = self.backend.capabilities()
        mode = self.draft.get(sc.TOPOLOGY, {}).get('topology.mode')
        if mode in getattr(caps, 'experimental', ()):
            warn += ('<div class="alert"><p>Moving files on this machine\'s own disks is experimental: it has '
                     'not yet been run end to end against a real Radarr and Sonarr. Every move still needs '
                     'your approval first.</p></div>')
        errs = ''.join(f'<p>{e(p.message)}</p>' for p in problems)
        errs = f'<div class="alert">{errs}</div>' if errs else ''
        secret_note = self.backend.capabilities().secret_file
        secret_note = (f'<p class="hint">Pasted keys are saved to {e(secret_note)}, readable by your user only.</p>'
                       if secret_note else '')
        inner = (f'{errs}<p class="meta">Nothing has been written yet.</p>{"".join(parts)}{secret_note}'
                 f'<h2>Checks</h2>{render_checks(checks)}{warn}'
                 f'<form method="post" action="/setup/review" class="settings">'
                 f'<input type="hidden" name="token" value="{e(ctx["token"])}"><div class="actions">'
                 f'<button type="submit" name="action" value="back" class="btn">Back</button>'
                 f'<button type="submit" name="action" value="save" class="btn primary">Save settings</button>'
                 f'</div></form>')
        return self.frame(REVIEW, inner, ctx)

    def render_done(self, ctx):
        checks = self.health.run_checks() if self.health else []
        ready = not any(c.status == sc.FAIL for c in checks)
        plan = ('<form method="post" action="/plan" class="inline">'
                f'<input type="hidden" name="token" value="{e(ctx["token"])}">'
                '<button type="submit" class="btn primary">Make a first plan</button></form>'
                '<p class="hint">Planning only reads your library and writes a plan. Nothing moves until you '
                'approve and run moves yourself.</p>' if ready else
                '<p>Fix the failed checks in <a href="/settings">Settings</a>, then make a first plan.</p>')
        body = (f'<h1>Migratarr is set up</h1><p class="meta">Your settings are saved.</p>'
                f'<h2>Checks</h2>{render_checks(checks)}{plan}')
        return page('Set up', body, ctx=ctx)

    # ------------------------------------------------------------ actions

    def post(self, step, form_data, ctx):
        if not self.active():
            raise sc.SettingsError('Migratarr is already set up. Change settings from the Settings page.')
        action = (form_data.get('action') or ['next'])[0]
        if step == REVIEW:
            if action == 'back':
                return 'redirect', '/setup/' + STEPS[-1]
            return self.create(ctx)
        if step not in STEPS:
            raise sc.SettingsError('There is no setup step called ' + str(step) + '.')
        current = self.values(step)
        values, problems = parse_form(self.backend.fields(step, current), form_data, current)
        notes = []
        normalize = getattr(self.backend, 'normalize', None)
        if normalize and not sc.blocking(problems):
            values, notes = normalize(step, values, dict(self.draft))
        self.draft[step] = values
        if notes and action == 'next':  # e.g. a disk was added: show it before moving on
            self.done.discard(step)
            return 200 if not sc.blocking(notes) else 400, self.render_step(step, values, notes, ctx)
        if action == 'back':
            return 'redirect', '/setup/' + STEPS[max(index_of(step) - 1, 0)]
        if action == 'detect':
            found = self.backend.discover(step, dict(self.draft))
            for path, value in found.values.items():
                if values.get(path) in (None, '', [], sc.SECRET_KEPT) or path not in values:
                    values[path] = value
            self.notes[step] = tuple(found.notes) or ('Nothing new was found.',)
            return 200, self.render_step(step, values, (), ctx)
        if not sc.blocking(problems):
            problems = problems + self.backend.validate(step, values, dict(self.draft))
        if step == sc.TOPOLOGY:
            chosen = values.get('topology.mode')
            if chosen and chosen not in self.backend.capabilities().topologies:
                problems.append(sc.Problem('topology.mode', 'This version cannot move files that way yet.'))
        if sc.blocking(problems):
            self.done.discard(step)
            return 400, self.render_step(step, values, problems, ctx)
        self.done.add(step)
        # A changed earlier step can invalidate later ones (e.g. topology -> disks): re-check them on the way.
        for later in STEPS[index_of(step) + 1:]:
            if later in self.done and sc.blocking(self.backend.validate(later, self.draft[later], dict(self.draft))):
                self.done.discard(later)
        return 'redirect', '/setup/' + self.next_step()

    def create(self, ctx):
        missing = [TITLES[s] for s in STEPS if s not in self.done]
        if missing:
            return 400, self.render_review(ctx, [sc.Problem(REVIEW, 'Finish these steps first: '
                                                            + ', '.join(missing) + '.')])
        problems = [p for s in STEPS for p in sc.blocking(self.backend.validate(s, self.draft[s], dict(self.draft)))]
        if problems:
            return 400, self.render_review(ctx, problems)
        result = self.backend.create({s: dict(self.draft[s]) for s in STEPS})
        self.draft, self.done, self.notes = {}, set(), {}
        self.on_created(result)
        return 'redirect', '/setup/done'
