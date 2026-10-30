"""Scheduled planning, run by the web UI while it is running (migratarr/ui.py).

On the days and at the time set in config/notifications.json, the scheduler:

- waits while a job is running (it tries again until the job ends, up to CATCH_UP);
- does not plan while the newest batch is still waiting for you: nothing in it
  approved or moved yet, or approved moves not run yet. It sends one reminder
  instead. "Mark run done" on the run page (or the runs list) counts as reviewed;
- does not plan while a health check fails (it notifies instead);
- otherwise starts the same planning job as "Plan a new run". Planning never
  approves or moves anything; the batch-ready notification follows from the job.

A time missed while the UI was down is caught up once if it is less than
CATCH_UP old. Times before the schedule was switched on never count. State
(last time handled, batches marked done) is in config/schedule-state.json.
Standard library only.
"""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import threading
import time

from migratarr import notify

STATE_NAME = 'schedule-state.json'
CATCH_UP = timedelta(hours=12)
TICK_SECONDS = 30
KEEP_REVIEWED = 50
_lock = threading.Lock()


def state_path(environ=None):
    from migratarr.paths import config_dir
    return config_dir(environ) / STATE_NAME


def load_state(path):
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(path, state):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name('.' + path.name + '.tmp')
    temp.write_text(json.dumps(state, indent=2) + '\n', encoding='utf-8')
    os.replace(temp, path)


def _parse(value):
    try:
        return datetime.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None


def local_time(day, hour, minute, tzinfo=None):
    """``hour:minute`` on ``day`` in local time, DST included (aware). With ``tzinfo`` (tests): that zone."""
    if tzinfo is not None:
        return datetime(day.year, day.month, day.day, hour, minute, tzinfo=tzinfo)
    stamp = time.mktime((day.year, day.month, day.day, hour, minute, 0, 0, 0, -1))
    return datetime.fromtimestamp(stamp, timezone.utc).astimezone()


def latest_occurrence(now, schedule, tzinfo=None):
    """The most recent scheduled time at or before ``now`` (local, aware), or None."""
    hour, minute = map(int, schedule['time'].split(':'))
    days = {notify.DAYS.index(d) for d in schedule.get('days') or [] if d in notify.DAYS}
    if not days:
        return None
    for back in range(8):
        day = (now - timedelta(days=back)).date()
        if day.weekday() in days:
            when = local_time(day, hour, minute, tzinfo)
            if when <= now:
                return when
    return None


def next_occurrence(now, schedule, tzinfo=None):
    """The next scheduled time after ``now``, or None (for the settings page)."""
    hour, minute = map(int, schedule['time'].split(':'))
    days = {notify.DAYS.index(d) for d in schedule.get('days') or [] if d in notify.DAYS}
    for ahead in range(8):
        day = (now + timedelta(days=ahead)).date()
        when = local_time(day, hour, minute, tzinfo)
        if day.weekday() in days and when > now:
            return when
    return None


def due(now, schedule, state, tzinfo=None):
    """The scheduled time to handle now, or None."""
    if not schedule.get('enabled'):
        return None
    when = latest_occurrence(now, schedule, tzinfo)
    if when is None or now - when > CATCH_UP:
        return None
    for done in (_parse(state.get('last_due')), _parse(state.get('enabled_at'))):
        if done is not None and when <= done:
            return None
    return when


def batch_waiting(base, state):
    """What is still waiting in the newest batch: {run_id, kind, count, size_gb}, or None.

    kind 'unreviewed': nothing approved, revoked or moved yet. kind 'approved': approved moves not yet run.
    """
    from migratarr import read_api
    runs = [r for r in read_api.list_runs(base) if r['manifest_present']]
    if not runs or runs[0]['run_id'] in (state.get('reviewed') or []):
        return None
    run_id = runs[0]['run_id']
    status = read_api.run_status(base, run_id)
    rows = status['rows']
    if not rows or status.get('closed'):
        return None
    waiting_states = (read_api.NOT_STARTED, read_api.CHECKED, read_api.STOPPED_SAFE)
    approved = [r for r in rows if r['approval'] == read_api.APPROVED and r['execution'] in waiting_states]
    touched = any(r['approval'] != read_api.UNAPPROVED or r['execution'] != read_api.NOT_STARTED for r in rows)
    pick, kind = (approved, 'approved') if approved else (rows, 'unreviewed') if not touched else (None, None)
    if not pick:
        return None
    size = sum(float(r.get('size_gb') or 0) for r in pick)
    return dict(run_id=run_id, kind=kind, count=len(pick), size_gb=round(size, 2))


def mark_reviewed(path, run_id):
    with _lock:
        state = load_state(path)
        reviewed = [r for r in state.get('reviewed') or [] if r != run_id] + [run_id]
        state['reviewed'] = reviewed[-KEEP_REVIEWED:]
        save_state(path, state)


def schedule_switched_on(path, now):
    """Called when the schedule is turned on: times before now never count."""
    with _lock:
        state = load_state(path)
        state['enabled_at'] = now.isoformat()
        save_state(path, state)


def reminder(waiting):
    what = (f'{waiting["count"]} approved move{"s" if waiting["count"] != 1 else ""} not run yet'
            if waiting['kind'] == 'approved' else
            f'{waiting["count"]} planned move{"s" if waiting["count"] != 1 else ""} not reviewed yet')
    return notify.Message(notify.BATCH_READY, 'Migratarr: the last batch is still waiting',
                          f'{what} (run {waiting["run_id"]}, {notify.gb(waiting["size_gb"])}). The scheduled plan '
                          'was skipped; it runs at the next scheduled time once this batch is done. "Done with '
                          'this batch" on the run page skips the rest.',
                          f'/run/{waiting["run_id"]}', dict(waiting))


def tick(base, now, *, settings_file, state_file, active_job, start_plan, gate, send=notify.send, log=print,
         tzinfo=None):
    """Handle a due scheduled time. Returns what happened ('', 'waiting-job', 'reminded', 'gate', 'planned')."""
    try:
        settings = notify.load(settings_file)
    except notify.NotifySettingsError as exc:
        log('Schedule: ' + str(exc))
        return ''
    with _lock:
        state = load_state(state_file)
        when = due(now, settings['schedule'], state, tzinfo)
        if when is None:
            return ''
        if active_job():
            return 'waiting-job'  # try again on the next tick, until CATCH_UP
        state['last_due'] = when.isoformat()  # handled from here on: never twice, even if something below fails
        save_state(state_file, state)
    waiting = batch_waiting(base, state)
    if waiting:
        log(f'Schedule: plan for {when:%a %H:%M} skipped, run {waiting["run_id"]} is still waiting')
        send(settings, reminder(waiting))
        return 'reminded'
    failing = gate()
    if failing:
        names = ', '.join(getattr(c, 'label', str(c)) for c in failing[:3])
        log(f'Schedule: plan for {when:%a %H:%M} skipped, {len(failing)} health check(s) failing')
        send(settings, notify.Message(notify.FAILED, 'Migratarr: scheduled plan skipped',
                                      f'{len(failing)} health check{"s" if len(failing) != 1 else ""} failing '
                                      f'({names}). Fix them in Settings & health; the next scheduled plan '
                                      'runs once they pass.', '/settings?check=1'))
        return 'gate'
    job_id = start_plan()
    log(f'Schedule: planning started for {when:%a %H:%M} (job {job_id})')
    return 'planned'


def run_forever(stop, tick_once, seconds=TICK_SECONDS, log=print):
    """The UI's scheduler thread: tick until ``stop`` (a threading.Event) is set."""
    while not stop.wait(seconds):
        try:
            tick_once(datetime.now(timezone.utc).astimezone())
        except Exception as exc:  # never take the UI down
            log(f'Schedule: {type(exc).__name__}: {exc}')
