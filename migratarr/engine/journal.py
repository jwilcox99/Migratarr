"""Journal checks before a run (D2): each pairing's "uncertain live attempt" events.

A journal that holds any of its pairing's uncertain events means an earlier
live attempt may have changed media or Arr state, so a new attempt is refused
until someone reconciles it by hand. The sets differ per pairing exactly as
they did in the four executors; they are kept, not unified (D2, D8).
"""
import json

from migratarr.engine.nas_side import require

SAME_DISK, CROSS_DISK = 'SAME_DISK_RENAME', 'CROSS_DISK_TRANSFER'
RECONCILE = 'Previous live attempt exists; reconcile manually before retrying'

UNCERTAIN = {
    ('Movie', SAME_DISK): (frozenset({'RENAME_INTENT', 'SUCCESS'}),
                           'Previous live attempt exists; inspect journal and reconcile manually before retrying'),
    ('TV', SAME_DISK): (frozenset({'RENAME_INTENT', 'RENAMED', 'SONARR_UPDATE_INTENT', 'SUCCESS'}), RECONCILE),
    ('Movie', CROSS_DISK): (frozenset({'COPY_INTENT', 'COPIED', 'RADARR_UPDATE_INTENT', 'DELETE_INTENT',
                                       'SOURCE_REMOVED', 'SUCCESS', 'RENAME_INTENT'}), RECONCILE),
    ('TV', CROSS_DISK): (frozenset({'COPY_INTENT', 'COPIED', 'SONARR_UPDATE_INTENT', 'DELETE_INTENT',
                                    'SOURCE_REMOVED', 'SUCCESS', 'RENAME_INTENT'}), RECONCILE),
}

# Only same-disk Movie can resume, and only the one diagnosed NFS EINVAL failure (D8).
RESUMABLE = frozenset({('Movie', SAME_DISK)})


def read_events(journal):
    # Truncated JSON raises rather than being taken for a fresh journal.
    return [json.loads(line) for line in journal.read_text().splitlines()]


def check_journal(journal, uncertain, message, resume=None):
    """Refuse a journal with uncertain events; with `resume`, return what it finds instead."""
    if journal.exists():
        events = read_events(journal)
        if resume is not None:
            return resume(events)
        require(not any(e['event'] in uncertain for e in events), message)
    require(resume is None, 'No failed journal to recover')


def nfs_refusal_recovery(events):
    """(failed attempt's PREFLIGHT_OK, its RENAME_INTENT) for `--resume-nfs-refusal`."""
    intents = [i for i, e in enumerate(events) if e['event'] == 'RENAME_INTENT']
    require(len(intents) == 1, 'Recovery requires exactly one previous rename intent')
    index = intents[0]
    require(index > 0 and index + 1 < len(events), 'Incomplete failed-attempt journal')
    failure = events[index + 1]
    require(failure.get('event') == 'STOPPED' and failure.get('error_type') == 'OSError'
            and failure.get('reason') == '[Errno 22] Invalid argument',
            'Recovery only supports the diagnosed NFS EINVAL refusal')
    require(not any(e['event'] in {'RENAMED', 'RADARR_UPDATE_INTENT', 'SUCCESS'} for e in events),
            'Journal indicates a completed or uncertain mutation')
    # Only successful check-only recovery runs may follow the original failure.
    tail = events[index + 2:]
    allowed = {'START', 'NFS_RECOVERY_VERIFIED', 'PREFLIGHT_OK', 'CHECK_ONLY'}
    require(all(e['event'] in allowed for e in tail)
            and (not tail or tail[-1]['event'] == 'CHECK_ONLY')
            and not any(e['event'] == 'START' and e.get('live') is not False for e in tail),
            'A later attempt needs manual reconciliation')
    prior = [e for e in events[:index] if e['event'] == 'PREFLIGHT_OK']
    require(prior, 'Missing failed-attempt preflight')
    return prior[-1], events[index]
