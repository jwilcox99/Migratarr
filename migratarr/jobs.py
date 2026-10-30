#!/usr/bin/env python3
"""Background jobs for the web UI: plan a run, or run approved moves one at a time.

The UI starts a job with start_job(); the job runs as its own process
(``migratarr_jobs.py run ...``), in its own session, so closing the browser or
restarting the UI doesn't stop it. Only one job runs at a time.

Files, under <base>/jobs/ (git-ignored):

    active.json        the running job, replaced atomically as it progresses
    <job_id>.log       everything the job and its steps print
    <job_id>.json      the finished job's record
    <job_id>.stop      a stop request: the job stops before its next move
    runner.lock        held (flock) by the running job for its whole life

A move job never approves anything and never interrupts a move: for each row,
smallest first, it re-checks the manifest hash, the row's journal state, its
approval, the runtime config and the source folders' permissions, then runs
the row's executor check-only and, only if that passes, with --execute, and
finally requires a SUCCESS bound to this manifest. Any executor failure stops
the job, like batch_cross_*.py. Standard library only.
"""
import argparse
import contextlib
import fcntl
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from migratarr import read_api
from migratarr.executor_manifest import approval_is_current

from migratarr.paths import CODE as REPO, module_command  # steps run as modules (clone or installed)
EXECUTORS = {
    ('Movie', 'SAME_DISK_RENAME'): 'execute_movie_nas.py',
    ('TV', 'SAME_DISK_RENAME'): 'execute_tv_nas.py',
    ('Movie', 'CROSS_DISK_TRANSFER'): 'execute_cross_movie.py',
    ('TV', 'CROSS_DISK_TRANSFER'): 'execute_cross_tv.py',
}
RUNNABLE_STATES = (read_api.NOT_STARTED, read_api.CHECKED, read_api.STOPPED_SAFE)
JOB_ID_CHARS = set('0123456789TZ-planrunexcut')

# Job states
STARTING, RUNNING, FINISHED, STOPPED, FAILED, INTERRUPTED = (
    'STARTING', 'RUNNING', 'FINISHED', 'STOPPED', 'FAILED', 'INTERRUPTED')


class JobError(RuntimeError):
    """A job can't be started, or a move job must stop."""


def utc():
    return datetime.now(timezone.utc).isoformat()


def jobs_dir(base):
    return Path(base) / 'jobs'


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)
            f.write('\n')
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


def valid_job_id(job_id):
    return bool(job_id) and len(job_id) <= 40 and set(job_id) <= JOB_ID_CHARS


# ---------------------------------------------------------------- reading (UI, never writes)

def process_alive(pid, job_id):
    """True if ``pid`` is a live (not zombie) migratarr_jobs.py process for ``job_id``."""
    try:
        cmdline = Path(f'/proc/{int(pid)}/cmdline').read_bytes().split(b'\0')
        state = Path(f'/proc/{int(pid)}/stat').read_text().rsplit(')', 1)[1].split()[0]
    except (OSError, ValueError, TypeError, IndexError):
        return False
    # Started as `python -m migratarr.jobs` (0.1.0); jobs begun by older UIs ran the file or `-m migratarr_jobs`.
    ours = any(c.endswith(b'migratarr_jobs.py') for c in cmdline) or \
        any(a == b'-m' and b in (b'migratarr.jobs', b'migratarr_jobs') for a, b in zip(cmdline, cmdline[1:]))
    return state != 'Z' and ours and job_id.encode() in cmdline


def active_job(base, alive=None):
    """The current job, or None. A job whose process is gone is reported INTERRUPTED."""
    alive = alive or process_alive
    job = read_json(jobs_dir(base) / 'active.json')
    if not job or not valid_job_id(job.get('job_id')):
        return None
    if job.get('state') in (STARTING, RUNNING) and not alive(job.get('pid'), job['job_id']):
        if job.get('state') == STARTING and time.time() - job.get('started_epoch', 0) < 30:
            return job
        job = dict(job, state=INTERRUPTED,
                   message='The job process is no longer running. Check the moves it was working on.')
    return job


def get_job(base, job_id):
    if not valid_job_id(job_id):
        return None
    active = active_job(base)
    if active and active['job_id'] == job_id:
        return active
    return read_json(jobs_dir(base) / (job_id + '.json'))


def recent_jobs(base, limit=10):
    folder = jobs_dir(base)
    names = sorted((p.name for p in folder.glob('*.json') if p.name != 'active.json'), reverse=True)
    jobs = [read_json(folder / n) for n in names[:limit]]
    return [j for j in jobs if j]


def log_tail(base, job_id, lines=80, max_bytes=64 * 1024):
    if not valid_job_id(job_id):
        return ''
    path = jobs_dir(base) / (job_id + '.log')
    try:
        with open(path, 'rb') as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            data = f.read().decode('utf-8', 'replace')
    except OSError:
        return ''
    # Progress bars rewrite the line with \r; keep only the latest version of each line.
    out = [line.rsplit('\r', 1)[-1] for line in data.replace('\r\n', '\n').split('\n')]
    return '\n'.join(out[-lines:])


def permission_problems(source_path, limit=5):
    """Folders under ``source_path`` the group can't write to.

    The NAS-side executors delete and rename as a NAS user that reaches media
    through the shared group, and Radarr/Sonarr write through it too, so every
    folder of a move needs group write. Nimrods (20261015T174112Z-0004) copied
    and then stopped at the delete because its folder was 2755. A missing
    source is left to the executor, which refuses it with its own message.
    """
    root = Path(source_path or '')
    if not source_path or not root.is_dir():
        return []
    problems = []
    for folder, dirs, _ in os.walk(root, followlinks=False):
        try:
            mode = os.lstat(folder).st_mode
        except OSError as exc:
            problems.append(f'{folder}: {exc.strerror}')
        else:
            if not mode & stat.S_IWGRP:
                problems.append(f'{folder} is {stat.filemode(mode)} (no group write)')
        if len(problems) >= limit:
            break
    return problems


def move_queue(base, run_id, execution_ids=None):
    """Rows a move job would run, smallest first: approved for this manifest and runnable.

    Returns (queue, held, manifest_hash). ``held`` lists approved rows that
    can't run now, each with a reason.
    """
    status = read_api.run_status(base, run_id)
    if not status['integrity_ok']:
        raise JobError('Manifest failed its integrity check')
    manifest = {r['execution_id']: r for r in read_api.get_manifest(base, run_id)['rows']}
    queue, held = [], []
    wanted = set(execution_ids) if execution_ids is not None else None
    for row in status['rows']:
        if row['approval'] != read_api.APPROVED or row['execution'] == read_api.SUCCEEDED:
            continue
        if wanted is not None and row['execution_id'] not in wanted:
            continue
        detail = manifest[row['execution_id']]
        item = dict(row, source_path=detail.get('source_path'), target_path=detail.get('target_path'))
        if row['execution'] not in RUNNABLE_STATES:
            held.append(dict(item, reason='Needs attention first: ' + row['execution'].replace('_', ' ').lower()))
        elif (row['media_type'], row['transfer_type']) not in EXECUTORS:
            held.append(dict(item, reason='No executor for ' + row['media_type'] + ' ' + row['transfer_type']))
        else:
            queue.append(item)
    queue.sort(key=lambda r: (float(r['size_gb'] or 0), r['execution_id']))
    return queue, held, status['manifest_hash']


# ---------------------------------------------------------------- starting (UI)

_start_lock = threading.Lock()


def start_job(base, kind, run_id=None, execution_ids=(), repo=REPO, popen=subprocess.Popen):
    """Start a plan or move job in its own process; return its job ID."""
    if kind not in ('plan', 'run'):
        raise JobError('Unknown job kind')
    with _start_lock:
        current = active_job(base)
        if current and current['state'] in (STARTING, RUNNING):
            raise JobError('Another job is already running: ' + current['job_id'])
        if current:
            raise JobError('The last job was interrupted; dismiss it on its page first.')
        job_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + kind
        folder = jobs_dir(base)
        folder.mkdir(exist_ok=True)
        command = module_command('migratarr_jobs.py', 'run', '--base', base, '--job', job_id, kind)
        if kind == 'run':
            command += ['--run', read_api._checked_run_id(run_id)]
            for execution_id in execution_ids:
                command += ['--id', execution_id]
        if runner_busy(base):
            raise JobError('A job process is still running; wait for it to finish.')
        # Written before the process starts, so the job's own updates (and its removal of
        # active.json when it finishes) always come after this record, never before it.
        write_json(folder / 'active.json', dict(
            job_id=job_id, kind=kind, run_id=run_id, state=STARTING, pid=None, started_utc=utc(),
            started_epoch=time.time(), queue=list(execution_ids), current=None, done=[], skipped=[],
            message='Starting'))
        try:
            with open(folder / (job_id + '.log'), 'ab') as log:
                proc = popen(command, cwd=str(repo), stdin=subprocess.DEVNULL, stdout=log,
                             stderr=subprocess.STDOUT, start_new_session=True)
        except BaseException:
            (folder / 'active.json').unlink()
            raise
        if hasattr(proc, 'wait'):  # reap the child when it exits, so it never lingers as a zombie
            threading.Thread(target=proc.wait, daemon=True).start()
        return job_id


def runner_busy(base):
    """True if a job process holds runner.lock (checked only when starting a job)."""
    path = jobs_dir(base) / 'runner.lock'
    if not path.exists():
        return False
    with open(path, 'a') as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        return False


def request_stop(base, job_id):
    job = active_job(base)
    if not job or job['job_id'] != job_id or job['state'] not in (STARTING, RUNNING):
        raise JobError('That job is not running.')
    (jobs_dir(base) / (job_id + '.stop')).write_text(utc() + '\n')


def dismiss_interrupted(base, job_id):
    job = active_job(base)
    if not job or job['job_id'] != job_id or job['state'] != INTERRUPTED:
        raise JobError('Only an interrupted job can be dismissed.')
    write_json(jobs_dir(base) / (job_id + '.json'), dict(job, finished_utc=utc()))
    (jobs_dir(base) / 'active.json').unlink()


# ---------------------------------------------------------------- running (job process)

class Job:
    def __init__(self, base, job_id, kind, run_id=None):
        self.base, self.folder = Path(base), jobs_dir(base)
        self.record = dict(job_id=job_id, kind=kind, run_id=run_id, state=RUNNING, pid=os.getpid(),
                           started_utc=utc(), started_epoch=time.time(), queue=[], current=None,
                           done=[], skipped=[], message='Running')

    def update(self, **changes):
        self.record.update(changes)
        write_json(self.folder / 'active.json', self.record)

    def say(self, text):
        print(text, flush=True)

    def stop_requested(self):
        return (self.folder / (self.record['job_id'] + '.stop')).exists()

    def finish(self, state, message):
        self.record.update(state=state, message=message, current=None, finished_utc=utc())
        write_json(self.folder / (self.record['job_id'] + '.json'), self.record)
        active = read_json(self.folder / 'active.json')
        if active and active.get('job_id') == self.record['job_id']:
            (self.folder / 'active.json').unlink()
        with contextlib.suppress(OSError):
            (self.folder / (self.record['job_id'] + '.stop')).unlink()
        self.say(f'\nJOB {state}: {message}')


def run_plan(job, repo=REPO, runner=subprocess.run):
    before = {r['run_id'] for r in read_api.list_runs(job.base)}
    job.update(message='Planning: scoring, building the plan, snapshot and manifest')
    code = runner(module_command('migratarr_plan.py'), cwd=str(repo)).returncode
    created = sorted({r['run_id'] for r in read_api.list_runs(job.base)} - before)
    job.record['run_id'] = created[-1] if created else None
    if code != 0:
        return job.finish(FAILED, 'Planning stopped; see the log. Nothing was approved or moved.')
    if len(created) != 1:
        return job.finish(FAILED, f'Expected one new run, found {len(created)}.')
    return job.finish(FINISHED, f'Run {created[0]} is ready to review. Nothing is approved yet.')


def run_moves(job, run_id, execution_ids, repo=REPO, runner=subprocess.run, config_snapshot=None,
              permissions=permission_problems):
    """Run each approved row in turn; see the module docstring for the checks."""
    base = job.base
    snapshot = config_snapshot or _config_snapshot
    config_before = snapshot()
    queue, held, manifest_hash = move_queue(base, run_id, execution_ids)
    for row in held:
        job.record['skipped'].append(dict(execution_id=row['execution_id'], title=row['title'], reason=row['reason']))
        job.say(f'HELD {row["execution_id"]} {row["title"]}: {row["reason"]}')
    job.update(queue=[r['execution_id'] for r in queue])
    job.say(f'{len(queue)} move(s) to run, smallest first; {len(held)} held.')
    for index, row in enumerate(queue, 1):
        execution_id, title = row['execution_id'], row['title']
        if job.stop_requested():
            return job.finish(STOPPED, f'Stopped as requested before {title}. {len(job.record["done"])} moved.')
        job.update(current=dict(execution_id=execution_id, title=title, size_gb=row['size_gb'],
                                index=index, total=len(queue), step='checking'),
                   message=f'{index}/{len(queue)}: {title}')
        job.say(f'\n=== {index}/{len(queue)} | {execution_id} | {title} | {row["size_gb"]} GB ===')
        # Everything that could have changed since the job started, checked before each move.
        if read_api.get_manifest(base, run_id)['manifest_hash'] != manifest_hash:
            return job.finish(FAILED, 'The manifest changed during the job; stopped.')
        state = read_api.execution_state(base, execution_id, manifest_hash)
        if state['state'] == read_api.SUCCEEDED:
            job.record['skipped'].append(dict(execution_id=execution_id, title=title, reason='Already moved'))
            continue
        if state['state'] not in RUNNABLE_STATES or state['recovered']:
            return job.finish(FAILED, f'{title} needs attention ({state["state"]}); stopped before it.')
        if not approval_is_current(base, run_id, execution_id, manifest_hash, _require):
            job.record['skipped'].append(dict(execution_id=execution_id, title=title,
                                              reason='Approval was revoked'))
            job.say('SKIPPED: approval no longer current')
            continue
        problems = permissions(row['source_path'])
        if problems:
            reason = 'Folder permissions: ' + '; '.join(problems)
            job.record['skipped'].append(dict(execution_id=execution_id, title=title, reason=reason))
            job.say('SKIPPED (nothing changed): ' + reason)
            continue
        if snapshot() != config_before:
            return job.finish(FAILED, 'Runtime configuration changed during the job; stopped.')
        script = EXECUTORS[(row['media_type'], row['transfer_type'])]
        for live in (False, True):
            if live and job.stop_requested():
                return job.finish(STOPPED, f'Stopped as requested after checking {title}; it was not moved.')
            job.update(current=dict(job.record['current'], step='moving' if live else 'checking'))
            job.say(('MOVING' if live else 'CHECK ONLY') + ': ' + os.path.basename(script))
            command = module_command(script, execution_id, '--base', base) + (['--execute'] if live else [])
            code = runner(command, cwd=str(repo), env=_executor_env(base)).returncode
            if code != 0 and not live:
                # A check-only refusal changes nothing, so the next move can still run.
                after = read_api.execution_state(base, execution_id, manifest_hash)
                if after['state'] in (read_api.STOPPED_SAFE, read_api.CHECKED, read_api.NOT_STARTED):
                    reason = 'Check-only refused: ' + (after['detail'] or f'exit {code}')
                    job.record['skipped'].append(dict(execution_id=execution_id, title=title, reason=reason))
                    job.say('SKIPPED (nothing changed): ' + reason)
                    break
            if code != 0:
                return job.finish(FAILED, f'{title}: the executor stopped (exit {code}). Later moves were not '
                                          'started. Check its journal before retrying.')
        else:
            final = read_api.execution_state(base, execution_id, manifest_hash)
            if final['state'] != read_api.SUCCEEDED:
                return job.finish(FAILED, f'{title}: no matching SUCCESS in its journal; stopped.')
            job.record['done'].append(dict(execution_id=execution_id, title=title, size_gb=row['size_gb']))
        job.update()
    moved = len(job.record['done'])
    skipped = len(job.record['skipped'])
    return job.finish(FINISHED, f'{moved} moved' + (f', {skipped} skipped or held' if skipped else '') + '.')


def _require(ok, message):
    if not ok:
        raise JobError(message)


def _config_snapshot():
    from migratarr.runtime_config import get_config, load_config
    return load_config(get_config().source_path).as_dict()


def _executor_env(base):
    env = dict(os.environ, MIGRATARR_BASE_PATH=Path(base).as_posix())
    with contextlib.suppress(Exception):
        from migratarr.runtime_config import get_config
        env['MIGRATARR_CONFIG'] = str(get_config().source_path)
    return env


def run(argv=None):
    parser = argparse.ArgumentParser(description='Internal: run one UI job (started by migratarr_ui.py).')
    parser.add_argument('--base', type=Path, required=True)
    parser.add_argument('--job', required=True)
    parser.add_argument('kind', choices=['plan', 'run'])
    parser.add_argument('--run', dest='run_id')
    parser.add_argument('--id', dest='execution_ids', action='append', default=[])
    args = parser.parse_args(argv)
    if not valid_job_id(args.job):
        return 'Invalid job ID'
    folder = jobs_dir(args.base)
    folder.mkdir(exist_ok=True)
    with open(folder / 'runner.lock', 'a') as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('Another job is running; this one did not start.', flush=True)
            return 3
        job = Job(args.base, args.job, args.kind, args.run_id)
        job.update()
        job.say(f'JOB {args.job} ({args.kind}) started {job.record["started_utc"]}')
        try:
            if args.kind == 'plan':
                run_plan(job)
            else:
                run_moves(job, args.run_id, args.execution_ids or None)
        except Exception as exc:  # record why, keep files and journals as they are
            job.finish(FAILED, f'{type(exc).__name__}: {exc}')
        # Discord / webhook / email, per config/notifications.json (migratarr/notify.py); never raises.
        from migratarr import notify
        notify.job_finished(args.base, job.record, log=job.say)
    return 0


if __name__ == '__main__':
    if sys.argv[1:2] == ['run']:
        sys.exit(run(sys.argv[2:]))
    sys.exit('migratarr_jobs.py is started by migratarr_ui.py; it has no commands of its own.')
