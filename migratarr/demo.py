"""migratarr demo: try Migratarr without Radarr, Sonarr, Jellyfin or TMDB.

    migratarr demo [--dir FOLDER] [--port 8767] [--no-ui]

Builds a throwaway setup in FOLDER (default: a new temporary folder):

- settings for "this machine's own disks", with two small local disks holding an
  empty folder for each title of a made-up library (migratarr/demo_library.py);
- a plan made by the real planning pipeline (the same steps as `migratarr plan`),
  run in this process against the made-up library: nothing is sent over the network
  and nothing is read from Docker;

then opens the web UI read-only on it, so the plan can be browsed but nothing can be
approved or moved. Your own settings and data folder are never read or written.
Delete FOLDER when done. Standard library only.
"""
import argparse
import io
from contextlib import contextmanager
import json
import os
from pathlib import Path
import runpy
import sys
import tempfile
import time
import urllib.request
from urllib.parse import urlsplit

from migratarr import demo_library as library

DISKS = {  # disk -> (Movie categories, TV categories) it holds
    'demo01': (('Common', 'Rare'), ('Current', 'Rare')),
    'demo02': (('Library', 'Archive'), ('Library', 'Archive')),
}
FOLDERS = {
    'Movie': {'Common': 'Movies/Common', 'Rare': 'Movies/Rare', 'Library': 'Movies/Library',
              'Archive': 'Movies/Archive'},
    'TV': {'Current': 'TV/Current', 'Rare': 'TV/Rare', 'Library': 'TV/Library', 'Archive': 'TV/Archive'},
}
KEYS = {'radarr': 'MIGRATARR_DEMO_RADARR_KEY', 'sonarr': 'MIGRATARR_DEMO_SONARR_KEY',
        'jellyfin': 'MIGRATARR_DEMO_JELLYFIN_KEY', 'tmdb': 'MIGRATARR_DEMO_TMDB_TOKEN'}
# Settings the demo replaces for its own process, so the user's own files are never used.
CLEARED = ('MIGRATARR_CONFIG', 'MIGRATARR_PLANNER_CONFIG', 'MIGRATARR_STORAGE_TARGETS', 'MIGRATARR_BASE_PATH',
           'MIGRATARR_NAS_HOST', 'MIGRATARR_NAS_USER', 'MIGRATARR_NAS_SSH_KEY')


def settings(home):
    disks = home / 'disks'
    runtime = {
        'schema_version': 2, 'base_path': str(home), 'topology': 'local', 'containers': {},
        'urls': {'radarr': 'http://radarr.demo.invalid:7878', 'sonarr': 'http://sonarr.demo.invalid:8989',
                 'jellyfin': 'http://jellyfin.demo.invalid:8096'},
        'secrets': {service: {'source': 'env', 'variable': variable} for service, variable in KEYS.items()},
        # No containers: Radarr and Sonarr would see files directly (the demo never moves anything).
        'arr_file_checks': {'radarr': {'mode': 'host'}, 'sonarr': {'mode': 'host'}},
    }
    placement = {media: {} for media in FOLDERS}
    for disk, holds in DISKS.items():
        for media, categories in zip(FOLDERS, holds):
            for category in categories:
                placement[media].setdefault(category, []).append(disk)
    targets = {
        'schema_version': 1, 'defaults': {'minimum_free_space_gb': 0},
        'targets': [{'id': disk, 'name': 'Demo disk ' + disk[-1], 'path': str(disks / disk), 'enabled': True,
                     'media_types': ['Movie', 'TV']} for disk in DISKS],
        'category_paths': FOLDERS, 'placement': placement,
    }
    planner = {'schema_version': 1, 'streaming': {'region': 'US', 'subscribed': ['Hulu', 'Peacock'],
                                                  'user_free_access': []}}
    return runtime, targets, planner


def library_folders():
    """(media, category, folder name) for every title, as Radarr and Sonarr report them."""
    for movie in library.radarr_movies():
        yield 'Movie', movie['path'].split('/')[3] if movie['path'].startswith('/media/Movies/') else None, \
            movie['path'].rsplit('/', 1)[1]
    for series in library.sonarr_series():
        yield 'TV', series['path'].split('/')[3], series['path'].rsplit('/', 1)[1]


def build(home):
    """Write the demo's settings and disks into ``home`` (which must be empty or missing)."""
    home = Path(home).expanduser().resolve()  # settings store absolute paths; "--dir demo" is relative
    if home.exists() and any(home.iterdir()):
        raise SystemExit(f'{home} is not empty; choose an empty or new folder for the demo.')
    runtime, targets, planner = settings(home)
    config = home / 'config'
    config.mkdir(parents=True)
    for name, data in (('runtime.json', runtime), ('storage-targets.json', targets), ('planner.json', planner)):
        (config / name).write_text(json.dumps(data, indent=2) + '\n', encoding='utf-8')
    disks = home / 'disks'
    for disk, holds in DISKS.items():
        for media, categories in zip(FOLDERS, holds):
            for category in FOLDERS[media]:
                (disks / disk / FOLDERS[media][category]).mkdir(parents=True, exist_ok=True)
    for media, category, name in library_folders():
        if category is None:  # a title outside every category folder: the planner reports it
            continue
        disk = next(d for d, holds in DISKS.items() if category in holds[0 if media == 'Movie' else 1])
        folder = disks / disk / FOLDERS[media][category] / name
        folder.mkdir(parents=True, exist_ok=True)
        (folder / ('placeholder.mkv' if media == 'Movie' else 'S01E01.mkv')).write_bytes(b'\0' * 4096)
    for path in [disks, *disks.rglob('*')]:
        if path.is_dir():
            path.chmod(0o775)  # the planner checks group write, as on a real library
    return home


@contextmanager
def swapped(*changes):
    """Temporarily set attributes: (object, name, value)."""
    saved = [(obj, name, getattr(obj, name)) for obj, name, _ in changes]
    for obj, name, value in changes:
        setattr(obj, name, value)
    try:
        yield
    finally:
        for obj, name, value in saved:
            setattr(obj, name, value)


def use_demo_environment(home):
    for name in CLEARED:
        os.environ.pop(name, None)
    os.environ.update({'MIGRATARR_HOME': str(home), 'MIGRATARR_SECRETS_DIR': str(Path(home) / 'secrets'),
                       **{variable: 'demo-' + service for service, variable in KEYS.items()}})
    from migratarr.planner_settings import get_settings
    from migratarr.runtime_config import get_config
    get_config.cache_clear()
    get_settings.cache_clear()


def run_step_here(repo, label, args, number, total):
    """migratarr.plan's step runner, in this process (where the made-up library answers)."""
    from migratarr import plan
    from migratarr.paths import module_name
    plan.banner(f'[{number}/{total}] {label}: {" ".join(args)}')
    saved = sys.argv
    sys.argv = [args[0], *args[1:]]
    code = 0
    try:
        runpy.run_module(module_name(args[0]), run_name='__main__')
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
        if exc.code is not None and not isinstance(exc.code, int):
            print(exc.code, file=sys.stderr)
    finally:
        sys.argv = saved
    print(f'[{number}/{total}] {label}: {"OK" if code == 0 else "FAILED (exit %d)" % code}', flush=True)
    return code


def demo_summary(real_summary, home):
    """migratarr.plan's summary without its "Next:" commands, which would act on the user's own
    data folder (approve, ui), not the demo's. Points at the read-only UI on the demo instead."""
    def summary(base, run_id):
        text = real_summary(base, run_id).split('\nEvery row is unapproved')[0].rstrip()
        return (f'{text}\n\nThis is a demo: nothing has moved, and nothing in it can be approved or moved.\n'
                f'Browse it again with:\n  MIGRATARR_HOME={home} migratarr ui --read-only')
    return summary


def demo_urlopen(request, timeout=None):
    """The made-up library's network, except that TMDB answers "no providers" where the tests'
    library simulates an outage: a demo shouldn't print retry warnings for a service it fakes."""
    try:
        return library.fake_urlopen(request, timeout)
    except OSError:
        if urlsplit(request.full_url).hostname != 'api.themoviedb.org':
            raise
        return io.BytesIO(b'{"results": {}}')


def make_plan(home):
    """Run the planning pipeline against the made-up library. Returns migratarr.plan's exit code."""
    from migratarr import plan
    with swapped((urllib.request, 'urlopen', demo_urlopen), (time, 'sleep', lambda seconds: None),
                 (plan, 'run_step', run_step_here), (plan, 'summary', demo_summary(plan.summary, home)),
                 # The preflight's health checks would contact the real services; the demo has none.
                 (plan, 'preflight', lambda repo, allow_dirty: ([], Path(home)))):
        return plan.main(['--allow-dirty'])


def main(argv=None):
    parser = argparse.ArgumentParser(prog='migratarr demo', description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--dir', type=Path, help='an empty or new folder for the demo (default: a temporary one)')
    parser.add_argument('--port', type=int, default=8767, help='port for the read-only UI (default 8767)')
    parser.add_argument('--no-ui', action='store_true', help='make the plan, print where it is, and stop')
    args = parser.parse_args(argv)
    home = build(args.dir or tempfile.mkdtemp(prefix='migratarr-demo-')).resolve()
    use_demo_environment(home)
    print(f'Migratarr demo in {home} (delete it when done). Planning a made-up library; nothing leaves '
          'this machine.', flush=True)
    code = make_plan(home)
    if code != 0:
        print('The demo plan failed; see above. Please report it.', file=sys.stderr)
        return code
    if args.no_ui:
        return 0
    from migratarr import ui
    print('\nOpening the plan read-only: browse it, nothing can be approved or moved.', flush=True)
    return ui.main(['--read-only', '--port', str(args.port), '--base', str(home)])


if __name__ == '__main__':
    sys.exit(main())
