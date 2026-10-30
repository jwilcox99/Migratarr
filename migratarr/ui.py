#!/usr/bin/env python3
"""Web UI for Migratarr: plan a run, confirm (approve) moves, run them, and watch.

    migratarr ui [--host 127.0.0.1] [--port 8765] [--read-only]

By default it listens on 127.0.0.1 only; from another computer, use an SSH tunnel
(ssh -L 8765:localhost:8765 <user>@<media host>) and browse to http://localhost:8765.
--host 0.0.0.0 (or $MIGRATARR_HOST, as in Docker) makes it reachable on your
network: then every page needs a sign-in, and the first visit creates the login
(migratarr/auth.py). Don't expose it to the internet.

Pages (GET) come from read_api.py (docs/dev/ui-data-contract.md) plus the disks'
free space, and never write, rename or lock anything; they never open
execution_logs/executor.lock. The actions (POST, see docs/ui-actions.md) only:
start a planning job, record approvals through approval_store.py (the same
hash-bound records as approve_execution.py), and start or stop a move job
(migratarr_jobs.py), which runs the existing executors one at a time. Every
form carries a per-process token. --read-only turns every action off.
Standard library only.
"""
import argparse
import errno
import hmac
import html
import json
import re
import secrets
import shutil
import sys
from urllib.parse import parse_qs, urlencode
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from migratarr import approval_store
from migratarr.executor_command import plain_reason
from migratarr import jobs
from migratarr import read_api

LOOPBACK_NAMES = {'localhost', '127.0.0.1', '[::1]', '::1'}
GiB = 1024 ** 3
SKIPPED_DETAILS = {'utc', 'execution_id', 'event', 'inventory', 'receipt', 'episode_files',
                   'episode_associations', 'source_inventory', 'manifest_sha256'}

EXECUTION_LABELS = {
    read_api.SUCCEEDED: 'Moved', read_api.UNFINISHED: 'In progress or interrupted',
    read_api.NEEDS_RECONCILIATION: 'Needs attention', read_api.JOURNAL_UNREADABLE: 'Journal unreadable',
    read_api.RECONCILED: 'Reconciled by hand',
    read_api.STOPPED_SAFE: 'Stopped before changing anything', read_api.CHECKED: 'Checked',
    read_api.NOT_STARTED: 'Not started',
}
APPROVAL_LABELS = {read_api.APPROVED: 'Approved', read_api.REVOKED: 'Revoked',
                   read_api.UNAPPROVED: 'Not approved'}
# Sections of the run page, in the order someone acting on the run cares about them.
GROUPS = (
    ('running', 'In progress',
     'A journal ends without a final event: the move is running now, or was interrupted.'),
    ('attention', 'Needs attention',
     'Media or Arr state may have changed. Check the journal before retrying anything.'),
    ('waiting', 'Approved, waiting to run', ''),
    ('unapproved', 'Not approved', 'Nothing runs until these are approved.'),
    ('done', 'Moved', ''),
)
CODE_TEXT = {
    'INSUFFICIENT_DESTINATION_SPACE': 'Not enough free space on the destination disk',
    'INSUFFICIENT_PROJECTED_SPACE': 'Not enough space once earlier moves land',
    'CUMULATIVE_DESTINATION_SPACE': 'Destination would drop below its reserve',
    'NO_ELIGIBLE_DESTINATION': 'No disk is set up for this category',
    'SOURCE_MISSING': 'Source folder not found',
    'DESTINATION_ROOT_MISSING': 'Destination category folder not found',
    'DESTINATION_COLLISION': 'Something already exists at the destination',
    'SOURCE_CATEGORY_UNKNOWN': 'Current category not recognized',
    'CONFLICTING_MANUAL_OVERRIDES': 'Conflicting override tags',
    'ARCHIVE_MOVE_REVIEW': 'Archive move: confirm before approving',
    'RARE_PROMOTION_REVIEW': 'Promotion to Rare: confirm before approving',
}
EVENT_TEXT = {
    'START': 'Started', 'PREFLIGHT_OK': 'All checks passed', 'CHECK_ONLY': 'Check-only run finished',
    'RENAME_INTENT': 'Rename starting', 'RENAMED': 'Renamed on the NAS',
    'NFS_VISIBILITY_WAIT': 'Waiting for the NFS view', 'NFS_VISIBILITY_READY': 'NFS view caught up',
    'NFS_RECOVERY_VERIFIED': 'NFS recovery verified',
    'COPY_INTENT': 'Copy starting', 'COPIED': 'Copy published and verified',
    'RADARR_UPDATE_INTENT': 'Updating the Radarr path', 'SONARR_UPDATE_INTENT': 'Updating the Sonarr path',
    'DELETE_INTENT': 'Removing the old copy', 'SOURCE_REMOVED': 'Old copy removed',
    'RECOVERY_STARTED': 'Recovery started', 'SUCCESS': 'Done', 'STOPPED': 'Stopped',
}
CATEGORIES = {'Common', 'Current', 'Library', 'Rare', 'Archive', 'HOLD'}


def e(value):
    return html.escape('' if value is None else str(value), quote=True)


def code_text(code):
    return CODE_TEXT.get(code, code.replace('_', ' ').capitalize())


def local_time(utc):
    try:
        return datetime.fromisoformat(str(utc)).astimezone().strftime('%b %d, %H:%M')
    except (TypeError, ValueError):
        return utc or ''


def size_text(gb):
    gb = float(gb or 0)
    return f'{gb / 1024:.2f} TB' if abs(gb) >= 1000 else f'{gb:.1f} GB'


def number(value):
    try:
        return float(value or 0)
    except ValueError:
        return 0.0


# ---------------------------------------------------------------- data (pure)

def runs_view(base):
    runs = []
    for run in read_api.list_runs(base):
        item = dict(run, totals={}, rows=0)
        if run['integrity_ok'] and run['manifest_present']:
            try:
                status = read_api.run_status(base, run['run_id'])
                item.update(totals=status['totals'], rows=len(status['rows']))
            except (read_api.ReadError, OSError, ValueError) as exc:
                item['integrity_problems'] = item['integrity_problems'] + [str(exc)]
        runs.append(item)
    return runs


def group_of(row):
    state = row['execution']
    if state == read_api.UNFINISHED:
        return 'running'
    if state in (read_api.NEEDS_RECONCILIATION, read_api.JOURNAL_UNREADABLE):
        return 'attention'
    if state in (read_api.SUCCEEDED, read_api.RECONCILED):
        return 'done'
    return 'waiting' if row['approval'] == read_api.APPROVED else 'unapproved'


def disk_view(rows, storage, reserves, usage=shutil.disk_usage):
    """Per disk: total, free now, and free after this run's moves that haven't finished."""
    change = {disk: 0.0 for disk in storage}
    for row in rows:
        if row['transfer_type'] != 'CROSS_DISK_TRANSFER' or row['execution'] == read_api.SUCCEEDED:
            continue
        size = number(row['size_gb'])
        if row.get('source_disk') in change:
            change[row['source_disk']] += size
        if row.get('target_disk') in change:
            change[row['target_disk']] -= size
    disks = []
    for disk, paths in sorted(storage.items()):
        try:
            total, used, free = usage(paths['local_path'])
        except OSError as exc:
            disks.append(dict(disk=disk, error=exc.strerror or str(exc)))
            continue
        disks.append(dict(disk=disk, total_gb=total / GiB, free_gb=free / GiB,
                          projected_free_gb=free / GiB + change[disk], change_gb=change[disk],
                          reserve_gb=reserves.get(disk)))
    return disks


def run_view(base, run_id, storage=None, reserves=None, usage=shutil.disk_usage):
    status = read_api.run_status(base, run_id)
    plan = read_api.get_plan(base, run_id)
    if not status['integrity_ok'] or not plan['integrity_ok']:
        return dict(run_id=run_id, integrity_ok=False,
                    integrity_problems=status['integrity_problems'] + plan['integrity_problems'])
    details = {r['execution_id']: r for r in read_api.get_manifest(base, run_id)['rows']}
    rows = []
    for row in status['rows']:
        detail = details.get(row['execution_id'], {})
        rows.append(dict(row, source_disk=detail.get('source_disk'), target_disk=detail.get('target_disk'),
                         source_path=detail.get('source_path'),
                         decision_reason=detail.get('decision_reason'),
                         warning_codes=[c for c in (detail.get('warnings') or '').split(';') if c],
                         group=None))
        rows[-1]['group'] = group_of(rows[-1])
    try:
        eligible, _ = approval_store.eligible_rows(base, run_id)
    except approval_store.ApprovalError:
        eligible = {}
    for row in rows:
        row['eligible'] = row['group'] == 'unapproved' and row['execution_id'] in eligible
    meta = next((r for r in read_api.list_runs(base) if r['run_id'] == run_id), {})
    return dict(run_id=run_id, integrity_ok=True, manifest_hash=status['manifest_hash'], integrity_problems=[], created_utc=meta.get('created_utc'),
                git_commit=meta.get('git_commit'), rows=rows, totals=status['totals'],
                blocked=[r for r in plan['rows'] if r.get('status') == 'BLOCKED'],
                disks=disk_view(rows, storage or {}, reserves or {}, usage),
                running=any(r['group'] == 'running' for r in rows))


def execution_view(base, execution_id):
    journal = read_api.events(base, execution_id)
    run_id = read_api.EXECUTION_ID.fullmatch(execution_id).group(1)
    manifest = read_api.get_manifest(base, run_id)
    row = next((r for r in manifest['rows'] if r['execution_id'] == execution_id), None)
    state = read_api.execution_state(base, execution_id, manifest['manifest_hash'])
    return dict(execution_id=execution_id, run_id=run_id, row=row, state=state, events=journal,
                running=state['state'] == read_api.UNFINISHED, manifest_hash=manifest['manifest_hash'])


def review_view(base, run_id, ids, pick_all, storage=None, reserves=None, usage=shutil.disk_usage,
                permissions=jobs.permission_problems):
    """What approving the selected rows would record. Reads only."""
    view = run_view(base, run_id, storage, reserves, usage)
    if not view['integrity_ok']:
        raise read_api.ReadError('Run failed its integrity check')
    eligible = [r for r in view['rows'] if r.get('eligible')]
    wanted = set(ids)
    rows = eligible if pick_all else [r for r in eligible if r['execution_id'] in wanted]
    for row in rows:
        row['permission_problems'] = permissions(row.get('source_path'))
    return dict(run_id=run_id, manifest_hash=view['manifest_hash'], rows=rows,
                total_gb=sum(number(r['size_gb']) for r in rows),
                not_eligible=sorted(wanted - {r['execution_id'] for r in eligible}) if not pick_all else [],
                disks=disk_view(rows, storage or {}, reserves or {}, usage))


def confirm_run_view(base, run_id, storage=None, reserves=None, usage=shutil.disk_usage,
                     permissions=jobs.permission_problems):
    """What a move job for this run would do right now. Reads only."""
    queue, held, manifest_hash = jobs.move_queue(base, run_id)
    details = {r['execution_id']: r for r in read_api.get_manifest(base, run_id)['rows']}
    for row in queue + held:
        row['source_disk'] = details[row['execution_id']].get('source_disk')
        row['target_disk'] = details[row['execution_id']].get('target_disk')
    for row in queue:
        row['permission_problems'] = permissions(row.get('source_path'))
    runnable = [r for r in queue if not r['permission_problems']]
    return dict(run_id=run_id, manifest_hash=manifest_hash, queue=queue, held=held,
                total_gb=sum(number(r['size_gb']) for r in runnable),
                disks=disk_view(runnable, storage or {}, reserves or {}, usage))


# ---------------------------------------------------------------- HTML

MARK = ('<svg class="mark" viewBox="0 0 160 160" aria-hidden="true">'
        '<rect width="160" height="160" rx="36" fill="#1B2B42"/>'
        '<rect x="34" y="36" width="76" height="16" rx="8" fill="#4A5C78"/>'
        '<rect x="50" y="60" width="80" height="16" rx="8" fill="#2DD4BF"/>'
        '<rect x="34" y="84" width="76" height="16" rx="8" fill="#4A5C78"/>'
        '<rect x="34" y="108" width="76" height="16" rx="8" fill="#4A5C78"/></svg>')


def page(title, body, refresh=False, ctx=None, seconds=30):
    """A full page. ``ctx`` (actions, token, job, notice) is None for the read-only view."""
    ctx = ctx or {}
    job = ctx.get('job')
    if job and job['state'] in (jobs.STARTING, jobs.RUNNING):
        refresh = True
    meta = f'<meta http-equiv="refresh" content="{seconds}">' if refresh else ''
    note = (f'<p class="live">Something is in progress, so this page reloads every {seconds} seconds.</p>'
            if refresh else '')
    if job and job['state'] in (jobs.STARTING, jobs.RUNNING) and not ctx.get('on_job_page'):
        note = (f'<p class="live">{e(JOB_BANNER.get(job["kind"], "A job is running"))}: {e(job.get("message"))}. '
                f'<a href="/job/{e(job["job_id"])}">Watch it</a></p>')
    elif job and job['state'] == jobs.INTERRUPTED and not ctx.get('on_job_page'):
        note += (f'<div class="alert"><p>The last job stopped unexpectedly. '
                 f'<a href="/job/{e(job["job_id"])}">Review it</a> before starting another.</p></div>')
    if ctx.get('notice'):
        note += f'<p class="notice">{e(ctx["notice"])}</p>'
    mode = '' if ctx.get('actions') else '<span class="readonly">Read-only view</span>'
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">{meta}'
            f'<title>{e(title)} | Migratarr</title><link rel="stylesheet" href="/static/app.css"></head>'
            f'<body><header class="top"><a class="home" href="/">{MARK}<span>Migratarr</span></a>'
            f'<nav class="nav"><a href="/settings">Settings &amp; health</a>{mode}</nav>'
            f'</header><main>{note}{body}</main></body></html>')


# ---------------------------------------------------------------- settings & health (read-only)

STATUS_TEXT = {'ok': 'OK', 'warn': 'Warning', 'fail': 'Failed', 'skip': 'Skipped'}


def settings_view(report=None, checks=None):
    """Effective settings for display: where each credential comes from, never its value."""
    from migratarr import services, settings
    report = report or settings.load()
    view = dict(report=report.as_dict(), services=[], disks=[], categories={}, planner=None,
                runtime=None, checks=None if checks is None else [c.as_dict() for c in checks])
    runtime, targets, planner = report.runtime, report.targets, report.planner
    if runtime is not None:
        view['runtime'] = dict(base_path=str(runtime.base_path), topology=runtime.topology,
                               schema_version=runtime.schema_version, nas=runtime.ssh_target,
                               nas_python=runtime.remote_python)
        credentials = settings.redacted_secrets(runtime)
        for descriptor in services.SERVICES.values():
            url = runtime.urls.get(descriptor.url_setting.split('.', 1)[1]) if descriptor.url_setting else None
            view['services'].append(dict(id=descriptor.id, label=descriptor.label, role=descriptor.role,
                                         url=url or descriptor.probe.base_url,
                                         credential=credentials.get(descriptor.id, '')))
    if targets is not None:
        for t in targets.targets:
            view['disks'].append(dict(id=t.id, name=t.name, path=str(t.path),
                                      remote_path=str(t.remote_path) if t.remote_path else None,
                                      enabled=t.enabled, media_types=list(t.media_types),
                                      reserve_gb=t.minimum_free_space_gb))
        for media, categories in targets.placement.items():
            view['categories'][media] = [dict(category=c, folder=str(targets.category_paths[media][c]),
                                              disks=list(ids)) for c, ids in categories.items()]
        view['arr_root'] = str(targets.arr_root)
    if planner is not None:
        view['planner'] = dict(region=planner.region, subscribed=sorted(planner.subscribed),
                               user_free_access=sorted(planner.user_free_access),
                               lock_tag=planner.overrides.lock_tag)
    return view


def render_settings(view, ctx=None, setup=False):
    report = view['report']
    parts = ['<h1>Settings &amp; health</h1>']
    if setup:
        parts.append('<div class="alert"><p>Migratarr can\'t start normally: its settings have problems. '
                     'Fix the files below, then restart <code>migratarr_ui.py</code>.</p></div>')
    parts.append('<p class="meta">Read-only. Change settings in the files listed here; '
                 '<code>migratarr doctor</code> runs the same checks from a terminal.</p>')
    errors = [p for p in report['problems'] if p['severity'] == 'error']
    warnings = [p for p in report['problems'] if p['severity'] == 'warning']
    if errors or warnings:
        items = ''.join(f'<li><span class="flag {"bad" if p["severity"] == "error" else "warn"}">'
                        f'{e(p["severity"])}</span> <code>{e(p["file"])}</code> <strong>{e(p["field"])}</strong>: '
                        f'{e(p["message"])}</li>' for p in errors + warnings)
        parts.append(f'<h2>Problems <span class="count">{len(errors)} errors, {len(warnings)} warnings</span></h2>'
                     f'<ul class="plain">{items}</ul>')
    parts.append(render_settings_files(view))
    checks = view.get('checks')
    if checks is None:
        parts.append('<h2>Health</h2><p class="hint">Checks contact each service, list Radarr/Sonarr root '
                     'folders and (NAS topology) connect to the NAS over SSH. They change nothing.</p>'
                     '<p><a class="btn" href="/settings?check=1">Run health checks</a></p>')
    else:
        rows = ''.join(f'<tr><td data-label="Status"><span class="flag {e(HEALTH_CLASS[c["status"]])}">'
                       f'{e(STATUS_TEXT[c["status"]])}</span></td><td data-label="Check">{e(c["label"])}</td>'
                       f'<td data-label="Detail">{e(c["detail"])}{fix_note(c)}</td></tr>' for c in checks)
        parts.append('<h2>Health</h2><table><thead><tr><th>Status</th><th>Check</th><th>Detail</th></tr></thead>'
                     f'<tbody>{rows}</tbody></table><p><a class="btn" href="/settings?check=1">Run again</a></p>')
    parts.append(render_settings_effective(view))
    return page('Settings & health', ''.join(parts), ctx=ctx)


def render_settings_files(view):
    report = view['report']
    missing = ' <span class="flag bad">missing</span>'
    files = ''.join(f'<li><code>{e(name)}</code> {e(path)}{missing if name in report["missing"] else ""}</li>'
                    for name, path in report['files'].items())
    return f'<h2>Files</h2><ul class="plain">{files}</ul>'


def render_settings_details(view):
    """Effective settings and file locations, under the editable sections of the Settings page."""
    return ('<h2>Effective settings</h2><p class="hint">What Migratarr reads now. Credentials show where '
            'each key comes from, never the key.</p>' + render_settings_effective(view) + render_settings_files(view))


def render_settings_effective(view):
    parts = []
    runtime = view.get('runtime')
    if runtime:
        nas = (f'<li>NAS <code>{e(runtime["nas"])}</code>, Python <code>{e(runtime["nas_python"])}</code></li>'
               if runtime['nas'] else '')
        parts.append(f'<h2>Host</h2><ul class="plain"><li>Topology <strong>{e(runtime["topology"])}</strong> '
                     f'(runtime.json version {runtime["schema_version"]})</li>'
                     f'<li>Base path <code>{e(runtime["base_path"])}</code></li>{nas}</ul>')
    if view['services']:
        rows = ''.join(f'<tr><td data-label="Service">{e(x["label"])}</td><td data-label="URL"><code>'
                       f'{e(x["url"] or "")}</code></td><td data-label="Key from">{e(x["credential"])}</td></tr>'
                       for x in view['services'])
        parts.append('<h2>Services</h2><table><thead><tr><th>Service</th><th>URL</th><th>Key from</th></tr>'
                     f'</thead><tbody>{rows}</tbody></table>')
    if view['disks']:
        rows = ''.join(f'<tr><td data-label="Disk">{e(d["id"])}<div class="sub">{e(d["name"])}</div></td>'
                       f'<td data-label="Path"><code>{e(d["path"])}</code></td>'
                       f'<td data-label="On NAS"><code>{e(d["remote_path"] or "")}</code></td>'
                       f'<td data-label="Holds">{e(", ".join(d["media_types"]))}'
                       f'{"" if d["enabled"] else " (not a destination)"}</td>'
                       f'<td data-label="Reserve" class="num">{d["reserve_gb"]} GB</td></tr>' for d in view['disks'])
        parts.append('<h2>Disks</h2><table><thead><tr><th>Disk</th><th>Path</th><th>On NAS</th><th>Holds</th>'
                     f'<th class="num">Reserve</th></tr></thead><tbody>{rows}</tbody></table>')
        for media, categories in view['categories'].items():
            rows = ''.join(f'<tr><td data-label="Category">{pill(c["category"])}</td>'
                           f'<td data-label="Folder"><code>{e(c["folder"])}</code></td>'
                           f'<td data-label="Disks">{e(", ".join(c["disks"]))}</td></tr>' for c in categories)
            parts.append(f'<h2>{e(media)} categories <span class="count">Arr root {e(view["arr_root"])}</span></h2>'
                         '<table><thead><tr><th>Category</th><th>Folder</th><th>Disks</th></tr></thead>'
                         f'<tbody>{rows}</tbody></table>')
    planner = view.get('planner')
    if planner:
        parts.append('<h2>Planner</h2><ul class="plain">'
                     f'<li>Streaming region <strong>{e(planner["region"])}</strong></li>'
                     f'<li>Subscribed: {e(", ".join(planner["subscribed"]) or "none")}</li>'
                     f'<li>Free to you: {e(", ".join(planner["user_free_access"]) or "none")}</li>'
                     f'<li>Lock tag <code>{e(planner["lock_tag"])}</code></li></ul>')
    return ''.join(parts)


HEALTH_CLASS = {'ok': 'ok', 'warn': 'warn', 'fail': 'bad', 'skip': ''}


def fix_note(check):
    if check['fix'] and check['status'] in ('warn', 'fail'):
        return '<div class="hint">' + e(check['fix']) + '</div>'
    return ''


JOB_KIND = {'plan': 'Planning', 'run': 'Moving'}
JOB_BANNER = {'plan': 'Planning is running', 'run': 'Moves are running'}


def form(action, ctx, fields=(), button='', cls='', extra=''):
    """A POST form carrying the per-process token. ``fields`` are (name, value) hidden inputs."""
    hidden = ''.join(f'<input type="hidden" name="{e(k)}" value="{e(v)}">'
                     for k, v in [('token', ctx['token']), *fields])
    label = f'<button type="submit" class="{e(cls or "btn")}">{e(button)}</button>' if button else ''
    return f'<form method="post" action="{e(action)}" class="inline">{hidden}{extra}{label}</form>'


def job_busy(ctx):
    job = (ctx or {}).get('job')
    return bool(job)


def pill(category):
    name = category if category in CATEGORIES else 'Other'
    return f'<span class="pill c-{e(name.lower())}">{e(category)}</span>'


def move_cell(current, recommended):
    return f'<span class="move">{pill(current)}<span class="to" aria-label="to">&#8594;</span>{pill(recommended)}</span>'


def disks_cell(row):
    if row.get('transfer_type') == 'SAME_DISK_RENAME':
        return f'{e(row.get("source_disk"))}, same disk'
    return f'{e(row.get("source_disk"))} to {e(row.get("target_disk"))}'


def bar_svg(low, high, kind, reserve_at, label):
    """Disk bar as SVG geometry: inline style attributes are blocked by the page's CSP."""
    w = 1000
    parts = [f'<svg class="bar" viewBox="0 0 {w} 22" preserveAspectRatio="none" role="img" aria-label="{e(label)}">',
             f'<rect class="track" width="{w}" height="22" rx="6"/>',
             f'<rect class="used" width="{low * w:.1f}" height="22"/>']
    if high - low > 0.0005:
        parts.append(f'<rect class="delta {kind}" x="{low * w:.1f}" width="{(high - low) * w:.1f}" height="22"/>')
    if reserve_at is not None:
        parts.append(f'<rect class="reserve" x="{reserve_at * w - 3:.1f}" width="6" height="22"/>')
    return ''.join(parts) + '</svg>'


def meter_svg(done):
    return (f'<svg class="meter" viewBox="0 0 120 6" preserveAspectRatio="none" aria-hidden="true">'
            f'<rect class="track" width="120" height="6" rx="3"/><rect class="fill" width="{done * 120:.1f}" '
            f'height="6" rx="3"/></svg>')


def render_plan_panel(ctx):
    if not (ctx or {}).get('actions'):
        return ''
    if job_busy(ctx):
        return ('<section class="panel"><h2>Plan a new run</h2><p class="hint">Available once the current job '
                'is finished.</p></section>')
    blocked = ctx.get('plan_blocked')
    why = (f'<div class="alert"><p>Planning is unavailable: {len(blocked)} health check'
           f'{"s" if len(blocked) != 1 else ""} failed the last time they ran '
           f'({e(", ".join(c.label for c in blocked[:3]))}{", ..." if len(blocked) > 3 else ""}). '
           '<a href="/settings?check=1">Fix them in Settings &amp; health</a>; warnings don\'t block planning.'
           '</p></div>' if blocked else '')
    return ('<section class="panel"><h2>Plan a new run</h2>' + why + '<p class="hint">Scores every movie and series, builds '
            'the move plan, snapshots it and writes a manifest (<code>migratarr_plan.py</code>). It reads '
            'Radarr, Sonarr, Jellyfin and TMDB and can take a while. Nothing is approved or moved.</p>'
            + form('/plan', ctx, button='Plan a new run', cls='btn primary') + '</section>')


def render_runs(runs, ctx=None):
    if not runs:
        return page('Runs', '<h1>Runs</h1>' + render_plan_panel(ctx) + '<p class="empty">No runs yet. Plan one '
                    + ('above.' if (ctx or {}).get('actions') else 'with <code>migratarr plan</code>.')
                    + '</p>', ctx=ctx)
    items = []
    for run in runs:
        totals = run['totals']
        moved = totals.get(read_api.SUCCEEDED, {}).get('count', 0)
        flags = []
        if not run['integrity_ok']:
            flags.append('<span class="flag bad">Integrity check failed</span>')
        elif not run['manifest_present']:
            flags.append('<span class="flag">No manifest</span>')
        for state in (read_api.UNFINISHED, read_api.NEEDS_RECONCILIATION, read_api.JOURNAL_UNREADABLE):
            if totals.get(state):
                flags.append(f'<span class="flag warn">{totals[state]["count"]} '
                             f'{e(EXECUTION_LABELS[state].lower())}</span>')
        progress = (meter_svg(moved / run["rows"]) + f'{moved} of {run["rows"]} moved' if run['rows'] else 'Nothing to move')
        items.append(
            f'<li><a href="/run/{e(run["run_id"])}"><span class="when">{e(local_time(run["created_utc"]))}</span>'
            f'<span class="rid">{e(run["run_id"])}</span>'
            f'<span class="counts">{run["counts"].get("planned_moves", "?")} planned</span>'
            f'<span class="progress">{progress}</span><span class="flags">{"".join(flags)}</span></a></li>')
    return page('Runs', '<h1>Runs</h1>' + render_plan_panel(ctx) + '<ol class="runs">' + ''.join(items) + '</ol>',
                ctx=ctx)


def render_disks(disks, hint="Free space now, and after this run's moves that haven't finished."):
    if not disks:
        return ''
    out = ['<section class="disks" aria-label="Disk space"><h2>Disk space</h2>',
           f'<p class="hint">{e(hint)}</p>',
           '<ul class="legend">' + ''.join(
               f'<li><svg viewBox="0 0 12 12" aria-hidden="true"><rect class="{cls}" width="12" height="12" '
               f'rx="2"/></svg>{label}</li>' for cls, label in (
                   ('used', 'Used now'), ('gain', 'Freed by remaining moves'),
                   ('loss', 'Taken by remaining moves'), ('reserve', 'Reserve'))) + '</ul>']
    for d in disks:
        if 'error' in d:
            out.append(f'<div class="disk"><div class="name">{e(d["disk"])}</div>'
                       f'<div class="error">Not readable: {e(d["error"])}</div></div>')
            continue
        total = d['total_gb'] or 1
        used_now = (total - d['free_gb']) / total
        used_after = min(max((total - d['projected_free_gb']) / total, 0), 1)
        low, high = sorted((used_now, used_after))
        kind = 'gain' if d['change_gb'] > 0 else 'loss'
        reserve_at = 1 - d['reserve_gb'] / total if d['reserve_gb'] else None
        under = d['free_gb'] < (d['reserve_gb'] or 0)
        change = ('No change' if abs(d['change_gb']) < 0.05 else
                  f'{size_text(d["projected_free_gb"])} free after ({"+" if d["change_gb"] > 0 else "−"}'
                  f'{size_text(abs(d["change_gb"]))})')
        out.append(
            f'<div class="disk{" under" if under else ""}"><div class="name">{e(d["disk"])}</div>'
            + bar_svg(low, high, kind, reserve_at, f'{d["disk"]}: {size_text(d["free_gb"])} free of '
                      f'{size_text(total)}; {change}') +
            f'<div class="figures"><span class="now">{size_text(d["free_gb"])} free of {size_text(total)}'
            f'{" (below reserve)" if under else ""}</span><span class="after">{e(change)}</span></div></div>')
    out.append('</section>')
    return ''.join(out)


def render_moves(rows, key, title, note, ctx=None, run_id=None):
    rows = [r for r in rows if r['group'] == key]
    if not rows:
        return ''
    actions = bool((ctx or {}).get('actions'))
    select = actions and key == 'unapproved' and any(r.get('eligible') for r in rows)
    revoke = actions and key == 'waiting'
    rows.sort(key=lambda r: (number(r['size_gb']), r['execution_id']))
    body = []
    for r in rows:
        pick = ''
        if select:
            pick = ('<td class="pick">' + (f'<input type="checkbox" name="id" value="{e(r["execution_id"])}" '
                                           f'aria-label="Select {e(r["title"])}">' if r.get('eligible') else '')
                    + '</td>')
        state = EXECUTION_LABELS.get(r['execution'], r['execution'])
        if r['recovered'] and r['execution'] == read_api.SUCCEEDED:
            state += ' (recovered)'
        when = f'<span class="sub">{e(EVENT_TEXT.get(r["last_event"], r["last_event"]))}, ' \
               f'{e(local_time(r["last_utc"]))}</span>' if r['last_event'] else ''
        detail = f'<span class="sub reason">{e(r["detail"])}</span>' if r['detail'] else ''
        notes = ''.join(f'<span class="note">{e(code_text(c))}</span>' for c in r['warning_codes'])
        reason = f'<span class="sub">{e(r["decision_reason"])}</span>' if r['decision_reason'] else ''
        approval = e(APPROVAL_LABELS.get(r["approval"], r["approval"]))
        if revoke and not job_busy(ctx):
            approval += form(f'/run/{run_id}/revoke', ctx, [('id', r['execution_id'])], 'Revoke', 'btn small')
        body.append(
            f'<tr>{pick}<td class="title"><a href="/execution/{e(r["execution_id"])}">{e(r["title"])}</a>'
            f'<span class="sub">{e(r["media_type"])}, {e(r["execution_id"].rsplit("-", 1)[1])}</span></td>'
            f'<td data-label="Move">{move_cell(r["current"], r["recommended"])}</td>'
            f'<td data-label="Disks">{disks_cell(r)}</td>'
            f'<td class="num" data-label="Size">{size_text(r["size_gb"])}</td>'
            f'<td data-label="Status"><span class="state s-{e(r["execution"].lower())}">{e(state)}</span>{when}{detail}</td>'
            f'<td data-label="Approval">{approval}</td>'
            f'<td data-label="Notes">{notes}{reason}</td></tr>')
    total = sum(number(r['size_gb']) for r in rows)
    table = ('<div class="scroll"><table><thead><tr>' + ('<th class="pick"><span class="sr">Select</span></th>'
                                                          if select else '') +
             '<th>Title</th><th>Move</th><th>Disks</th>'
             '<th class="num">Size</th><th>Status</th><th>Approval</th><th>Notes</th></tr></thead><tbody>'
             + ''.join(body) + '</tbody></table></div>')
    if select:
        count = sum(1 for r in rows if r.get('eligible'))
        buttons = ('<div class="actions"><button type="submit" class="btn primary" name="pick" value="selected">'
                   'Review selected</button><button type="submit" class="btn" name="pick" value="all">'
                   f'Review all {count}</button><span class="hint">Nothing is approved until you confirm on '
                   'the next page.</span></div>')
        table = form(f'/run/{run_id}/review', ctx, extra=table + buttons)
    return (f'<section class="group g-{key}" id="{key}"><h2>{e(title)} <span class="count">{len(rows)}, '
            f'{size_text(total)}</span></h2>' + (f'<p class="hint">{e(note)}</p>' if note else '') +
            table + '</section>')


def render_blocked(blocked):
    if not blocked:
        return ''
    body = ''.join(
        f'<tr><td class="title">{e(r.get("title"))}<span class="sub">{e(r.get("media_type"))}</span></td>'
        f'<td data-label="Move">{move_cell(r.get("current"), r.get("recommended"))}</td>'
        f'<td data-label="Disks">{disks_cell(r)}</td>'
        f'<td class="num" data-label="Size">{size_text(r.get("size_gb"))}</td>'
        f'<td data-label="Why">{"".join(f"<span class=note>{e(code_text(c))}</span>" for c in r["blocker_codes"])}</td></tr>'
        for r in sorted(blocked, key=lambda r: r.get('title') or ''))
    return (f'<section class="group g-blocked"><h2>Blocked by the planner <span class="count">{len(blocked)}'
            '</span></h2><p class="hint">These never reach the manifest. A later run plans them again.</p>'
            '<div class="scroll"><table><thead><tr><th>Title</th><th>Move</th><th>Disks</th>'
            '<th class="num">Size</th><th>Why</th></tr></thead><tbody>' + body + '</tbody></table></div></section>')


def render_steps(view, ctx):
    """Plan -> Confirm -> Run, with what's waiting at each step and the next action."""
    if not (ctx or {}).get('actions'):
        return ''
    waiting = [r for r in view['rows'] if r['group'] == 'unapproved' and r.get('eligible')]
    ready = [r for r in view['rows'] if r['group'] == 'waiting']
    moved = [r for r in view['rows'] if r['group'] == 'done']
    run_button = ''
    if ready and not job_busy(ctx):
        run_button = (f'<a class="btn primary" href="/run/{e(view["run_id"])}/confirm-run">Run '
                      f'{len(ready)} approved move{"s" if len(ready) != 1 else ""}</a>')
    elif ready:
        run_button = '<span class="hint">Available when the current job is finished.</span>'
    return ('<ol class="steps">'
            f'<li class="done"><span class="n">1</span><span><b>Plan</b><span class="sub">Run planned '
            f'{e(local_time(view["created_utc"]))}</span></span></li>'
            f'<li class="{"now" if waiting else "done"}"><span class="n">2</span><span><b>Confirm</b>'
            f'<span class="sub">{len(waiting)} to review'
            + (f' (<a href="#unapproved">review</a>)' if waiting else '') + '</span></span></li>'
            f'<li class="{"now" if ready else ("done" if moved else "")}"><span class="n">3</span><span><b>Run</b>'
            f'<span class="sub">{len(ready)} approved, {len(moved)} moved</span></span>{run_button}</li></ol>')


def render_run(view, ctx=None):
    if not view['integrity_ok']:
        return page(view['run_id'], f'<h1>Run {e(view["run_id"])}</h1><div class="alert"><p>This run failed its '
                    'integrity check, so nothing from it is shown.</p><ul>' +
                    ''.join(f'<li>{e(p)}</li>' for p in view['integrity_problems']) + '</ul></div>', ctx=ctx)
    commit = (view['git_commit'] or '')[:7]
    head = (f'<p class="crumbs"><a href="/">Runs</a></p><h1>Run of {e(local_time(view["created_utc"]))}</h1>'
            f'<p class="meta">{e(view["run_id"])}, ' + (f'code {e(commit)}, ' if commit else '')
            + f'{len(view["rows"])} moves in the manifest, '
            f'{len(view["blocked"])} blocked</p>')
    sections = ''.join(render_moves(view['rows'], *group, ctx=ctx, run_id=view['run_id']) for group in GROUPS)
    if not view['rows']:
        sections = '<p class="empty">The planner found nothing eligible to move in this run.</p>'
    return page('Run ' + view['run_id'], head + render_steps(view, ctx) + render_disks(view['disks']) + sections +
                render_blocked(view['blocked']), refresh=view['running'], ctx=ctx)


def render_execution(view, ctx=None):
    row = view['row'] or {}
    state = view['state']
    head = (f'<p class="crumbs"><a href="/">Runs</a> / <a href="/run/{e(view["run_id"])}">{e(view["run_id"])}</a>'
            f'</p><h1>{e(row.get("title", view["execution_id"]))}</h1>'
            f'<p class="meta">{e(view["execution_id"])}</p>')
    if row:
        head += (f'<div class="summary">{move_cell(row.get("current"), row.get("recommended"))}'
                 f'<span>{disks_cell(row)}</span><span>{size_text(row.get("size_gb"))}</span>'
                 f'<span class="state s-{e(state["state"].lower())}">'
                 f'{e(EXECUTION_LABELS.get(state["state"], state["state"]))}</span></div>')
    if state.get('detail'):
        head += f'<p class="reason">{e(plain_reason(state["detail"]))}</p>'
    head += render_reconcile(view, ctx)
    if not view['events']:
        return page(view['execution_id'], head + '<p class="empty">This move has not been started yet.</p>',
                    ctx=ctx)
    items, previous = [], None
    for event in view['events']:
        try:
            moment = datetime.fromisoformat(event.get('utc', ''))
        except ValueError:
            moment = None
        gap = ''
        if moment and previous:
            seconds = int((moment - previous).total_seconds())
            if seconds >= 60:
                gap = (f'<span class="gap">{seconds // 3600}h {seconds % 3600 // 60}m later</span>'
                       if seconds >= 3600 else f'<span class="gap">{seconds // 60}m later</span>')
        previous = moment or previous
        name = event['event']
        label = EVENT_TEXT.get(name, name)
        if name == 'START':
            label += ' (live)' if event.get('live') else ' (check only)'
        extra = {k: v for k, v in event.items() if k not in SKIPPED_DETAILS}
        reason = plain_reason(extra.pop('reason', None))  # older journals may hold a traceback
        facts = ''.join(f'<span class="fact">{e(k.replace("_", " "))}: {e(v if not isinstance(v, (dict, list)) else json.dumps(v))}</span>'
                        for k, v in extra.items() if k != 'live')
        why = ''
        if reason:
            short = reason if len(reason) <= 200 else reason[:199] + '…'
            why = (f'<details><summary>{e(short)}</summary><pre>{e(reason)}</pre></details>'
                   if len(reason) > 200 else f'<p class="reason">{e(reason)}</p>')
        items.append(f'<li class="ev ev-{e(name.lower())}"><span class="t">{e(local_time(event.get("utc")))}</span>'
                     f'<span class="what">{e(label)} <code>{e(name)}</code></span>{gap}{facts}{why}</li>')
    return page(view['execution_id'], head + '<h2>Journal</h2><ol class="timeline">' + ''.join(items) + '</ol>',
                refresh=view['running'], ctx=ctx)


def render_reconcile(view, ctx):
    """The "mark reconciled" form, for moves that need attention (reconcile_store.py)."""
    from migratarr import reconcile_store
    state = view['state']
    if state['state'] == read_api.RECONCILED:
        r = state.get('reconciled') or {}
        return (f'<p class="notice">Marked reconciled ({e(r.get("outcome"))}) on {e(local_time(r.get("utc")))}; '
                f'it was {e(EXECUTION_LABELS.get(r.get("state_before"), r.get("state_before")))}. It won\'t run again '
                'from this plan; the next plan looks at it afresh.</p>')
    if not (ctx and ctx.get('actions')) or state['state'] not in reconcile_store.RECONCILABLE:
        return ''
    if ctx.get('job'):
        return ''
    options = ''.join(f'<label class="opt"><input type="radio" name="outcome" value="{e(k)}" required> {e(v)}</label>'
                      for k, v in reconcile_store.OUTCOMES.items())
    extra = (f'<fieldset><legend>What did you find?</legend>{options}</fieldset>'
             '<label for="note">What you checked (kept with the journal)</label>'
             '<textarea id="note" name="note" rows="3" maxlength="2000" required></textarea>'
             f'<input type="hidden" name="manifest_hash" value="{e(view.get("manifest_hash", ""))}">')
    return ('<details class="reconcile"><summary>Mark reconciled</summary><p class="hint">Use this after checking '
            'the media and Radarr/Sonarr by hand. It changes nothing on disk or in Radarr/Sonarr; it records '
            'what you found, so this move stops being flagged. A later attempt undoes it.</p>'
            + form(f'/execution/{view["execution_id"]}/reconcile', ctx, button='Record', extra=extra)
            + '</details>')


def problem_notes(row):
    return ''.join(f'<span class="note bad">{e(p)}</span>' for p in row.get('permission_problems') or [])


def simple_table(rows, extra_head='', extra_cell=lambda r: ''):
    body = ''.join(
        f'<tr><td class="title"><a href="/execution/{e(r["execution_id"])}">{e(r["title"])}</a>'
        f'<span class="sub">{e(r["media_type"])}, {e(r["execution_id"].rsplit("-", 1)[1])}</span></td>'
        f'<td data-label="Move">{move_cell(r["current"], r["recommended"])}</td>'
        f'<td data-label="Disks">{disks_cell(r)}</td>'
        f'<td class="num" data-label="Size">{size_text(r["size_gb"])}</td>{extra_cell(r)}</tr>' for r in rows)
    return ('<div class="scroll"><table><thead><tr><th>Title</th><th>Move</th><th>Disks</th>'
            f'<th class="num">Size</th>{extra_head}</tr></thead><tbody>{body}</tbody></table></div>')


def render_review(view, ctx):
    run_id = view['run_id']
    head = (f'<p class="crumbs"><a href="/">Runs</a> / <a href="/run/{e(run_id)}">{e(run_id)}</a></p>'
            '<h1>Confirm approvals</h1>')
    if not view['rows']:
        return page('Confirm approvals', head + '<p class="empty">Nothing selected that can be approved. '
                    f'<a href="/run/{e(run_id)}#unapproved">Back to the run</a></p>', ctx=ctx)
    flagged = [r for r in view['rows'] if r['permission_problems']]
    warn = ''
    if flagged:
        warn = (f'<div class="alert"><p>{len(flagged)} of these {"has" if len(flagged) == 1 else "have"} folders '
                'without group write. They can be '
                'approved, but a move job skips them (changing nothing) until the permissions are fixed, e.g. '
                '<code>chmod g+w</code> on each folder listed.</p></div>')
    if view['not_eligible']:
        warn += ('<p class="hint">Not approvable any more, so left out: ' + e(', '.join(view['not_eligible'])) + '</p>')

    def notes(r):
        codes = ''.join(f'<span class="note">{e(code_text(c))}</span>' for c in r['warning_codes'])
        why = f'<span class="sub">{e(r["decision_reason"])}</span>' if r.get('decision_reason') else ''
        return f'<td data-label="Notes">{codes}{problem_notes(r)}{why}</td>'
    count = len(view['rows'])
    fields = [('manifest_hash', view['manifest_hash'])] + [('id', r['execution_id']) for r in view['rows']]
    confirm = ('<div class="actions">' + form(f'/run/{run_id}/approve', ctx, fields,
                                               f'Approve {count} move{"s" if count != 1 else ""} '
                                               f'({size_text(view["total_gb"])})', 'btn primary')
               + f'<a class="btn" href="/run/{e(run_id)}#unapproved">Back</a><span class="hint">Records one '
               'approval per move, bound to this exact manifest. Nothing moves until you run them.</span></div>')
    body = (head + f'<p class="meta">{count} moves, {size_text(view["total_gb"])}</p>' + warn
            + simple_table(view['rows'], '<th>Notes</th>', notes)
            + render_disks(view['disks'], 'Free space now, and after these moves if they all run.') + confirm)
    return page('Confirm approvals', body, ctx=ctx)


def render_confirm_run(view, ctx):
    run_id = view['run_id']
    head = (f'<p class="crumbs"><a href="/">Runs</a> / <a href="/run/{e(run_id)}">{e(run_id)}</a></p>'
            '<h1>Run approved moves</h1>')
    runnable = [r for r in view['queue'] if not r['permission_problems']]
    flagged = [r for r in view['queue'] if r['permission_problems']]
    parts = [head]
    if job_busy(ctx):
        parts.append('<div class="alert"><p>A job is already running or needs review. '
                     f'<a href="/job/{e(ctx["job"]["job_id"])}">See it</a>.</p></div>')
    if not view['queue']:
        parts.append('<p class="empty">Nothing approved is waiting to run.</p>')
    else:
        parts.append(f'<p class="meta">{len(runnable)} moves, {size_text(view["total_gb"])}, one at a time, '
                     'smallest first. Each is checked (check-only) and only then moved; every move re-checks '
                     'its approval, the manifest and the live Radarr/Sonarr state.</p>')
        if flagged:
            parts.append(f'<div class="alert"><p>{len(flagged)} will be skipped (nothing changed) because of '
                         'folder permissions. Fix them and run again to include them.</p></div>')
        parts.append(simple_table(view['queue'], '<th>Order and checks</th>', lambda r: (
            f'<td data-label="Checks">{problem_notes(r) or "<span class=sub>Ready</span>"}</td>')))
    if view['held']:
        parts.append('<h2>Approved but held</h2>' + simple_table(view['held'], '<th>Why</th>', lambda r: (
            f'<td data-label="Why"><span class="note">{e(r["reason"])}</span></td>')))
    if runnable and not job_busy(ctx):
        parts.append(render_disks(view['disks'], 'Free space now, and after these moves.'))
        fields = [('manifest_hash', view['manifest_hash'])] + [('id', r['execution_id']) for r in runnable]
        parts.append('<div class="actions">' + form(f'/run/{run_id}/start', ctx, fields,
                                                     f'Start moving {len(runnable)} ({size_text(view["total_gb"])})',
                                                     'btn primary')
                     + f'<a class="btn" href="/run/{e(run_id)}">Back</a><span class="hint">You can stop the job '
                       'after any move; a move in progress is never interrupted.</span></div>')
    return page('Run approved moves', ''.join(parts), ctx=ctx)


JOB_STATE_TEXT = {jobs.STARTING: 'Starting', jobs.RUNNING: 'Running', jobs.FINISHED: 'Finished',
                  jobs.STOPPED: 'Stopped as requested', jobs.FAILED: 'Stopped with a problem',
                  jobs.INTERRUPTED: 'Interrupted'}


def render_job(job, log, ctx):
    ctx = dict(ctx, on_job_page=True)
    state = job['state']
    running = state in (jobs.STARTING, jobs.RUNNING)
    head = (f'<p class="crumbs"><a href="/">Runs</a>' + (f' / <a href="/run/{e(job["run_id"])}">{e(job["run_id"])}</a>'
                                                          if job.get('run_id') else '') + '</p>'
            f'<h1>{e(JOB_KIND.get(job["kind"], "Job"))}</h1><p class="meta">{e(job["job_id"])}, started '
            f'{e(local_time(job.get("started_utc")))}</p>')
    cls = {jobs.FINISHED: 's-succeeded', jobs.FAILED: 's-needs_reconciliation', jobs.INTERRUPTED: 's-needs_reconciliation',
           jobs.STOPPED: 's-stopped_safe'}.get(state, 's-unfinished')
    parts = [head, f'<div class="summary"><span class="state {cls}">{e(JOB_STATE_TEXT.get(state, state))}</span>'
                   f'<span>{e(job.get("message"))}</span></div>']
    current = job.get('current')
    if running and current:
        parts.append(f'<p>Now: <a href="/execution/{e(current["execution_id"])}">{e(current["title"])}</a> '
                     f'({e(current["index"])} of {e(current["total"])}, {size_text(current["size_gb"])}, '
                     f'{"moving" if current.get("step") == "moving" else "checking"})</p>')
    actions = []
    if running:
        actions.append(form(f'/job/{job["job_id"]}/stop', ctx, button='Stop after this move', cls='btn'))
    if state == jobs.INTERRUPTED:
        parts.append('<div class="alert"><p>The job process ended without finishing. A move it was running may '
                     'need attention: open the run and check anything in progress before starting again.</p></div>')
        actions.append(form(f'/job/{job["job_id"]}/dismiss', ctx, button='Dismiss', cls='btn'))
    if job['kind'] == 'plan' and state == jobs.FINISHED and job.get('run_id'):
        actions.append(f'<a class="btn primary" href="/run/{e(job["run_id"])}#unapproved">Review the new run</a>')
    if job['kind'] == 'run' and not running and job.get('run_id'):
        actions.append(f'<a class="btn" href="/run/{e(job["run_id"])}">Back to the run</a>')
    if actions:
        parts.append('<div class="actions">' + ''.join(actions) + '</div>')
    if job.get('done'):
        parts.append('<h2>Moved</h2><ul class="plain">' + ''.join(
            f'<li><a href="/execution/{e(d["execution_id"])}">{e(d["title"])}</a> '
            f'<span class="sub">{size_text(d.get("size_gb"))}</span></li>' for d in job['done']) + '</ul>')
    if job.get('skipped'):
        parts.append('<h2>Skipped or held</h2><ul class="plain">' + ''.join(
            f'<li><a href="/execution/{e(d["execution_id"])}">{e(d["title"])}</a>'
            f'<span class="note">{e(d["reason"])}</span></li>' for d in job['skipped']) + '</ul>')
    parts.append(f'<h2>Output</h2><pre class="log">{e(log) or "No output yet."}</pre>')
    return page('Job ' + job['job_id'], ''.join(parts), refresh=running, ctx=ctx, seconds=5)


def render_jobs(recent, ctx):
    if not recent:
        return ''
    return ('<section class="panel"><h2>Recent jobs</h2><ul class="plain">' + ''.join(
        f'<li><a href="/job/{e(j["job_id"])}">{e(JOB_KIND.get(j["kind"], "Job"))}, '
        f'{e(local_time(j.get("started_utc")))}</a> <span class="sub">{e(JOB_STATE_TEXT.get(j["state"], j["state"]))}: '
        f'{e(j.get("message"))}</span></li>' for j in recent) + '</ul></section>')


CSS = """
:root{--bg:#0F1B2D;--panel:#1B2B42;--line:#2A3D59;--slate:#4A5C78;--muted:#94A3B8;--text:#E6EDF6;
--teal:#2DD4BF;--sky:#38BDF8;--indigo:#818CF8;--amber:#F5B942;--archive:#94A3B8;--red:#F87171;
color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 "Segoe UI",system-ui,-apple-system,
"Helvetica Neue",Arial,sans-serif;font-variant-numeric:tabular-nums}
a{color:var(--text);text-decoration-color:var(--slate);text-underline-offset:3px}
a:hover{text-decoration-color:var(--teal)}
a:focus-visible,summary:focus-visible{outline:2px solid var(--teal);outline-offset:2px;border-radius:4px}
.top{display:flex;align-items:center;justify-content:space-between;padding:14px 24px;border-bottom:1px solid var(--line)}
.home{display:flex;align-items:center;gap:10px;font-weight:700;font-size:17px;text-decoration:none}
.mark{width:28px;height:28px}
.readonly{color:var(--muted);font-size:13px}
.nav{display:flex;gap:16px;align-items:center;font-size:14px}
.signin{display:grid;gap:12px;max-width:360px}
.days{display:flex;flex-wrap:wrap;gap:6px 14px}.field-error{color:var(--red);margin:4px 0 0}
input[type=time]{max-width:140px;color-scheme:dark}.signin label{display:grid;gap:4px}
.signout{display:flex;gap:8px;align-items:center}.signout .who{color:var(--muted)}
button.link{background:none;border:0;padding:0;color:inherit;text-decoration:underline;cursor:pointer;font:inherit}
table+h2,table+p+h2,ul.plain+h2{margin-top:28px}
main{max-width:1240px;margin:0 auto;padding:24px}
h1{font-size:28px;line-height:1.2;margin:4px 0 6px;font-weight:650}
h2{font-size:18px;margin:0 0 4px;font-weight:650}
.count{color:var(--muted);font-weight:400;font-size:15px;margin-left:6px}
.meta,.hint,.crumbs{color:var(--muted);margin:0 0 16px}
.crumbs{margin-bottom:4px;font-size:14px}
.live{background:var(--panel);border-left:3px solid var(--sky);padding:8px 12px;margin:0 0 20px;font-size:14px}
.empty{color:var(--muted);padding:24px 0}
.alert{border:1px solid var(--red);border-radius:8px;padding:12px 16px}
code{font-size:13px;color:var(--muted)}
.runs{list-style:none;padding:0;margin:0;border-top:1px solid var(--line)}
.runs a{display:grid;grid-template-columns:150px 170px 110px 1fr auto;gap:16px;align-items:center;
padding:14px 8px;border-bottom:1px solid var(--line);text-decoration:none}
.runs a:hover{background:var(--panel)}
.when{font-weight:600}.rid,.counts{color:var(--muted);font-size:14px}
.progress{display:flex;align-items:center;gap:10px;font-size:14px}
.meter{width:120px;height:6px;flex:none}.meter .track{fill:var(--slate)}.meter .fill{fill:var(--teal)}
.flags{display:flex;gap:6px;flex-wrap:wrap;justify-content:flex-end}
.flag{font-size:12px;padding:2px 8px;border-radius:10px;background:var(--panel);color:var(--muted)}
.flag.warn{color:var(--amber)}.flag.bad{color:var(--red)}.flag.ok{color:var(--teal)}
.disks{margin:8px 0 36px}
.disk{display:grid;grid-template-columns:90px 1fr;gap:4px 16px;align-items:center;margin:14px 0}
.disk .name{font-weight:650}
.bar{display:block;width:100%;height:22px;border-radius:6px;overflow:hidden}
.bar .track{fill:var(--panel)}.bar .used{fill:var(--slate)}
.bar .delta.gain{fill:var(--teal)}.bar .delta.loss{fill:var(--indigo)}.bar .reserve{fill:var(--amber)}
.legend{display:flex;flex-wrap:wrap;gap:6px 18px;list-style:none;padding:0;margin:0 0 6px;font-size:13px;color:var(--muted)}
.legend li{display:flex;align-items:center;gap:6px}.legend svg{width:12px;height:12px}
.legend .used{fill:var(--slate)}.legend .gain{fill:var(--teal)}.legend .loss{fill:var(--indigo)}.legend .reserve{fill:var(--amber)}
.figures{grid-column:2;display:flex;justify-content:space-between;gap:12px;font-size:13px;color:var(--muted)}
.disk.under .now{color:var(--amber)}
.disk .error{color:var(--red);font-size:14px}
.group{margin:0 0 36px}
.g-running h2{color:var(--sky)}.g-attention h2{color:var(--red)}
.scroll{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:14px}
th{text-align:left;color:var(--muted);font-weight:500;padding:8px 10px;border-bottom:1px solid var(--line)}
td{padding:10px;border-bottom:1px solid var(--line);vertical-align:top}
.num{text-align:right;white-space:nowrap}
td.title{min-width:200px}td.title a{font-weight:600}
.sub{display:block;color:var(--muted);font-size:13px}
.sub.reason{color:var(--amber)}
.move{display:inline-flex;align-items:center;gap:6px;white-space:nowrap}
.to{color:var(--muted)}
.pill{display:inline-block;padding:1px 9px;border-radius:999px;border:1.5px solid currentColor;font-size:13px;font-weight:600}
.c-common{color:var(--teal)}.c-current{color:var(--sky)}.c-library{color:var(--indigo)}
.c-rare{color:var(--amber)}.c-archive{color:var(--archive)}.c-hold,.c-other{color:var(--muted)}
.state{font-weight:600}
.s-succeeded{color:var(--teal)}.s-unfinished{color:var(--sky)}
.s-needs_reconciliation,.s-journal_unreadable{color:var(--red)}
.s-stopped_safe{color:var(--amber)}.s-checked,.s-not_started{color:var(--text)}
.note{display:block;color:var(--amber);font-size:13px}
.summary{display:flex;flex-wrap:wrap;align-items:center;gap:16px;margin:0 0 20px;padding:12px 16px;
background:var(--panel);border-radius:8px}
.reason{color:var(--amber);margin:6px 0 0}
.timeline{list-style:none;margin:0;padding:0 0 0 18px;border-left:2px solid var(--line)}
.ev{position:relative;padding:0 0 18px 14px}
.ev::before{content:"";position:absolute;left:-25px;top:6px;width:10px;height:10px;border-radius:50%;
background:var(--slate);border:2px solid var(--bg)}
.ev-success::before{background:var(--teal)}.ev-stopped::before{background:var(--red)}
.ev-recovery_started::before{background:var(--amber)}
.t{display:block;color:var(--muted);font-size:13px}.what{font-weight:600}
.gap{display:inline-block;margin-left:10px;color:var(--muted);font-size:13px}
.fact{display:block;color:var(--muted);font-size:13px}
details summary{cursor:pointer;color:var(--amber)}
pre{white-space:pre-wrap;word-break:break-all;font-size:12px;color:var(--muted);background:var(--panel);
padding:10px;border-radius:6px;max-height:320px;overflow:auto}
.panel{background:var(--panel);border-radius:8px;padding:14px 18px;margin:0 0 24px}
.panel h2{margin-bottom:2px}.panel .hint{margin-bottom:10px}
.inline{display:inline}
.btn{display:inline-block;font:inherit;font-size:14px;font-weight:600;color:var(--text);background:transparent;
border:1.5px solid var(--slate);border-radius:6px;padding:6px 14px;cursor:pointer;text-decoration:none;margin:0 8px 0 0}
.btn:hover{border-color:var(--teal)}
.btn:focus-visible{outline:2px solid var(--teal);outline-offset:2px}
.btn.primary{background:var(--teal);border-color:var(--teal);color:#0F1B2D}
.btn.primary:hover{filter:brightness(1.08)}
.btn.small{font-size:12px;padding:1px 8px;margin:4px 0 0;display:block}
.actions{display:flex;flex-wrap:wrap;align-items:center;gap:8px;margin:16px 0 28px}
.actions .hint{margin:0}
.notice{background:var(--panel);border-left:3px solid var(--teal);padding:8px 12px;margin:0 0 20px;font-size:14px}
.alert{margin:0 0 20px}.alert p{margin:4px 0}
.note.bad{color:var(--red)}
.steps{list-style:none;display:flex;flex-wrap:wrap;gap:12px;padding:0;margin:8px 0 28px}
.steps li{display:flex;align-items:center;gap:10px;background:var(--panel);border-radius:8px;padding:10px 14px;
flex:1 1 220px;border:1.5px solid transparent}
.steps li.now{border-color:var(--teal)}
.steps .n{display:inline-flex;align-items:center;justify-content:center;width:24px;height:24px;border-radius:50%;
background:var(--slate);font-size:13px;font-weight:700;flex:none}
.steps li.done .n{background:var(--teal);color:#0F1B2D}
.steps li .btn{margin-left:auto}
.pick{width:28px}.pick input{width:16px;height:16px;accent-color:var(--teal)}
.sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0)}
.plain{list-style:none;padding:0;margin:0 0 24px}.plain li{padding:6px 0;border-bottom:1px solid var(--line)}
.plain .note{display:inline;margin-left:8px}.plain .sub{display:inline;margin-left:8px}
pre.log{max-height:480px}
@media (max-width:760px){main{padding:16px}.runs a{grid-template-columns:1fr;gap:4px}
.flags{justify-content:flex-start}.disk{grid-template-columns:1fr}.figures{grid-column:1;flex-direction:column;gap:0}
table,tbody,tr,td{display:block}thead{display:none}
tr{padding:12px 0;border-bottom:1px solid var(--line)}td{border:0;padding:3px 0}
td[data-label]{display:grid;grid-template-columns:76px 1fr;gap:8px}
td[data-label]::before{content:attr(data-label);color:var(--muted);font-size:13px}
td[data-label]>*{grid-column:2}
td.num{text-align:left}td.title{min-width:0;padding-bottom:6px}}
.reconcile{margin:0 0 24px}.reconcile textarea{width:100%;max-width:640px;background:var(--panel);color:var(--text);border:1px solid var(--line);border-radius:6px;padding:8px}
.reconcile fieldset{border:0;padding:0;margin:8px 0}.reconcile .opt,.reconcile label{display:block;margin:4px 0}
"""


# ---------------------------------------------------------------- server

ROUTES = [
    (re.compile(r'/'), 'runs'),
    (re.compile(r'/run/(\d{8}T\d{6}Z)/confirm-run'), 'confirm_run'),
    (re.compile(r'/job/(\d{8}T\d{6}Z-(?:plan|run))'), 'job'),
    (re.compile(r'/api/job/(\d{8}T\d{6}Z-(?:plan|run))'), 'api_job'),
    (re.compile(r'/run/(\d{8}T\d{6}Z)'), 'run'),
    (re.compile(r'/execution/(\d{8}T\d{6}Z-\d{4,})'), 'execution'),
    (re.compile(r'/api/runs'), 'api_runs'),
    (re.compile(r'/api/run/(\d{8}T\d{6}Z)'), 'api_run'),
    (re.compile(r'/api/execution/(\d{8}T\d{6}Z-\d{4,})'), 'api_execution'),
    (re.compile(r'/static/app\.css'), 'css'),
    (re.compile(r'/settings'), 'settings'),
    (re.compile(r'/api/settings'), 'api_settings'),
    (re.compile(r'/settings/notifications'), 'notifications'),
    (re.compile(r'/settings/([a-z_]+)'), 'settings_section'),
    (re.compile(r'/setup'), 'setup'),
    (re.compile(r'/setup/([a-z_]+)'), 'setup_step'),
]


POST_ROUTES = [
    (re.compile(r'/plan'), 'plan'),
    (re.compile(r'/run/(\d{8}T\d{6}Z)/review'), 'review'),
    (re.compile(r'/run/(\d{8}T\d{6}Z)/approve'), 'approve'),
    (re.compile(r'/run/(\d{8}T\d{6}Z)/revoke'), 'revoke'),
    (re.compile(r'/run/(\d{8}T\d{6}Z)/start'), 'start'),
    (re.compile(r'/job/(\d{8}T\d{6}Z-(?:plan|run))/stop'), 'stop'),
    (re.compile(r'/job/(\d{8}T\d{6}Z-(?:plan|run))/dismiss'), 'dismiss'),
    (re.compile(r'/execution/(\d{8}T\d{6}Z-\d{4,})/reconcile'), 'reconcile'),
    (re.compile(r'/settings/notifications'), 'notify_save'),
    (re.compile(r'/settings/notifications/test'), 'notify_test'),
    (re.compile(r'/run/(\d{8}T\d{6}Z)/reviewed'), 'reviewed'),
    (re.compile(r'/settings/([a-z_]+)/review'), 'settings_review'),
    (re.compile(r'/settings/([a-z_]+)/save'), 'settings_save'),
    (re.compile(r'/setup/([a-z_]+)'), 'setup_step'),
]
SETTINGS_POSTS = {'settings_review', 'settings_save', 'setup_step'}
NOTICES = {'approved': '{n} approved. Nothing has moved yet.', 'unchanged': 'Those were already approved.',
           'revoked': 'Approval revoked.', 'stop': 'Stop requested: the job stops before its next move.',
           'reviewed': 'Done with this batch: the next scheduled plan will run.'}
NOTIFY_NOTICES = {'saved': 'Saved.', 'unchanged': 'Nothing changed, so nothing was saved.'}
SETTINGS_NOTICES = {'saved': 'Saved. The previous file was backed up.',
                    'unchanged': 'Nothing changed, so nothing was saved.'}
SETUP_OPEN = {'setup', 'setup_step', 'css'}  # what setup mode serves; everything else redirects to /setup


def match_route(routes, path):
    for pattern, name in routes:
        match = pattern.fullmatch(path)
        if match:
            return name, (match.group(1) if match.groups() else None)
    return None, None


class App:
    def __init__(self, base, storage=None, reserves=None, usage=shutil.disk_usage, actions=False,
                 token=None, start_job=jobs.start_job, permissions=jobs.permission_problems,
                 setup=False, health=None, load_settings=None, settings=None, wizard=None, plan_gate=None,
                 notifications=None):
        self.base, self.storage, self.reserves, self.usage = Path(base), storage or {}, reserves or {}, usage
        # Without editable settings pages, setup mode is view-only (Phase 2); with them, settings can be
        # fixed from the browser while plan, approve and run stay refused (see respond_post).
        self.actions = actions and (not setup or settings is not None)
        self.token = token or secrets.token_urlsafe(24)
        self.start_job, self.permissions = start_job, permissions
        # setup: the settings files exist but don't load, so only Settings & health (and its forms) work.
        self.setup = setup
        if health is None:
            from migratarr.health import run_checks as health
        if load_settings is None:
            from migratarr.settings import load as load_settings
        self.health, self.load_settings = health, load_settings
        # settings_ui.SettingsPages and setup_wizard.SetupWizard (main() passes both, backed by
        # migratarr.settings.ui_backend). The wizard is active only while settings files are missing.
        self.settings, self.wizard = settings, wizard
        # plan_gate() -> failing health checks; planning is refused while any fail (decision 2026-10-26).
        self.plan_gate, self.gate_failures = plan_gate, None
        self.on_reload = None
        # notify_ui.NotificationsPage: schedule & notifications (config/notifications.json); None: not offered.
        self.notifications = notifications

    def setup_mode(self):
        return bool(self.wizard and self.wizard.active())

    def reload(self, storage=None, reserves=None, base=None):
        """After settings change: use the new disks and reserves without a restart; leave setup mode."""
        if base is not None:
            self.base = Path(base)
        self.storage, self.reserves = storage or {}, reserves or {}
        self.setup = False

    def check_plan_gate(self):
        """Failing checks that block planning ([] when none). Remembered for the runs page."""
        if self.plan_gate is None:
            return []
        self.gate_failures = list(self.plan_gate())
        return self.gate_failures

    def ctx(self, query=None):
        if not self.actions:
            return None
        query = query or {}
        notice = NOTICES.get((query.get('done') or [''])[0], '')
        return dict(actions=True, token=self.token, job=jobs.active_job(self.base),
                    notice=notice.format(n=e((query.get('n') or [''])[0])) if notice else '',
                    plan_blocked=self.gate_failures or None)

    def respond(self, path, query=None, host=None):
        """Return (status, content type, body bytes) for a GET of `path`. Never writes."""
        name, arg = match_route(ROUTES, path)
        if not name:
            return self.not_found('There is no page at this address.')
        if self.setup_mode() and name not in SETUP_OPEN:
            return 'redirect', '/setup'
        try:
            if name == 'css':
                extra = getattr(self.settings, 'css', '') if self.settings else ''
                return 200, 'text/css; charset=utf-8', (CSS + extra).encode()
            if name in ('setup', 'setup_step', 'settings_section'):
                return self.respond_settings(name, arg, query)
            if name == 'notifications':
                if not self.notifications:
                    return self.not_found('Schedule and notifications are not available here.')
                ctx = self.ctx(query) or {}
                notice = NOTIFY_NOTICES.get(((query or {}).get('done') or [''])[0], '')
                body = self.notifications.get(notice, token=ctx.get('token', ''), host=host)
                if not ctx.get('actions'):
                    body = '<div class="alert"><p>Read-only view: start the UI without --read-only to change these.</p></div>' + body
                return self.html(page('Schedule & notifications', body, ctx=ctx or None))
            if name in ('settings', 'api_settings') or self.setup:
                if self.setup and name not in ('settings', 'api_settings', 'runs'):
                    return self.not_found('Fix the settings first: see Settings & health.')
                if name == 'settings' and self.settings:
                    return self.respond_settings(name, arg, query)
                report = self.load_settings()
                checks = self.health(report) if (query or {}).get('check') == ['1'] else None
                view = settings_view(report, checks)
                if name == 'api_settings':
                    return self.json(view)
                if self.settings:  # 'runs' in setup mode lands on the editable Settings page
                    return 'redirect', '/settings'
                return self.html(render_settings(view, self.ctx(query), setup=self.setup))
            if name in ('job', 'api_job', 'confirm_run') and not self.actions:
                return self.not_found('Jobs are turned off in the read-only view.')
            ctx = self.ctx(query)
            if name == 'runs':
                recent = render_jobs(jobs.recent_jobs(self.base, 5), ctx) if ctx else ''
                body = render_runs(runs_view(self.base), ctx)
                return self.html(body.replace('</main>', recent + '</main>', 1) if recent else body)
            if name == 'api_runs':
                return self.json(runs_view(self.base))
            if name in ('run', 'api_run'):
                view = run_view(self.base, arg, self.storage, self.reserves, self.usage)
                if name == 'api_run':
                    return self.json(view)
                return self.html(self.with_waiting_note(render_run(view, ctx), arg, ctx))
            if name == 'confirm_run':
                view = confirm_run_view(self.base, arg, self.storage, self.reserves, self.usage, self.permissions)
                return self.html(render_confirm_run(view, ctx))
            if name in ('job', 'api_job'):
                job = jobs.get_job(self.base, arg)
                if not job:
                    return self.not_found('No such job.')
                log = jobs.log_tail(self.base, arg)
                return self.json(dict(job, log=log)) if name == 'api_job' else self.html(render_job(job, log, ctx))
            view = execution_view(self.base, arg)
            return self.json(view) if name == 'api_execution' else self.html(render_execution(view, ctx))
        except (read_api.ReadError, jobs.JobError) as exc:
            return self.not_found(str(exc))

    def respond_post(self, path, form_data):
        """Handle one action. Returns ('redirect', url) or (status, content type, body)."""
        name, arg = match_route(POST_ROUTES, path)
        if not (self.actions and name):
            return 405, 'text/plain; charset=utf-8', b'Not allowed.\n'
        if not hmac.compare_digest((form_data.get('token') or [''])[0], self.token):
            return self.problem('This page has expired', 'The UI was restarted or the form is stale. Go back, '
                                'reload the page and try again. Nothing was changed.', 403)
        ids = form_data.get('id', [])
        expected = (form_data.get('manifest_hash') or [''])[0]
        ctx = self.ctx()
        if name in SETTINGS_POSTS:
            return self.respond_settings_post(name, arg, form_data, ctx)
        if self.setup_mode():
            return self.problem('Finish setup first', 'Migratarr is not set up yet. Nothing was changed.', 409,
                                '/setup')
        if self.setup:  # files present but invalid: settings can be fixed, nothing else runs
            return self.problem('Fix the settings first', 'The settings files have problems, so planning, '
                                'approving and moving are off. Nothing was changed.', 409, '/settings')
        if name in ('notify_save', 'notify_test', 'reviewed') and not self.notifications:
            return 405, 'text/plain; charset=utf-8', b'Not allowed.\n'
        if name in ('notify_save', 'notify_test'):
            from migratarr import notify
            try:
                result = (self.notifications.post(form_data, token=self.token) if name == 'notify_save'
                          else self.notifications.test(token=self.token))
            except (notify.NotifySettingsError, OSError) as exc:
                return self.problem('Not saved', str(exc), 500, '/settings/notifications')
            if result[0] == 'redirect':
                return result
            return result[0], 'text/html; charset=utf-8', page('Schedule & notifications', result[1], ctx=ctx).encode()
        if name == 'reviewed':
            from migratarr import schedule
            read_api.get_manifest(self.base, arg)  # a real run, or ReadError (404)
            schedule.mark_reviewed(self.notifications.state_file, arg)
            return 'redirect', f'/run/{arg}?done=reviewed'
        try:
            if name == 'plan':
                failing = self.check_plan_gate()
                if failing:
                    return self.plan_refused(failing)
                return 'redirect', '/job/' + self.start_job(self.base, 'plan')
            if name == 'review':
                pick_all = (form_data.get('pick') or [''])[0] == 'all'
                if not ids and not pick_all:
                    return self.problem('Nothing selected', 'Tick at least one move, or use "Review all".', 400,
                                        '/run/' + arg + '#unapproved')
                view = review_view(self.base, arg, ids, pick_all, self.storage, self.reserves, self.usage,
                                   self.permissions)
                return self.html(render_review(view, ctx))
            if name == 'approve':
                new = approval_store.approve(self.base, arg, ids, expected, source='ui')
                query = {'done': 'approved', 'n': len(new)} if new else {'done': 'unchanged'}
                return 'redirect', f'/run/{arg}?{urlencode(query)}#waiting'
            if name == 'revoke':
                if len(ids) != 1:
                    return self.problem('Nothing to revoke', 'Choose one move.', 400)
                if ctx['job']:
                    raise jobs.JobError('Wait for the current job to finish before revoking.')
                approval_store.revoke(self.base, arg, ids[0])
                return 'redirect', f'/run/{arg}?done=revoked#unapproved'
            if name == 'start':
                queue, _, manifest_hash = jobs.move_queue(self.base, arg)
                if manifest_hash != expected:
                    raise jobs.JobError('The manifest changed since this page was opened; review it again.')
                order = [r['execution_id'] for r in queue]
                if not ids or not set(ids) <= set(order):
                    raise jobs.JobError('The approved moves changed since this page was opened; review them again.')
                chosen = [i for i in order if i in set(ids)]
                return 'redirect', '/job/' + self.start_job(self.base, 'run', arg, chosen)
            if name == 'stop':
                jobs.request_stop(self.base, arg)
                return 'redirect', f'/job/{arg}?done=stop'
            if name == 'dismiss':
                jobs.dismiss_interrupted(self.base, arg)
                return 'redirect', '/'
            if name == 'reconcile':
                from migratarr import reconcile_store
                if ctx['job']:
                    raise jobs.JobError('Wait for the current job to finish before reconciling.')
                manifest = read_api.get_manifest(self.base, read_api.EXECUTION_ID.fullmatch(arg).group(1))
                if manifest['manifest_hash'] != expected:
                    raise jobs.JobError('The manifest changed since this page was opened; reload it.')
                try:
                    reconcile_store.record(self.base, arg, (form_data.get('outcome') or [''])[0],
                                           (form_data.get('note') or [''])[0],
                                           manifest_hash=manifest['manifest_hash'], source='ui')
                except reconcile_store.ReconcileError as exc:
                    return self.problem('Nothing was recorded', str(exc), 409, '/execution/' + arg)
                return 'redirect', '/execution/' + arg
        except (approval_store.ApprovalError, jobs.JobError, read_api.ReadError) as exc:
            return self.problem('Nothing was changed', str(exc), 409,
                                '/run/' + arg if arg and read_api.RUN_ID.fullmatch(arg) else '/')
        return self.not_found('There is no action at this address.')

    def with_waiting_note(self, body, run_id, ctx):
        """On the newest batch, while scheduled planning waits for it: say so, with "Done with this batch"."""
        if not (ctx and ctx.get('actions') and self.notifications):
            return body
        from migratarr import notify, schedule
        try:
            if not notify.load(self.notifications.settings_file)['schedule'].get('enabled'):
                return body
            waiting = schedule.batch_waiting(self.base, schedule.load_state(self.notifications.state_file))
        except Exception:  # a note, never a reason the page fails
            return body
        if not waiting or waiting['run_id'] != run_id:
            return body
        what = ('approved moves here have not run yet' if waiting['kind'] == 'approved'
                else 'nothing here has been approved yet')
        note = (f'<div class="notice"><p>Scheduled planning waits for this batch: {e(what)}. When you have '
                'approved and run what you want, or if you want none of it, mark it done; the next scheduled '
                'plan then runs.</p>' + form(f'/run/{run_id}/reviewed', ctx, button='Done with this batch',
                                           cls='btn') + '</div>')
        marker = '<p class="meta">'
        at = body.find(marker)
        end = body.find('</p>', at) + len('</p>') if at != -1 else -1
        return body[:end] + note + body[end:] if end > 0 else body

    def respond_settings(self, name, arg, query):
        from migratarr import settings_contract as sc
        ctx = self.ctx()  # settings pages carry their own notices (NOTICES' codes mean other things)
        try:
            if name in ('setup', 'setup_step'):
                if not self.wizard:
                    return self.not_found('Setup is not available in this version.')
                result = self.wizard.get(arg, ctx)
            elif not self.settings:
                return self.not_found('Settings are not available in this version.')
            elif name == 'settings':
                query = query or {}
                notice = SETTINGS_NOTICES.get(query.get('done', [''])[0], '')
                run = query.get('check') == ['1']  # probes contact services: only on request
                extra = ()
                if self.notifications:
                    summary, state = self.notifications.summary()
                    extra = (('/settings/notifications', 'Schedule & notifications', summary, state),)
                result = 200, self.settings.index(ctx, notice, run_checks=run, overview=self.settings_overview(),
                                                  setup=self.setup, extra=extra)
                if run:
                    self.gate_failures = [c for c in self.settings.last_checks if c.status == sc.FAIL]
            else:
                result = 200, self.settings.edit(arg, ctx)
        except sc.SettingsError as exc:
            return self.not_found(str(exc))
        return result if result[0] == 'redirect' else (result[0], 'text/html; charset=utf-8', result[1].encode())

    def respond_settings_post(self, name, arg, form_data, ctx):
        from migratarr import settings_contract as sc
        try:
            if name == 'setup_step':
                if not self.wizard:
                    return self.not_found('Setup is not available in this version.')
                result = self.wizard.post(arg, form_data, ctx)
            elif not self.settings or self.setup_mode():
                return self.problem('Finish setup first', 'Nothing was changed.', 409, '/setup')
            elif name == 'settings_review':
                result = self.settings.review(arg, form_data, ctx)
            else:
                result = self.settings.save(arg, form_data, ctx)
        except sc.SettingsError as exc:
            back = '/setup' if name == 'setup_step' else '/settings/' + arg if arg else '/settings'
            return self.problem('Nothing was changed', str(exc), 409, back)
        return result if result[0] == 'redirect' else (result[0], 'text/html; charset=utf-8', result[1].encode())

    def settings_overview(self):
        """The effective settings (Phase 2's read-only view) shown under the editable sections."""
        try:
            return render_settings_details(settings_view(self.load_settings()))
        except Exception as exc:  # the editable page must work even when the files can't be summarised
            return f'<p class="hint">Effective settings could not be shown: {e(type(exc).__name__)}</p>'

    def plan_refused(self, failing):
        items = ''.join(f'<li><strong>{e(c.label)}</strong>: {e(c.detail)}</li>' for c in failing)
        body = (f'<h1>Planning is unavailable</h1><div class="alert"><p>{len(failing)} health check'
                f'{"s" if len(failing) != 1 else ""} failed. Migratarr plans only when no check fails '
                f'(warnings are fine). Nothing was changed.</p><ul class="plain">{items}</ul></div>'
                f'<p><a class="btn primary" href="/settings?check=1">Open Settings &amp; health</a> '
                f'<a class="btn" href="/">Back</a></p>')
        return 409, 'text/html; charset=utf-8', page('Planning is unavailable', body, ctx=self.ctx()).encode()

    def problem(self, title, message, status=400, back='/'):
        body = page(title, f'<h1>{e(title)}</h1><div class="alert"><p>{e(message)}</p></div>'
                           f'<p><a class="btn" href="{e(back)}">Back</a></p>', ctx=self.ctx())
        return status, 'text/html; charset=utf-8', body.encode()

    @staticmethod
    def html(text):
        return 200, 'text/html; charset=utf-8', text.encode()

    @staticmethod
    def json(data):
        return 200, 'application/json', json.dumps(data, indent=2, default=str).encode()

    @staticmethod
    def not_found(message):
        body = page('Not found', f'<h1>Not found</h1><p>{e(message)}</p><p><a href="/">Back to runs</a></p>')
        return 404, 'text/html; charset=utf-8', body.encode()


SESSION_COOKIE = 'migratarr_session'
AUTH_PAGES = {'/login', '/logout', '/account/new'}
OPEN_WITHOUT_LOGIN = AUTH_PAGES | {'/static/app.css'}


def safe_next(value):
    """Where to go after signing in: a path on this UI only (no other site, no //host)."""
    value = (value or '').strip()
    if not value.startswith('/') or value.startswith('//') or '\\' in value or any(ord(c) < 32 for c in value):
        return '/'
    return value


def render_sign_in(create, error='', next_path='/', username=''):
    """The sign-in page, or (create) the first-visit page that sets the login."""
    title = 'Create your login' if create else 'Sign in'
    intro = ('<p>Migratarr is reachable from your network, so it needs a login before anything else. '
             'Choose a username and password; you will sign in with them from now on. '
             'To change it later, run <code>migratarr password</code>.</p>' if create else '')
    password = ('<label>Password<input type="password" name="password" autocomplete="new-password" '
                'minlength="8" required></label><label>Same password again<input type="password" '
                'name="confirm" autocomplete="new-password" minlength="8" required></label>' if create else
                '<label>Password<input type="password" name="password" autocomplete="current-password" '
                'required></label>')
    body = (f'<h1>{title}</h1>{intro}' + (f'<div class="alert"><p>{e(error)}</p></div>' if error else '') +
            f'<form class="signin" method="post" action="{"/account/new" if create else "/login"}">'
            f'<input type="hidden" name="next" value="{e(safe_next(next_path))}">'
            f'<label>Username<input type="text" name="username" autocomplete="username" maxlength="64" required '
            f'value="{e(username)}"></label>{password}'
            f'<button type="submit" class="btn primary">{"Create login" if create else "Sign in"}</button></form>')
    # No page links before signing in: they would only lead back here.
    return page(title, body, ctx=dict(actions=True)).replace('<a href="/settings">Settings &amp; health</a>', '', 1)


def with_account_nav(body, username, form_token):
    """Adds "Signed in as … / Sign out" to a page's top bar."""
    signout = (f'<form class="signout" method="post" action="/logout"><input type="hidden" name="token" '
               f'value="{e(form_token)}"><span class="who">{e(username)}</span>'
               f'<button type="submit" class="link">Sign out</button></form>')
    return body.replace('</nav>', signout + '</nav>', 1)


def same_origin(origin, host_header):
    """Origin header (scheme://host[:port]) names the same host and port as the request's Host header."""
    return (origin or '').split('://', 1)[-1].strip().lower() == (host_header or '').strip().lower()


def host_is_loopback(host_header):
    host = (host_header or '').strip().lower()
    if host.startswith('['):
        name = host.split(']')[0] + ']'
    else:
        name = host.rsplit(':', 1)[0] if host.count(':') == 1 else host
    return name in LOOPBACK_NAMES


def make_handler(app, accounts=None, require_login=False, throttle=None):
    """The HTTP handler. ``accounts`` (auth.Accounts) turns sign-in on: required whenever a login exists,
    and, with ``require_login`` (the UI listens beyond this machine), always, the first visit creating it.
    Without ``require_login`` only localhost Host names are served (DNS rebinding); with it, any Host name,
    since every page then needs a session cookie, which a rebound name never carries."""
    from migratarr import auth as auth_module
    throttle = throttle or auth_module.Throttle()

    class Handler(BaseHTTPRequestHandler):
        server_version = 'Migratarr-UI'
        sys_version = ''

        def log_message(self, fmt, *args):  # never log a query string or form (keys, passwords)
            sys.stderr.write('%s - - [%s] %s %s\n' % (self.address_string(), self.log_date_time_string(),
                                                      self.command, self.path.split('?', 1)[0]))

        def send(self, status, content_type, body, head=False, location=None, cookie=None):
            if content_type.startswith('text/html') and self.user and accounts is not None:
                body = with_account_nav(body.decode(), self.user, accounts.form_token(self.cookie)).encode()
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            if location:
                self.send_header('Location', location)
            if cookie is not None:
                self.send_header('Set-Cookie', cookie)
            self.send_header('Cache-Control', 'no-store')
            forms = app.actions or self.login_on()
            self.send_header('Content-Security-Policy',
                             "default-src 'none'; style-src 'self'; img-src 'self'; "
                             f"form-action {chr(39) + 'self' + chr(39) if forms else chr(39) + 'none' + chr(39)}; "
                             "frame-ancestors 'none'; base-uri 'none'")
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.end_headers()
            if not head:
                self.wfile.write(body)

        # ---- sign-in

        def login_on(self):
            """Whether this request needs a signed-in session ('create' while no login exists yet)."""
            if accounts is None:
                return False
            try:
                exists = accounts.exists()
            except auth_module.AuthError:
                return True  # a damaged file still locks the UI; `migratarr password` fixes it
            return True if exists else ('create' if require_login else False)

        def read_session(self):
            self.user = self.cookie = None
            if accounts is None:
                return
            for part in (self.headers.get('Cookie') or '').split(';'):
                name, _, value = part.strip().partition('=')
                if name == SESSION_COOKIE:
                    self.cookie = value
                    self.user = accounts.session_user(value)

        def session_cookie(self, value, max_age):
            return (f'{SESSION_COOKIE}={value}; Path=/; Max-Age={max_age}; HttpOnly; SameSite=Lax')

        def gate(self, path, head=False):
            """None when the request may go on; otherwise the response was sent."""
            need = self.login_on()
            if not need or self.user or path in OPEN_WITHOUT_LOGIN:
                return None
            target = '/account/new' if need == 'create' else '/login'
            if path.startswith('/api/'):
                self.send(HTTPStatus.UNAUTHORIZED, 'application/json; charset=utf-8',
                          b'{"error": "sign in first"}\n', head)
            elif self.command in ('GET', 'HEAD'):
                query = urlencode({'next': self.path}) if self.path not in ('/', '') else ''
                self.send(HTTPStatus.SEE_OTHER, 'text/plain; charset=utf-8', b'', head,
                          location=target + ('?' + query if query else ''))
            else:
                self.send(HTTPStatus.UNAUTHORIZED, 'text/plain; charset=utf-8',
                          b'Sign in first. Nothing was changed.\n')
            return True

        def auth_get(self, path, query, head):
            need = self.login_on()
            next_path = safe_next((query.get('next') or ['/'])[0])
            if path == '/logout' or not need:
                return self.send(HTTPStatus.SEE_OTHER, 'text/plain; charset=utf-8', b'', head, location='/')
            create = need == 'create'
            if (path == '/account/new') != create:  # only the page that applies right now
                return self.send(HTTPStatus.SEE_OTHER, 'text/plain; charset=utf-8', b'', head,
                                 location='/account/new' if create else '/login')
            if self.user:
                return self.send(HTTPStatus.SEE_OTHER, 'text/plain; charset=utf-8', b'', head, location=next_path)
            self.send(200, 'text/html; charset=utf-8', render_sign_in(create, next_path=next_path).encode(), head)

        def auth_post(self, path, data):
            field = lambda name: (data.get(name) or [''])[0]
            next_path = safe_next(field('next'))
            if path == '/logout':
                if self.user and hmac.compare_digest(field('token'), accounts.form_token(self.cookie)):
                    self.user = None
                    return self.send(HTTPStatus.SEE_OTHER, 'text/plain; charset=utf-8', b'',
                                     location='/login', cookie=self.session_cookie('', 0))
                return self.send(HTTPStatus.SEE_OTHER, 'text/plain; charset=utf-8', b'', location='/')
            need = self.login_on()
            create = need == 'create'
            if not need or (path == '/account/new') != create:
                return self.send(HTTPStatus.CONFLICT, 'text/plain; charset=utf-8',
                                 b'This page no longer applies; reload it.\n')
            address = self.client_address[0]
            wait = throttle.blocked_for(address)
            if wait and not create:
                body = render_sign_in(False, f'Too many wrong passwords. Try again in {wait} seconds.',
                                      next_path, field('username'))
                return self.send(HTTPStatus.TOO_MANY_REQUESTS, 'text/html; charset=utf-8', body.encode())
            if create:
                problem = auth_module.check_new_login(field('username'), field('password'), field('confirm'))
                if not problem:
                    try:
                        accounts.set(field('username'), field('password'))
                    except (auth_module.AuthError, OSError) as exc:
                        problem = str(exc)
                if problem:
                    body = render_sign_in(True, problem, next_path, field('username'))
                    return self.send(HTTPStatus.BAD_REQUEST, 'text/html; charset=utf-8', body.encode())
            elif not accounts.verify(field('username'), field('password')):
                throttle.failed(address)
                body = render_sign_in(False, 'Wrong username or password.', next_path, field('username'))
                return self.send(HTTPStatus.UNAUTHORIZED, 'text/html; charset=utf-8', body.encode())
            throttle.succeeded(address)
            value = accounts.issue()
            self.user, self.cookie = accounts.session_user(value), value
            return self.send(HTTPStatus.SEE_OTHER, 'text/plain; charset=utf-8', b'', location=next_path,
                             cookie=self.session_cookie(value, auth_module.SESSION_DAYS * 86400))

        # ---- requests

        def host_allowed(self):
            # Localhost only unless listening on the network with sign-in (see make_handler).
            return require_login and accounts is not None or host_is_loopback(self.headers.get('Host'))

        def handle_get(self, head):
            self.read_session()
            if not self.host_allowed():
                return self.send(HTTPStatus.MISDIRECTED_REQUEST, 'text/plain; charset=utf-8',
                                 b'Open this page through localhost.\n', head)
            path, _, query = self.path.split('#', 1)[0].partition('?')
            if accounts is not None and path in AUTH_PAGES:
                return self.auth_get(path, parse_qs(query), head)
            if self.gate(path, head):
                return
            result = app.respond(path, parse_qs(query), host=self.headers.get('Host'))
            if result[0] == 'redirect':
                return self.send(HTTPStatus.SEE_OTHER, 'text/plain; charset=utf-8', b'', head, location=result[1])
            self.send(*result, head=head)

        def do_GET(self):
            self.handle_get(head=False)

        def do_HEAD(self):
            self.handle_get(head=True)

        def refuse(self):
            self.user = None
            self.send(HTTPStatus.METHOD_NOT_ALLOWED, 'text/plain; charset=utf-8',
                      b'This view is read-only.\n')

        def do_POST(self):
            self.read_session()
            path = self.path.split('?', 1)[0]
            auth_page = accounts is not None and path in AUTH_PAGES
            if not (app.actions or auth_page):
                return self.refuse()
            if not self.host_allowed():
                return self.send(HTTPStatus.MISDIRECTED_REQUEST, 'text/plain; charset=utf-8',
                                 b'Open this page through localhost.\n')
            # Browsers send these on cross-site requests; refuse anything not from this UI's own pages.
            site = self.headers.get('Sec-Fetch-Site')
            origin = self.headers.get('Origin')
            host = self.headers.get('Host')
            if (site and site not in ('same-origin', 'none')) or (
                    origin and origin != 'null' and not (same_origin(origin, host) or (
                        not require_login and host_is_loopback(origin.split('://', 1)[-1])))):
                return self.send(HTTPStatus.FORBIDDEN, 'text/plain; charset=utf-8', b'Cross-site request refused.\n')
            kind = (self.headers.get('Content-Type') or '').split(';', 1)[0].strip()
            try:
                length = int(self.headers.get('Content-Length') or 0)
            except ValueError:
                length = -1
            if kind != 'application/x-www-form-urlencoded' or not 0 <= length <= 256 * 1024:
                return self.send(HTTPStatus.BAD_REQUEST, 'text/plain; charset=utf-8', b'Bad form.\n')
            data = parse_qs(self.rfile.read(length).decode('utf-8', 'replace'), keep_blank_values=True)
            if auth_page:
                return self.auth_post(path, data)
            if self.gate(path):
                return
            result = app.respond_post(path, data)
            if result[0] == 'redirect':
                return self.send(HTTPStatus.SEE_OTHER, 'text/plain; charset=utf-8', b'', location=result[1])
            self.send(*result)

        do_PUT = do_DELETE = do_PATCH = do_OPTIONS = refuse

    return Handler


def load_context(base=None, environ=None):
    """Base path, disk paths and reserves from the deployment config (``environ``: tests, the settings backend)."""
    from migratarr.runtime_config import get_config, load_config
    runtime = get_config() if environ is None else load_config(environ=environ)
    reserves = {}
    try:
        from migratarr.storage_targets import deployed_targets_path, load_targets
        targets = load_targets(deployed_targets_path(runtime, environ))
        reserves = {t.id: t.minimum_free_space_gb for t in targets.targets}
    except Exception as exc:  # reserves are a display nicety; the page works without them
        print('Reserves not shown: ' + str(exc), file=sys.stderr)
    return Path(base) if base else runtime.base_path, runtime.storage, reserves


def reload_context(base=None, environ=None):
    """load_context after the settings files changed (the loaders cache what they read)."""
    from migratarr.planner_settings import get_settings
    from migratarr.runtime_config import get_config
    get_config.cache_clear()
    get_settings.cache_clear()
    return load_context(base, environ)


def build_app(base=None, actions=True, backend=None):
    """The UI with its settings pages, first-run wizard and plan gate (handoff §3-4).

    - settings files missing: the wizard (every page redirects to /setup);
    - present but invalid: Settings & health with edit forms; plan, approve and run refused;
    - valid: the normal UI. Planning is refused while any health check fails.
    After a save or setup the new disks, reserves and base path are used without a restart.
    """
    import os
    from migratarr import settings_contract as sc
    from migratarr import settings_ui
    from migratarr import setup_wizard
    from migratarr import health
    from migratarr.settings.ui_backend import UiBackend
    backend = backend or UiBackend(os.environ, base=base)
    pages = settings_ui.SettingsPages(backend, backend)
    wizard = setup_wizard.SetupWizard(backend, backend)
    gate = lambda: [c for c in backend.run_checks() if c.status == sc.FAIL]  # the checks doctor runs
    report = backend.report()
    environ = backend.environ if backend.environ is not os.environ else None
    from migratarr import notify, notify_ui, schedule
    notifications = notify_ui.NotificationsPage(notify.settings_path(backend.environ),
                                                schedule.state_path(backend.environ))
    common = dict(actions=actions, settings=pages, wizard=wizard, plan_gate=gate, load_settings=backend.report,
                  notifications=notifications,
                  health=lambda report: health.run_checks(report, environ=backend.environ, http=backend.http,
                                                          run=backend.run))
    if report.missing:
        print('Not set up yet: open the UI to set up Migratarr (missing: ' + ', '.join(report.missing) + ').',
              file=sys.stderr)
        app = App(base or backend.base_of(report), **common)
    else:
        try:
            ctx_base, storage, reserves = load_context(base, environ)
            app = App(ctx_base, storage, reserves, **common)
        except ValueError as exc:  # ConfigError, storage targets: show what's wrong instead of exiting
            print('Settings problem; starting on Settings & health: ' + str(exc), file=sys.stderr)
            app = App(base or backend.base_of(report), setup=True, **common)

    def reload(_result=None):
        try:
            new_base, storage, reserves = reload_context(base, environ)
        except ValueError as exc:
            print('Settings saved but do not load yet: ' + str(exc), file=sys.stderr)
            app.setup = True
            return
        app.reload(storage, reserves, new_base)
        app.gate_failures = None

    pages.on_saved = wizard.on_created = app.on_reload = reload
    return app


LOOPBACK_BINDS = {'127.0.0.1', 'localhost', '::1'}


def env_port(environ):
    value = environ.get('MIGRATARR_PORT') or '8765'
    try:
        return int(value)
    except ValueError:
        raise SystemExit(f'MIGRATARR_PORT must be a number, not {value!r}.')


class ServerV6(ThreadingHTTPServer):
    import socket as _socket
    address_family = _socket.AF_INET6


def start_scheduler(app):
    """Scheduled planning (migratarr/schedule.py) in a background thread. Returns the Event that stops it."""
    import threading
    from migratarr import notify, schedule
    stop = threading.Event()
    log = lambda line: print(line, file=sys.stderr, flush=True)

    def tick_once(now):
        if not app.notifications or app.setup or app.setup_mode():
            return
        schedule.tick(app.base, now, settings_file=app.notifications.settings_file,
                      state_file=app.notifications.state_file, active_job=lambda: jobs.active_job(app.base),
                      start_plan=lambda: app.start_job(app.base, 'plan'), gate=app.check_plan_gate,
                      send=notify.send, log=log)
    threading.Thread(target=schedule.run_forever, args=(stop, tick_once), kwargs=dict(log=log),
                     name='migratarr-schedule', daemon=True).start()
    return stop


def main(argv=None, environ=None):
    import os
    environ = os.environ if environ is None else environ
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--host', default=environ.get('MIGRATARR_HOST') or '127.0.0.1',
                        help='address to listen on (default 127.0.0.1, or $MIGRATARR_HOST); '
                             'anything else needs a sign-in, set on the first visit')
    parser.add_argument('--port', type=int, default=env_port(environ),
                        help='port (default 8765, or $MIGRATARR_PORT)')
    parser.add_argument('--base', type=Path, help='Migratarr base path (default: runtime.json base_path)')
    parser.add_argument('--read-only', action='store_true',
                        help='View only: no planning, approving or moving from the browser')
    args = parser.parse_args(argv)
    # One-time move of runtime.json to version 2 (docs/settings.md); never blocks startup.
    from migratarr.settings import migrate_if_needed
    from migratarr.auth import Accounts, AuthError
    migrate_if_needed(log=lambda message: print(message, file=sys.stderr, flush=True))
    app = build_app(args.base, actions=not args.read_only)
    network = args.host not in LOOPBACK_BINDS
    accounts = Accounts()
    try:
        has_login = accounts.exists()
    except AuthError as exc:
        print(str(exc), file=sys.stderr)
        has_login = True
    server_class = ServerV6 if ':' in args.host else ThreadingHTTPServer
    try:
        server = server_class((args.host, args.port), make_handler(app, accounts, require_login=network))
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            print(f'Port {args.port} is already in use (is another Migratarr UI running?). '
                  f'Stop it, or start this one with --port {args.port + 1}.', file=sys.stderr)
            return 2
        if exc.errno in (errno.EADDRNOTAVAIL, errno.EAFNOSUPPORT):
            print(f'Cannot listen on {args.host}: {exc.strerror}. Use 0.0.0.0 for every network '
                  'interface, or 127.0.0.1 for this machine only.', file=sys.stderr)
            return 2
        raise
    stop_scheduler = start_scheduler(app) if not args.read_only else None
    mode = 'read-only view' if args.read_only else 'UI (plan, confirm, run)'
    if network:
        shown = 'http://<this host>:' + str(args.port) if args.host in ('0.0.0.0', '::') else \
            f'http://{args.host}:{args.port}'
        print(f'Migratarr {mode} on {shown}  (Ctrl+C to stop; running jobs keep going)\n'
              + ('Sign-in required.' if has_login else
                 'No login yet: the first visit creates it. Open it now, before anyone else on your network does.')
              + " Don't expose this port to the internet.", flush=True)
    else:
        print(f'Migratarr {mode} on http://localhost:{args.port}  (Ctrl+C to stop; running jobs keep going)\n'
              f'From another computer: ssh -L {args.port}:localhost:{args.port} <user>@<this host>, '
              'or start it with --host 0.0.0.0 to use it on your network with a sign-in'
              + (' (sign-in required: a login is set)' if has_login else ''), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if stop_scheduler:
            stop_scheduler.set()
        server.server_close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
