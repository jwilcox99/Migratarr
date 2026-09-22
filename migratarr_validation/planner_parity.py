"""Read-only byte comparison of the pinned pre-target planner and candidate."""
import argparse
import ast
import csv
import hashlib
from io import StringIO
import json
from pathlib import Path
from unittest.mock import patch

from runtime_config import load_config
from storage_targets import load_targets
from .parity import PLANNER, _assigned_name, _execute, load_legacy, load_overrides, run_legacy


def csv_bytes(tree, rows):
    namespace = {}
    _execute([n for n in tree.body if _assigned_name(n) == 'FIELDS'], namespace)
    output = StringIO(newline='')
    writer = csv.DictWriter(output, fieldnames=namespace['FIELDS'])
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode('utf-8')


def compare_planners(baseline, movie_csv, tv_csv, overrides, runtime, targets, facts=None):
    """Memoize read-only observations, or replay them without touching NAS paths."""
    baseline_hash = hashlib.sha256(baseline.read_text(encoding='utf-8').encode()).hexdigest()
    if baseline_hash != '9f01c24ee8b50aa375641b2d1447aa0f7438ae261cef01da1642b2c6f20c8009':
        raise ValueError('Baseline must be the planner at c31afc6')
    old_tree, old = load_legacy(runtime, planner=baseline)
    new_tree, new = load_legacy(runtime, targets=targets)
    replay = facts is not None
    facts = facts if replay else {'exists': {}, 'resolve': {}, 'size': {}, 'free': {}}
    original_exists, original_resolve = Path.exists, Path.resolve

    def observed(kind, path, probe):
        key = str(path)
        if key not in facts[kind]:
            if replay:
                raise ValueError(f'Missing frozen observation: {kind} {key}')
            facts[kind][key] = probe(path)
        return facts[kind][key]

    exists = lambda p: observed('exists', p, original_exists)
    resolve = lambda p: Path(observed('resolve', p, lambda q: str(original_resolve(q))))
    size_probe, free_probe = old['dir_size_bytes'], old['free_bytes']
    for ns in (old, new):
        ns['dir_size_bytes'] = lambda p: observed('size', p, size_probe)
        ns['free_bytes'] = lambda p: observed('free', p, free_probe)
    with patch.object(Path, 'exists', exists), patch.object(Path, 'resolve', resolve):
        old_rows = run_legacy(old_tree, old, movie_csv, tv_csv, overrides)
        new_rows = run_legacy(new_tree, new, movie_csv, tv_csv, overrides)
    old_bytes, new_bytes = csv_bytes(old_tree, old_rows), csv_bytes(new_tree, new_rows)
    digest = lambda value: hashlib.sha256(value).hexdigest()
    return {'byte_identical': old_bytes == new_bytes,
            'baseline_rows': len(old_rows), 'candidate_rows': len(new_rows),
            'baseline_csv_sha256': digest(old_bytes), 'candidate_csv_sha256': digest(new_bytes),
            'baseline_code_sha256': digest(baseline.read_bytes()),
            'candidate_code_sha256': digest(PLANNER.read_bytes()),
            'facts_sha256': digest(json.dumps(facts, sort_keys=True).encode()),
            'input_sha256': {str(p): digest(p.read_bytes()) for p in (movie_csv, tv_csv)}}, facts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('baseline', 'movie-csv', 'tv-csv', 'overrides-json', 'runtime-config', 'targets'):
        parser.add_argument('--' + name, type=Path, required=True)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--capture-facts', type=Path)
    group.add_argument('--replay-facts', type=Path)
    args = parser.parse_args()
    # Capture outputs are exclusive; refuse before any filesystem observation.
    if args.capture_facts and args.capture_facts.exists():
        parser.error('capture-facts already exists')
    try:
        result, facts = compare_planners(
            args.baseline, args.movie_csv, args.tv_csv, load_overrides(args.overrides_json),
            load_config(args.runtime_config, environ={}), load_targets(args.targets),
            json.loads(args.replay_facts.read_text()) if args.replay_facts else None)
        for name in ('overrides_json', 'runtime_config', 'targets'):
            path = getattr(args, name)
            result['input_sha256'][str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
        if args.capture_facts:
            with args.capture_facts.open('x', encoding='utf-8') as handle:
                json.dump(facts, handle, indent=2, sort_keys=True)
                handle.write('\n')
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        parser.exit(2, f'Planner parity could not complete: {exc}\n')
    print(json.dumps(result, indent=2))
    return 0 if result['byte_identical'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
