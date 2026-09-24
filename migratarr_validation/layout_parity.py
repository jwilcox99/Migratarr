"""Replay manifest rows through pre-media_layout and current executor path checks.

Read-only: parses every <base>/manifests/<run>/execution_manifest.csv (no
checksum or approval requirement, since old runs are evidence, not work to do)
and, for each row, compares the pinned executors' paths()/remote_path() and
NAS-side path checks with the current ones. Nothing touches media, Arr or the
NAS. Baselines live in tests/fixtures/*_before_media_layout.py.
"""
import argparse
import ast
import csv
import importlib
import json
from pathlib import Path, PurePath, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / 'tests' / 'fixtures'
MEDIA = {'Movie': ('Movies', ('Common', 'Rare', 'Library', 'Archive')),
         'TV': ('TV', ('Current', 'Rare', 'Library', 'Archive'))}
# (media_type, transfer_type) -> executors that accept such a row.
EXECUTORS = {('Movie', 'SAME_DISK_RENAME'): ('execute_movie', 'execute_movie_nas'),
             ('TV', 'SAME_DISK_RENAME'): ('execute_tv_nas',),
             ('Movie', 'CROSS_DISK_TRANSFER'): ('execute_cross_movie',),
             ('TV', 'CROSS_DISK_TRANSFER'): ('execute_cross_tv',)}


class OldRefused(Exception):
    pass


def old_require(ok, message):
    if not ok:
        raise OldRefused(message)


def old_functions(name, runtime, wanted=('paths', 'remote_path')):
    """The pinned executor's own functions, executed against `runtime`."""
    path = FIXTURES / f'{name}_before_media_layout.py'
    tree = ast.parse(path.read_text(encoding='utf-8'))
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in wanted]
    namespace = {'RUNTIME': runtime, 'DISKS': runtime.remote_disks, 'require': old_require,
                 'Path': Path, 'PurePosixPath': PurePosixPath}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), 'exec'), namespace)
    return namespace


def old_nas_cross(source, destination, media, remote_disks):
    """execute_cross_movie.py:452-456 / execute_cross_tv.py:491-495 at e11681b, verbatim
    but for Path -> PurePosixPath (the same class on the Linux NAS)."""
    folder, cats = MEDIA[media]
    message = 'Invalid NAS movie path' if media == 'Movie' else 'Invalid NAS TV path'
    src, dst = PurePosixPath(source), PurePosixPath(destination)
    for p in (src, dst):
        old_require(len(p.parts) == 6 and str(PurePosixPath(*p.parts[:3])) in remote_disks.values()
                    and p.parts[3] == folder and p.parts[4] in set(cats)
                    and '..' not in p.parts, message)
    old_require(src.name == dst.name and src.parts[:3] != dst.parts[:3], 'Not a cross-disk pair')


def old_nas_same(source, destination, root, media):
    """execute_movie_nas.py:318-323 / execute_tv_nas.py:340-345 at e11681b (template code)."""
    folder, cats = MEDIA[media]
    message = 'Invalid NAS movie path' if media == 'Movie' else 'Invalid NAS TV path'
    src, dst = PurePosixPath(source), PurePosixPath(destination)
    for p in (src, dst):
        old_require(len(p.parts) == 6 and p.parts[:4] == (PurePosixPath(root) / folder).parts
                    and p.parts[4] in set(cats) and '..' not in p.parts, message)
    old_require(src != dst and src.name == dst.name, 'Invalid rename pair')


def outcome(function, *args):
    try:
        return ['ok', function(*args)]
    except Exception as exc:  # each executor raises its own Refused type
        if type(exc).__name__ not in {'Refused', 'OldRefused'}:
            raise
        return ['refused', str(exc)]


def posix(value):
    return value.as_posix() if isinstance(value, PurePath) else value


def compare_row(name, row, old, new, runtime):
    """List of differences between old and new handling of one manifest row."""
    media = row['media_type']
    expected, actual = outcome(old['paths'], row), outcome(new.paths, row)
    normalize = lambda r: [r[0], [posix(v) for v in r[1]] if r[0] == 'ok' else r[1]]
    expected, actual = normalize(expected), normalize(actual)
    diffs = [] if expected == actual else [{'check': 'paths', 'old': expected, 'new': actual}]
    if expected[0] != 'ok' or actual[0] != 'ok' or name == 'execute_movie':
        return diffs, expected[0] == 'ok'
    remotes = []
    for local in expected[1][:2]:
        a, b = outcome(old['remote_path'], local), outcome(new.remote_path, local)
        if a != b:
            diffs.append({'check': 'remote_path', 'path': local, 'old': a, 'new': b})
        remotes.append(a[1] if a[0] == 'ok' else None)
    if None not in remotes:
        if name.startswith('execute_cross'):
            a = outcome(old_nas_cross, *remotes, media, runtime.remote_disks)
            b = outcome(new.nas_pair, *remotes, new.remote_layout())
        else:
            disk = row['source_disk']
            a = outcome(old_nas_same, *remotes, runtime.remote_disks[disk], media)
            b = outcome(new.nas_pair, *remotes, new.remote_layout(disk))
        if a[0] != b[0] or (a[0] == 'refused' and a[1] != b[1]):
            diffs.append({'check': 'nas_pair', 'paths': remotes, 'old': a, 'new': [b[0], str(b[1])]})
    return diffs, True


def replay_manifests(base, runtime):
    modules = {name: importlib.import_module(name) for names in EXECUTORS.values() for name in names}
    olds = {name: old_functions(name, runtime) for name in modules}
    report = {'manifests': 0, 'rows': 0, 'checks': 0, 'accepted': 0, 'refused': 0, 'differences': [],
              'skipped_rows': 0}
    for manifest in sorted((Path(base) / 'manifests').glob('*/execution_manifest.csv')):
        report['manifests'] += 1
        with manifest.open(newline='', encoding='utf-8-sig') as handle:
            for row in csv.DictReader(handle):
                report['rows'] += 1
                names = EXECUTORS.get((row.get('media_type'), row.get('transfer_type')))
                if not names:
                    report['skipped_rows'] += 1
                    continue
                for name in names:
                    diffs, accepted = compare_row(name, row, olds[name], modules[name], runtime)
                    report['checks'] += 1
                    report['accepted' if accepted else 'refused'] += 1
                    for diff in diffs:
                        diff.update(manifest=manifest.parent.name, execution_id=row.get('execution_id'),
                                    executor=name)
                        report['differences'].append(diff)
    report['identical'] = not report['differences'] and report['checks'] > 0
    report['differences'] = report['differences'][:50]
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', type=Path, help='deployment base path (default: runtime.json base_path)')
    args = parser.parse_args(argv)
    from runtime_config import get_config
    runtime = get_config()
    report = replay_manifests(args.base or runtime.base_path, runtime)
    print(json.dumps(report, indent=2, default=str))
    return 0 if report['identical'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
