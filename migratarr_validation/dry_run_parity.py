"""Record a dry-run planner's API traffic once, then replay any planner version.

`record` runs the current dry_run_movies.py or dry_run_tv.py for real (the
same read-only Radarr/Sonarr/Jellyfin/TMDB traffic and cache use as a normal
dry run) and saves every response it received, plus the instant it started.
`compare` replays that recording through a baseline and a candidate planner
with no network, Docker, cache or sleep, the clock frozen at the recorded
instant, and compares their CSVs byte for byte. API keys are never recorded.
"""
import argparse
import ast
import contextlib
import csv
import datetime as _dt
import hashlib
import io
import json
import os
import sys
from pathlib import Path
import tempfile
from unittest.mock import patch

from .capture import redirect_destinations

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = {'movie': 'dry_run_movies.py', 'tv': 'dry_run_tv.py'}
HOOKED = ('docker_output', 'request_json', 'cached')
IO_NAME = '__migratarr_io__'


def frozen_datetime(instant):
    class FrozenDatetime(_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return instant.astimezone(tz) if tz else instant.astimezone().replace(tzinfo=None)
    return FrozenDatetime


def _error(record):
    # Planners branch on "an exception happened" and may print type(e).__name__
    # and str(e); rebuild an exception that reproduces both.
    kind = type(record['type'], (Exception,), {})
    return kind(record['message'])


class Recorder:
    """Memoizing proxy: the first answer to each request is the only answer."""
    replay = False

    def __init__(self, instant):
        self.data = {'now': instant.isoformat(), 'requests': {}, 'cached': {}}
        self.datetime = frozen_datetime(instant)

    def _remember(self, table, key, call):
        if key not in table:
            try:
                table[key] = {'value': call()}
            except Exception as exc:
                table[key] = {'error': {'type': type(exc).__name__, 'message': str(exc)}}
        entry = table[key]
        if 'error' in entry:
            raise _error(entry['error'])
        return entry['value']

    def hook(self, name, real):
        if name == 'docker_output':
            return real  # API keys pass through and are never stored.
        if name == 'request_json':
            return lambda url, *a, **k: self._remember(self.data['requests'], url, lambda: real(url, *a, **k))
        return lambda namespace, key, loader: tuple(self._remember(
            self.data['cached'], f'{namespace}|{key}', lambda: list(real(namespace, key, loader))))


class Replayer:
    replay = True

    def __init__(self, recording):
        self.data = recording
        self.misses = []
        self.datetime = frozen_datetime(_dt.datetime.fromisoformat(recording['now']))

    def _lookup(self, table, key):
        if key not in self.data[table]:
            self.misses.append(f'{table}:{key}')
            raise LookupError('not in recording: ' + key)
        entry = self.data[table][key]
        if 'error' in entry:
            raise _error(entry['error'])
        return json.loads(json.dumps(entry['value']))  # fresh copy per call

    def hook(self, name, real):
        if name == 'docker_output':
            return lambda container, command: 'replay-placeholder-key'
        if name == 'request_json':
            return lambda url, *a, **k: self._lookup('requests', url)
        return lambda namespace, key, loader: tuple(self._lookup('cached', f'{namespace}|{key}'))


def instrument(source, filename, output, cache_dir):
    tree = ast.parse(source, filename=filename)
    tree = redirect_destinations(tree, output, cache_dir)
    body, hooked = [], set()
    for node in tree.body:
        body.append(node)
        if isinstance(node, ast.FunctionDef) and node.name in HOOKED:
            hooked.add(node.name)
            body.append(ast.parse(f'{node.name} = {IO_NAME}.hook({node.name!r}, {node.name})').body[0])
        elif isinstance(node, ast.ImportFrom) and node.module == 'datetime' and any(
                alias.name == 'datetime' and alias.asname is None for alias in node.names):
            body.append(ast.parse(f'datetime = {IO_NAME}.datetime').body[0])
    if hooked != set(HOOKED):
        raise ValueError(f'{filename}: missing I/O functions {sorted(set(HOOKED) - hooked)}')
    tree.body = body
    return compile(ast.fix_missing_locations(tree), filename, 'exec')


def run_planner(path, io_layer, output, cache_dir):
    code = instrument(Path(path).read_text(encoding='utf-8'), str(path), output, cache_dir)
    namespace = {'__name__': '__main__', '__file__': str(path), IO_NAME: io_layer}
    stdout = io.StringIO()
    with contextlib.ExitStack() as stack:
        # A live recording keeps the planner's progress output on stderr.
        stack.enter_context(contextlib.redirect_stdout(stdout if io_layer.replay else sys.stderr))
        if io_layer.replay:
            stack.enter_context(patch('time.sleep'))
            stack.enter_context(patch.dict(os.environ, {'TMDB_TOKEN': os.environ.get('TMDB_TOKEN', 'replay')}))
        exec(code, namespace)
    return stdout.getvalue()


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def record(kind, out, cache_dir=None, now=None):
    if out.exists():
        raise ValueError(f'Recording already exists: {out}')
    if not os.environ.get('TMDB_TOKEN'):
        raise ValueError('TMDB_TOKEN is required by the placement scripts')
    script = ROOT / SCRIPTS[kind]
    if cache_dir is None:
        from runtime_config import get_config
        cache_dir = get_config().base_path / 'cache'
    recorder = Recorder(now or _dt.datetime.now(_dt.timezone.utc))
    with tempfile.TemporaryDirectory() as temp:
        csv_path = Path(temp) / f'{kind}_dry_run.csv'
        run_planner(script, recorder, csv_path, cache_dir)
        csv_bytes = csv_path.read_bytes()
    recording = dict(recorder.data, kind=kind, script=SCRIPTS[kind],
                     script_sha256=sha256(script.read_text(encoding='utf-8').encode()),
                     csv_sha256=sha256(csv_bytes))
    with out.open('x', encoding='utf-8') as handle:
        json.dump(recording, handle, sort_keys=True)
    return {'recording': str(out), 'kind': kind, 'now': recording['now'],
            'requests': len(recording['requests']), 'cached': len(recording['cached']),
            'csv_rows': csv_bytes.count(b'\n') - 1, 'csv_sha256': recording['csv_sha256']}


def replay(path, recording):
    replayer = Replayer(recording)
    with tempfile.TemporaryDirectory() as temp:
        csv_path = Path(temp) / 'replay.csv'
        try:
            run_planner(path, replayer, csv_path, Path(temp) / 'cache')
        except Exception as exc:
            raise ValueError(f'{path} failed under replay: {type(exc).__name__}: {exc}') from exc
        return csv_path.read_bytes(), replayer.misses


def row_differences(old, new, limit=20):
    old_rows = list(csv.DictReader(io.StringIO(old.decode('utf-8'))))
    new_rows = list(csv.DictReader(io.StringIO(new.decode('utf-8'))))
    diffs = []
    for index, (a, b) in enumerate(zip(old_rows, new_rows)):
        changed = sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))
        if changed:
            diffs.append({'row': index + 1, 'title': a.get('title'),
                          'changed': {k: [a.get(k), b.get(k)] for k in changed}})
    return {'rows': [len(old_rows), len(new_rows)], 'rows_changed': len(diffs), 'sample': diffs[:limit]}


def compare(recording_path, baseline, candidate=None):
    recording = json.loads(Path(recording_path).read_text(encoding='utf-8'))
    candidate = candidate or ROOT / recording['script']
    old, old_misses = replay(baseline, recording)
    new, new_misses = replay(candidate, recording)
    identical = old == new and not old_misses and not new_misses
    code_sha = lambda p: sha256(Path(p).read_text(encoding='utf-8').encode())
    result = {'byte_identical': identical, 'kind': recording['kind'], 'recorded_now': recording['now'],
              'baseline_code_sha256': code_sha(baseline), 'candidate_code_sha256': code_sha(candidate),
              'baseline_csv_sha256': sha256(old), 'candidate_csv_sha256': sha256(new),
              # The same code replayed must reproduce the recording run's own CSV.
              'recording_reproduced': {
                  'baseline': code_sha(baseline) != recording['script_sha256'] or sha256(old) == recording['csv_sha256'],
                  'candidate': code_sha(candidate) != recording['script_sha256'] or sha256(new) == recording['csv_sha256']},
              'replay_misses': {'baseline': old_misses[:20], 'candidate': new_misses[:20]}}
    if old != new:
        result['differences'] = row_differences(old, new)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    rec = sub.add_parser('record', help='run the current planner for real and save its API traffic')
    rec.add_argument('--kind', choices=SCRIPTS, required=True)
    rec.add_argument('--out', type=Path, required=True)
    rec.add_argument('--cache-dir', type=Path, help='planner cache (default: <base_path>/cache)')
    cmp = sub.add_parser('compare', help='replay a recording through two planner versions')
    cmp.add_argument('--recording', type=Path, required=True)
    cmp.add_argument('--baseline', type=Path, required=True)
    cmp.add_argument('--candidate', type=Path, help='default: the repository planner of the recorded kind')
    args = parser.parse_args(argv)
    try:
        if args.command == 'record':
            result = record(args.kind, args.out, args.cache_dir)
        else:
            result = compare(args.recording, args.baseline, args.candidate)
    except (OSError, ValueError, KeyError) as exc:
        parser.exit(2, f'dry-run parity could not complete: {exc}\n')
    print(json.dumps(result, indent=2))
    if args.command == 'compare' and not (result['byte_identical']
                                          and all(result['recording_reproduced'].values())):
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
