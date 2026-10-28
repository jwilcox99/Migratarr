"""run(): one approved move, for any of the four pairings (unified-execution-core §3, gate 3).

The four execute_*.py scripts are now thin wrappers (D6): each keeps its
arguments and stdout, the helpers that still differ per executor or are
shipped to the NAS byte for byte (inventory, progress, run_progress, paths,
load_plan, nas_pair, remote_layout, remote_program, NasTransport.call), and
calls wire() once at import and run() from main().

`ns` is the executor's own namespace, read at call time (Names), so a test
or a recovery script that patches an executor global is honored exactly as
when every step was written out in that file.
"""
import os
from pathlib import Path
import re
import sys

from migratarr.engine import adapters, arr, journal, primitives
from migratarr.engine.events import ConsoleSink, ExecutionResult, JournalSink, Log
from migratarr.engine.nas_side import require
from migratarr.engine.strategies import STRATEGIES

SERVICE = {'Movie': 'radarr', 'TV': 'sonarr'}


class Names:
    """Attribute view of an executor's globals, looked up on every access."""
    __slots__ = ('_namespace',)

    def __init__(self, namespace):
        object.__setattr__(self, '_namespace', namespace)

    def __getattr__(self, name):
        try:
            return self._namespace[name]
        except KeyError:
            raise AttributeError(name) from None


def wire(namespace, media, transfer):
    """Install the engine's per-pairing pieces into an executor's globals.

    Adds: execute(), check_journal(), the Radarr/Sonarr client class, and the
    Arr helpers that executor used to define (episode_state and
    verify_episode_contents for TV, verify_movie for cross-disk Movie).
    """
    ns = Names(namespace)
    service = SERVICE[media]
    uncertain, message = journal.UNCERTAIN[(media, transfer)]
    strategy = STRATEGIES[transfer]

    if (media, transfer) in journal.RESUMABLE:
        def check_journal(journal_path, resume=False):
            return journal.check_journal(journal_path, uncertain, message,
                                         journal.nfs_refusal_recovery if resume else None)

        def execute(base, execution_id, live, client, log, transport=None, recovery=None):
            return strategy(ns, media, client, log, transport).execute(base, execution_id, live, recovery)
    else:
        def check_journal(journal_path):
            return journal.check_journal(journal_path, uncertain, message)

        if strategy.cross:
            def execute(base, execution_id, live, client, log, transport):
                return strategy(ns, media, client, log, transport).execute(base, execution_id, live)
        else:
            def execute(base, execution_id, live, client, log, transport=None):
                return strategy(ns, media, client, log, transport).execute(base, execution_id, live)

    for function in (check_journal, execute):
        function.__module__ = namespace['__name__']
    namespace.update(check_journal=check_journal, execute=execute)
    namespace[service.capitalize()] = arr.client(service, ns)
    helpers = [adapters.episode_state, adapters.verify_episode_contents] if media == 'TV' else []
    if media == 'Movie' and strategy.cross:
        helpers.append(adapters.verify_movie)
    primitives.bind(namespace, *helpers)
    return ns


def run(ns, execution_id, *, base, live, media, transfer, resume=False, announce=None):
    """One attempt; returns an ExecutionResult once the journal holds START.

    Refusals before the journal is opened (wrong platform or ID, the host lock,
    an uncertain earlier attempt) raise, as before. From START on, any
    exception is journaled as STOPPED and returned, never re-raised: no
    automatic rollback or retry. `announce(status)` runs where the scripts
    printed their result line, so a failure there is journaled as before.
    """
    require(sys.platform.startswith('linux'), 'Run on the Linux media host')
    import fcntl
    require(re.fullmatch(r'\d{8}T\d{6}Z-\d{4,}', execution_id), 'Invalid execution ID')
    logs = Path(base) / 'execution_logs'
    logs.mkdir(exist_ok=True)
    resumable = (media, transfer) in journal.RESUMABLE
    with (logs / 'executor.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        journal_path = logs / (execution_id + '.jsonl')
        extra = (ns.check_journal(journal_path, resume),) if resumable else ()
        if not resumable:
            ns.check_journal(journal_path)
        with journal_path.open('a', encoding='utf-8') as stream:
            log = Log([ConsoleSink(lambda message: ns.progress(message)), JournalSink(stream, execution_id)])
            # Persist the journal directory entry before any media mutation.
            fd = os.open(logs, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            try:
                log('START', live=live)
                with ns.NasTransport() as transport:
                    client = getattr(ns, SERVICE[media].capitalize())()
                    status = ns.execute(base, execution_id, live, client, log, transport, *extra)
                if announce is not None:
                    announce(status)
                return ExecutionResult(status, execution_id, log.last_event, None, None, journal_path)
            except BaseException as exc:
                log('STOPPED', error_type=type(exc).__name__, reason=str(exc))
                return ExecutionResult('STOPPED', execution_id, 'STOPPED', type(exc).__name__, str(exc),
                                       journal_path)


def main(ns, args, media, transfer, resume=False):
    """The scripts' shared main(): result line on stdout, STOPPED lines on stderr, exit code."""
    result = run(ns, args.execution_id, base=args.base, live=args.execute, media=media, transfer=transfer,
                 resume=resume, announce=lambda status: print(status + ': ' + args.execution_id))
    if result.status == 'STOPPED':
        print('STOPPED: ' + result.reason, file=sys.stderr)
        print('Inspect journal: ' + str(result.journal_path) + '. No automatic rollback or retry.',
              file=sys.stderr)
        return 1
    return 0
