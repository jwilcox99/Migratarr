#!/usr/bin/env python3
"""Throwaway Radarr, Sonarr and Jellyfin for a first real local-disk move. See README.md.

    ./kit.py init            create the test folder tree under $MTEST_ROOT; disk2 is a
                             loop-mounted ext4 image (sudo mount), so it is a separate filesystem
    ./kit.py up              re-mount disk2 if needed (after a reboot), start the test containers
                             and wait until they answer
    ./kit.py seed            root folders, tags, small dummy titles, Jellyfin API key
    ./kit.py wizard-values   what to type into Migratarr's setup wizard
    ./kit.py check           prove the test Migratarr settings point only at the test kit
    ./kit.py status          where each test title is: in Radarr/Sonarr and on the test disks
    ./kit.py report          write everything to send back to ~/mtest-report-<time>.txt
    ./kit.py down [--delete] stop and remove the containers; --delete also unmounts disk2
                             and removes $MTEST_ROOT

Settings come from the environment: MTEST_ROOT (required, absolute), MTEST_PUID and
MTEST_PGID (default: your user's uid and primary gid), TZ (optional).

Touches nothing outside $MTEST_ROOT and the containers of compose project "mtest". The only
commands run with sudo are mount/umount of $MTEST_ROOT/disk2.img on $MTEST_ROOT/disk2, and
chown/rmdir lost+found on that new disk; each is printed, with the reason, before it runs.
Standard library only; Python 3.10 or newer.
"""
import argparse
import json
import os
from pathlib import Path, PurePosixPath
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

KIT = Path(__file__).resolve().parent
COMPOSE = KIT / 'compose.yaml'
PROJECT = 'mtest'
MARKER = '.migratarr-test-kit'
ARR_ROOT = '/media'
UI_PORT = 8766
SERVICES = {  # service: (container, host port, port inside the container)
    'radarr': ('mtest-radarr', 17878, 7878),
    'sonarr': ('mtest-sonarr', 18989, 8989),
    'jellyfin': ('mtest-jellyfin', 18096, 8096),
}
DISKS = ('disk1', 'disk2')
# disk2 is an ext4 image loop-mounted on $MTEST_ROOT/disk2. Migratarr's engine refuses a
# cross-disk move between two folders on one filesystem (same st_dev), so plain folders
# could only ever test same-disk renames.
LOOP_DISK = 'disk2'
LOOP_IMAGE = 'disk2.img'
DEFAULT_IMAGE_SIZE = '2G'
MIN_IMAGE_SIZE = 64 * 1024 * 1024
CATEGORIES = {'Movies': ('Common', 'Rare', 'Library', 'Archive'),
              'TV': ('Current', 'Rare', 'Library', 'Archive')}
# Which test disk holds each category folder. compose.yaml mounts exactly these.
LAYOUT = {
    'radarr': {'Movies/Common': 'disk1', 'Movies/Rare': 'disk1',
               'Movies/Library': 'disk2', 'Movies/Archive': 'disk2'},
    'sonarr': {'TV/Current': 'disk2', 'TV/Rare': 'disk2',
               'TV/Library': 'disk1', 'TV/Archive': 'disk1'},
}
START = {'radarr': 'Movies/Common', 'sonarr': 'TV/Current'}  # where seeded titles start
LOCK = 'migratarr-lock'
# (title, year, tag). The tag forces the planner's recommendation (docs/planner-settings.md):
# -rare stays on the same disk (rename), -library goes to the other disk (transfer), lock never moves.
MOVIES = (('Night of the Living Dead', 1968, 'migratarr-rare'),
          ('Nosferatu', 1922, 'migratarr-library'),
          ('The General', 1926, LOCK))
SERIES = (('The Twilight Zone', 1959, 'migratarr-rare'),
          ('Bonanza', 1959, 'migratarr-library'),
          ('The Andy Griffith Show', 1960, LOCK))
SEED_TAG = 'mtest-seeded'
JELLYFIN_USER = 'mtest-admin'
JELLYFIN_APP = 'migratarr-test-kit'
FFMPEG = '/usr/lib/jellyfin-ffmpeg/ffmpeg'  # inside lscr.io/linuxserver/jellyfin
WAIT = 240  # seconds


class KitError(Exception):
    pass


def say(text=''):
    print(text, flush=True)


# ---------------------------------------------------------------- settings

def root():
    value = os.environ.get('MTEST_ROOT', '')
    if not value:
        raise KitError('Set MTEST_ROOT first, e.g.  export MTEST_ROOT="$HOME/migratarr-test"')
    path = Path(value).expanduser()
    if not path.is_absolute() or os.path.normpath(str(path)) != str(path).rstrip('/'):
        raise KitError(f'MTEST_ROOT must be a plain absolute path (got {value!r})')
    if path in (Path('/'), Path.home()) or len(path.parts) < 3:
        raise KitError(f'MTEST_ROOT={path} is too high up; use a new folder such as $HOME/migratarr-test')
    return path


def ids():
    return (int(os.environ.get('MTEST_PUID') or os.getuid()),
            int(os.environ.get('MTEST_PGID') or os.getgid()))


def require_kit_folder(base):
    if not (base / MARKER).is_file():
        raise KitError(f'{base} was not made by "./kit.py init" (no {MARKER} file). Run init first.')


def require_docker(run=subprocess.run, which=shutil.which):
    """Docker installed, running, and usable without sudo; else what to do about it."""
    if not which('docker'):
        raise KitError('Docker is not installed. Ubuntu/Debian: sudo apt install docker.io docker-compose-v2, '
                       'then sudo usermod -aG docker "$USER", then log out and back in')
    result = run(['docker', 'info', '--format', '{{.ServerVersion}}'], capture_output=True, text=True)
    if result.returncode == 0:
        return
    detail = (result.stderr or result.stdout or '').strip()
    if 'permission denied' in detail.lower():
        raise KitError('Docker needs sudo for you right now. Add yourself to the docker group '
                       '(sudo usermod -aG docker "$USER"), then log out of this machine and back in '
                       '(a new terminal is not enough); "id -nG" should then list docker')
    raise KitError('Docker is installed but not answering (is it running? sudo systemctl start docker)'
                   + (': ' + detail.splitlines()[-1] if detail else ''))


def compose_env(base):
    puid, pgid = ids()
    env = dict(os.environ, MTEST_ROOT=str(base), MTEST_PUID=str(puid), MTEST_PGID=str(pgid))
    return env


def compose(base, *args, capture=False):
    cmd = ['docker', 'compose', '-f', str(COMPOSE), '-p', PROJECT, *args]
    try:
        return subprocess.run(cmd, env=compose_env(base), check=True, text=True,
                              capture_output=capture).stdout
    except FileNotFoundError:
        raise KitError('docker is not installed or not on PATH') from None
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or '').strip()
        raise KitError('docker compose ' + ' '.join(args) + ' failed' + (': ' + detail if detail else '')) from None


def make_folder(folder, pgid):
    folder.mkdir(parents=True, exist_ok=True)
    try:
        os.chown(folder, -1, pgid)
    except PermissionError:
        raise KitError(f'cannot give {folder} to group {pgid}: is your user in that group? '
                       f'(id -nG)') from None
    # Group-writable, and new files inherit the group (Migratarr checks group write).
    folder.chmod(0o2775)


# ---------------------------------------------------------------- disk2: its own filesystem
#
# The functions that decide what to run (parse_size, mount_steps, fixup_steps, umount_steps,
# describe_disks) are pure, so tests can check them without sudo. run_steps runs a plan.

class Step:
    def __init__(self, argv, sudo, why):
        self.argv, self.sudo, self.why = list(argv), sudo, why

    def __eq__(self, other):
        return isinstance(other, Step) and (self.argv, self.sudo) == (other.argv, other.sudo)

    def __repr__(self):
        return f'Step({self.argv!r}, sudo={self.sudo})'


def parse_size(text):
    """'2G', '512M', '2GiB' or a plain number of bytes -> bytes (K/M/G/T are binary units)."""
    value = str(text).strip().upper()
    value = value[:-2] if value.endswith('IB') else value[:-1] if value.endswith('B') else value
    units = {'K': 1 << 10, 'M': 1 << 20, 'G': 1 << 30, 'T': 1 << 40}
    factor = units.get(value[-1:], 1)
    if value[-1:] in units:
        value = value[:-1]
    try:
        size = int(float(value) * factor)
    except ValueError:
        raise ValueError(f'not a size: {text!r} (use e.g. 2G or 512M)') from None
    if size < MIN_IMAGE_SIZE:
        raise ValueError(f'{text!r} is too small for the test disk (at least 64M)')
    return size


def size_arg(text):
    try:
        return parse_size(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def human_size(size):
    for unit, factor in (('GiB', 1 << 30), ('MiB', 1 << 20)):
        if size >= factor:
            return f'{size / factor:g} {unit}'
    return f'{size} bytes'


def mount_steps(base, *, image_exists, mounted, size, mkfs='mkfs.ext4'):
    """What makes $MTEST_ROOT/disk2 a mounted filesystem of its own; nothing if it already is."""
    disk, image = base / LOOP_DISK, base / LOOP_IMAGE
    if mounted:
        return []
    steps = []
    if not image_exists:
        steps += [Step(['truncate', '-s', str(size), str(image)], False,
                       f'an empty {human_size(size)} image file (sparse: it only uses space as it fills)'),
                  Step([mkfs, '-q', '-F', str(image)], False, 'format the image as ext4')]
    steps.append(Step(['mount', '-o', 'loop,nosuid,nodev', str(image), str(disk)], True,
                      f'attach the image at {disk} as a separate filesystem (only root can mount)'))
    return steps


def fixup_steps(disk, *, owner, group, has_lost_found, uid, gid):
    """A new ext4 filesystem's top folder is root's and holds lost+found; hand it to the kit."""
    steps = []
    if has_lost_found:
        steps.append(Step(['rmdir', str(disk / 'lost+found')], True,
                          "remove mkfs's empty, root-owned lost+found so the disk holds only test folders"))
    if (owner, group) != (uid, gid):
        steps.append(Step(['chown', f'{uid}:{gid}', str(disk)], True,
                          f'give the new disk\'s top folder to you (uid {uid}) and group {gid}, '
                          'like disk1 (mkfs makes it root\'s)'))
    return steps


def umount_steps(base, *, mounted):
    if not mounted:
        return []
    return [Step(['umount', str(base / LOOP_DISK)], True,
                 'detach the test disk image so the folder can be deleted')]


def describe_disks(devices):
    """{disk: st_dev} -> (separate filesystems?, one line saying so)."""
    missing = [d for d in DISKS if d not in devices]
    if missing:
        return False, f'{", ".join(missing)} missing (run ./kit.py init)'
    a, b = (devices[d] for d in DISKS)
    dev = lambda d: f'{os.major(d)}:{os.minor(d)}'
    if a == b:
        return False, (f'disk1 and disk2 are on the SAME filesystem (device {dev(a)}): Migratarr refuses '
                       'cross-disk moves between them ("NAS filesystems are not distinct"). '
                       '"./kit.py init" without --no-loop makes disk2 its own filesystem; after a '
                       'reboot, "./kit.py up" mounts it again.')
    return True, (f'disk1 and disk2 are separate filesystems (devices {dev(a)} and {dev(b)}): '
                  'cross-disk moves can run.')


def disk_devices(base):
    return {d: (base / d).stat().st_dev for d in DISKS if (base / d).is_dir()}


def find_mkfs():
    found = shutil.which('mkfs.ext4') or next(
        (p for p in ('/usr/sbin/mkfs.ext4', '/sbin/mkfs.ext4') if os.access(p, os.X_OK)), None)
    if not found:
        raise KitError('mkfs.ext4 is not installed (Ubuntu/Debian: sudo apt install e2fsprogs), '
                       'or use "./kit.py init --no-loop" (cross-disk moves will then be refused)')
    return found


def run_steps(steps, why):
    """Print every command (and why) first, then run them in order. sudo only where marked."""
    if not steps:
        return
    as_root = os.geteuid() == 0
    if any(s.sudo for s in steps):
        if not as_root and not shutil.which('sudo'):
            raise KitError('sudo is not installed; the kit needs it to mount the test disk '
                           '(or use "./kit.py init --no-loop")')
        say(why + (' Running as root:' if as_root else
                   ' These commands run (sudo may ask for your password; Ctrl-C there stops):'))
    else:
        say(why)
    commands = []
    for s in steps:
        argv = (['sudo'] + s.argv) if s.sudo and not as_root else s.argv
        commands.append(argv)
        say(f'  {shlex.join(argv)}\n      {s.why}')
    for argv in commands:
        try:
            subprocess.run(argv, check=True)
        except FileNotFoundError:
            raise KitError(f'{argv[0]} is not installed') from None
        except subprocess.CalledProcessError as exc:
            raise KitError(f'"{shlex.join(argv)}" failed (exit {exc.returncode})') from None


def clear_mountpoint(disk):
    """The mount point must be empty. An older init's empty folder tree is removed; files are not."""
    files = sorted(str(p) for p in disk.rglob('*') if not p.is_dir() or p.is_symlink())
    if files:
        raise KitError(f'{disk} has files in it ({files[0]}{" and more" if len(files) > 1 else ""}). '
                       'The test disk is mounted there and would hide them. Start over with '
                       '"./kit.py down --delete", or keep plain folders with "./kit.py init --no-loop".')
    for folder, subfolders, _ in os.walk(disk, topdown=False):
        for name in subfolders:
            os.rmdir(os.path.join(folder, name))


def ensure_loop_disk(base, *, size=None, create=False, running=()):
    """Mount disk2.img on disk2 (making the image first if create). Returns whether disk2 is mounted."""
    disk, image = base / LOOP_DISK, base / LOOP_IMAGE
    if os.path.ismount(disk):
        say(f'{disk} is already a mounted filesystem; leaving it as it is.')
        return True
    if not image.is_file() and not create:
        return False
    if running:
        raise KitError(f'{disk} is not mounted but test containers are running ({", ".join(sorted(running))}); '
                       'they would not see the disk. "./kit.py down" first, then "./kit.py up".')
    uid, gid = os.getuid(), ids()[1]
    if disk.exists():
        clear_mountpoint(disk)
    make_folder(disk, gid)
    steps = mount_steps(base, image_exists=image.is_file(), mounted=False,
                        size=size or parse_size(DEFAULT_IMAGE_SIZE),
                        mkfs=find_mkfs() if not image.is_file() else 'mkfs.ext4')
    run_steps(steps, f'{disk} needs to be a separate filesystem: Migratarr refuses cross-disk moves '
                     f'between two folders on one filesystem. The kit '
                     + ('makes' if not image.is_file() else 'mounts')
                     + f' the ext4 image {image} on it.')
    if not os.path.ismount(disk):
        raise KitError(f'{disk} is still not a mount point after mounting {image}')
    st = disk.stat()
    run_steps(fixup_steps(disk, owner=st.st_uid, group=st.st_gid,
                          has_lost_found=(disk / 'lost+found').exists(), uid=uid, gid=gid),
              'The new disk\'s top folder belongs to root.')
    disk.chmod(0o2775)
    return True


def host_path(base, service, container_path):
    """/media/Movies/Common/X (as Radarr sees it) -> $MTEST_ROOT/disk1/Movies/Common/X."""
    rel = PurePosixPath(container_path).relative_to(ARR_ROOT)
    for folder, disk in LAYOUT[service].items():
        parts = PurePosixPath(folder).parts
        if rel.parts[:len(parts)] == parts:
            return base / disk / rel
    raise KitError(f'{container_path} is not under a test category folder')


# ---------------------------------------------------------------- HTTP

_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # localhost only: never a proxy


def http(method, url, headers=None, body=None, timeout=30):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method, headers=dict(
        headers or {}, Accept='application/json', **({'Content-Type': 'application/json'} if data else {})))
    try:
        with _opener.open(request, timeout=timeout) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read()
    if not raw.strip():
        return status, None
    try:
        return status, json.loads(raw)
    except ValueError:  # e.g. an HTML page while an app is still starting
        return (status if status >= 400 else 502), raw.decode(errors='replace')[:300]


class Arr:
    def __init__(self, base, service):
        self.base, self.service = base, service
        self.container, self.port, _ = SERVICES[service]
        self.key, url_base = self.read_config()
        self.url = f'http://127.0.0.1:{self.port}{url_base}/api/v3/'

    def read_config(self):
        path = self.base / 'config' / self.service / 'config.xml'
        try:
            tree = ET.parse(path)
        except (OSError, ET.ParseError):
            raise KitError(f'{path} is not there yet: is {self.container} running? ("./kit.py up")') from None
        key = (tree.findtext('ApiKey') or '').strip()
        if not key:
            raise KitError(f'{path} has no ApiKey yet; wait a few seconds and try again')
        return key, (tree.findtext('UrlBase') or '').strip().rstrip('/')

    def call(self, method, path, body=None, ok=(200, 201, 202)):
        status, data = http(method, self.url + path, {'X-Api-Key': self.key}, body)
        if status not in ok:
            raise KitError(f'{self.service}: {method} {path} answered HTTP {status}: {data}')
        return data

    def get(self, path):
        return self.call('GET', path)

    def post(self, path, body):
        return self.call('POST', path, body)


def jellyfin_auth(token=None):
    value = (f'MediaBrowser Client="{JELLYFIN_APP}", Device="kit", DeviceId="{JELLYFIN_APP}", '
             f'Version="1.0"')
    return {'Authorization': value + (f', Token="{token}"' if token else '')}


def jellyfin_url(path):
    return f'http://127.0.0.1:{SERVICES["jellyfin"][1]}{path}'


# ---------------------------------------------------------------- commands

def cmd_init(args):
    base = root()
    puid, pgid = ids()
    # The README clones the code to src/ and installs it to venv/ first; nothing else may be there.
    if base.exists() and not (base / MARKER).exists() and {p.name for p in base.iterdir()} - {'src', 'venv'}:
        raise KitError(f'{base} already exists and has other files in it. Choose a new, empty folder.')
    old = os.umask(0o002)
    try:
        for folder in [base, base / 'config', base / 'seed', base / 'migratarr'] + [
                base / 'config' / s for s in SERVICES]:
            make_folder(folder, pgid)
        (base / MARKER).write_text('Made by migratarr examples/local-test/kit.py. '
                                   '"./kit.py down --delete" removes this whole folder.\n')
        if args.no_loop:
            if (base / LOOP_IMAGE).is_file() and not os.path.ismount(base / LOOP_DISK):
                raise KitError(f'{base / LOOP_IMAGE} is the test disk from an earlier init; run "./kit.py init" '
                               'without --no-loop to mount it')
        else:
            ensure_loop_disk(base, size=args.disk2_size, create=True)
        for disk in DISKS:
            make_folder(base / disk, pgid)
            for media, cats in CATEGORIES.items():
                make_folder(base / disk / media, pgid)
                for c in cats:
                    make_folder(base / disk / media / c, pgid)
    finally:
        os.umask(old)
    say(f'Test folder ready: {base}')
    say(f'  disks:    {base}/disk1, {base}/disk2 (group {pgid}, mode 2775)'
        + ('' if args.no_loop else f'; disk2 is {base / LOOP_IMAGE}, loop-mounted'))
    separate, line = describe_disks(disk_devices(base))
    say(('  ' if separate else '  WARNING: ') + line)
    say(f'  configs:  {base}/config/<radarr|sonarr|jellyfin>')
    say(f'  Migratarr data folder: {base}/migratarr   pasted keys: {base}/secrets')
    say(f'Containers will run as PUID {puid}, PGID {pgid}.')


def port_open(port):
    with socket.socket() as s:
        s.settimeout(1)
        return s.connect_ex(('127.0.0.1', port)) == 0


def running_containers():
    try:
        out = subprocess.run(['docker', 'ps', '--format', '{{.Names}}'], check=True, text=True,
                             capture_output=True).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise KitError('cannot run "docker ps": ' + type(exc).__name__ + '. Can your user use docker without sudo?')
    return set(out.split())


def cmd_check_compose(args):
    """compose.yaml's mounts and ports must match LAYOUT and SERVICES."""
    base = root()
    data = json.loads(compose(base, 'config', '--format', 'json', capture=True))
    problems = []
    for service, (container, host_port, inner) in SERVICES.items():
        svc = data['services'].get(service, {})
        if svc.get('container_name') != container:
            problems.append(f'{service}: container_name is {svc.get("container_name")}, expected {container}')
        ports = {(str(p.get('host_ip')), str(p.get('published')), int(p.get('target'))) for p in svc.get('ports', [])}
        if ports != {('127.0.0.1', str(host_port), inner)}:
            problems.append(f'{service}: ports {sorted(ports)}, expected 127.0.0.1:{host_port}->{inner}')
        mounts = {v['target']: v['source'] for v in svc.get('volumes', [])}
        expected = {'/config': str(base / 'config' / service)}
        expected.update({f'{ARR_ROOT}/{folder}': str(base / disk / folder)
                         for folder, disk in LAYOUT.get(service, {}).items()})
        if service == 'jellyfin':
            expected['/seed'] = str(base / 'seed')
        if mounts != expected:
            problems.append(f'{service}: mounts {mounts} differ from {expected}')
        for source in mounts.values():
            if not Path(source).is_relative_to(base):
                problems.append(f'{service}: {source} is outside {base}')
    if problems:
        raise KitError('compose.yaml does not match kit.py:\n  ' + '\n  '.join(problems))
    say('compose.yaml matches the kit layout: every mount is under ' + str(base) + ', ports on 127.0.0.1 only.')


def cmd_up(args):
    base = root()
    require_kit_folder(base)
    cmd_check_compose(args)
    mine = {c for c, _, _ in SERVICES.values()} & running_containers()
    # After a reboot the loop mount is gone; mount it before Docker bind-mounts disk2's folders.
    ensure_loop_disk(base, running=mine)
    separate, line = describe_disks(disk_devices(base))
    if not separate:
        say('WARNING: ' + line)
    for service, (container, port, _) in SERVICES.items():
        if container not in mine and port_open(port):
            raise KitError(f'port {port} is already in use by something else; {service} cannot start there')
    if port_open(UI_PORT):
        say(f'Note: port {UI_PORT} (the test Migratarr UI) is in use already. Stop that before "migratarr ui".')
    compose(base, 'up', '-d')
    say('Waiting for the apps to answer (first start can take a minute or two)...')
    deadline = time.time() + WAIT
    for service in ('radarr', 'sonarr'):
        while True:
            try:
                arr = Arr(base, service)
                arr.get('system/status')
                say(f'  {service}: ready on http://localhost:{arr.port}')
                break
            except (KitError, OSError):
                if time.time() > deadline:
                    raise KitError(f'{service} did not answer within {WAIT}s: docker logs {SERVICES[service][0]}')
                time.sleep(3)
    while True:
        try:
            status, info = http('GET', jellyfin_url('/System/Info/Public'))
            if status == 200 and isinstance(info, dict):
                say(f'  jellyfin: ready on http://localhost:{SERVICES["jellyfin"][1]}')
                break
        except OSError:
            pass
        if time.time() > deadline:
            raise KitError(f'jellyfin did not answer within {WAIT}s: docker logs mtest-jellyfin')
        time.sleep(3)
    say('Next: ./kit.py seed')


def ensure_tags(arr, labels):
    tags = {t['label']: t['id'] for t in arr.get('tag')}
    for label in labels:
        if label not in tags:
            tags[label] = arr.post('tag', {'label': label})['id']
    return tags


def ensure_root_folders(arr):
    have = {r['path'] for r in arr.get('rootfolder')}
    for folder in LAYOUT[arr.service]:
        path = f'{ARR_ROOT}/{folder}'
        if path not in have:
            arr.post('rootfolder', {'path': path})
    bad = [r['path'] for r in arr.get('rootfolder') if r['path'].startswith(ARR_ROOT + '/')
           and r.get('accessible') is not True]
    if bad:
        raise KitError(f'{arr.service} cannot write to its root folders {bad}: check PUID/PGID and folder modes')


def ensure_sample(base):
    sample = base / 'seed' / 'sample.mkv'
    if sample.is_file() and sample.stat().st_size > 0:
        return sample
    cmd = ['docker', 'exec', SERVICES['jellyfin'][0], FFMPEG, '-hide_banner', '-loglevel', 'error',
           '-f', 'lavfi', '-i', 'testsrc2=size=320x240:rate=24:duration=20',
           '-f', 'lavfi', '-i', 'sine=frequency=440:duration=20',
           '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '35', '-c:a', 'aac', '-shortest',
           '-y', '/seed/sample.mkv']
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode == 0 and sample.is_file() and sample.stat().st_size > 0:
        say(f'  sample video: {sample} ({sample.stat().st_size // 1024} KiB, 20 s)')
        return sample
    say('  ! could not make a real video with Jellyfin\'s ffmpeg; using a 2 MiB placeholder file instead.')
    say('    Radarr/Sonarr may refuse to import it; note this in what you send back.')
    sample.write_bytes(os.urandom(2 * 1024 * 1024))
    return sample


def place(sample, folder, name):
    if not folder.is_relative_to(root()):
        raise KitError(f'{folder} is outside the test folder')
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / name
    if not target.exists():
        shutil.copyfile(sample, target)
    pgid = ids()[1]
    for p in (folder, target):
        os.chown(p, -1, pgid)
    folder.chmod(0o2775)
    target.chmod(0o664)
    return target


def lookup(arr, kind, title, year):
    results = arr.get(f'{kind}/lookup?term=' + urllib.parse.quote(title))
    names = {title.casefold(), f'{title} ({year})'.casefold()}  # Sonarr may add the year to tell remakes apart
    for r in results or []:
        if str(r.get('title', '')).casefold() in names and r.get('year') == year:
            return r
    found = ', '.join(f'{r.get("title")} ({r.get("year")})' for r in (results or [])[:5]) or 'nothing'
    raise KitError(f'{arr.service} lookup for "{title}" ({year}) found {found}. Is the container online?')


def wait_until(test, what, seconds=120):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if test():
            return True
        time.sleep(3)
    say(f'  ! {what} did not happen within {seconds}s')
    return False


def seed_movies(base, sample, state):
    arr = Arr(base, 'radarr')
    ensure_root_folders(arr)
    tags = ensure_tags(arr, sorted({t for _, _, t in MOVIES} | {SEED_TAG}))
    profile = arr.get('qualityprofile')[0]['id']
    start = f'{ARR_ROOT}/{START["radarr"]}'
    for title, year, tag in MOVIES:
        found = lookup(arr, 'movie', title, year)
        if found.get('id'):
            movie = arr.get(f'movie/{found["id"]}')
        else:
            body = dict(found, qualityProfileId=profile, rootFolderPath=start, monitored=False,
                        minimumAvailability='released', tags=[tags[tag], tags[SEED_TAG]],
                        addOptions={'searchForMovie': False})
            movie = arr.post('movie', body)
        folder = host_path(base, 'radarr', movie['path'])
        if not movie.get('hasFile'):
            place(sample, folder, PurePosixPath(movie['path']).name + '.mkv')
            arr.post('command', {'name': 'RescanMovie', 'movieId': movie['id']})
            imported = wait_until(lambda: arr.get(f'movie/{movie["id"]}').get('hasFile'),
                                  f'Radarr importing {title}')
        else:
            imported = True
        state['movies'].append({'id': movie['id'], 'title': title, 'year': year, 'tag': tag,
                                'folder': PurePosixPath(movie['path']).name, 'imported': bool(imported)})
        say(f'  movie  {title} ({year}): {movie["path"]}  tag {tag}  '
            + ('imported' if imported else 'NOT imported'))


def seed_series(base, sample, state):
    arr = Arr(base, 'sonarr')
    ensure_root_folders(arr)
    tags = ensure_tags(arr, sorted({t for _, _, t in SERIES} | {SEED_TAG}))
    profile = arr.get('qualityprofile')[0]['id']
    start = f'{ARR_ROOT}/{START["sonarr"]}'
    for title, year, tag in SERIES:
        found = lookup(arr, 'series', title, year)
        if found.get('id'):
            series = arr.get(f'series/{found["id"]}')
        else:
            seasons = [dict(s, monitored=False) for s in found.get('seasons', [])]
            body = dict(found, seasons=seasons, qualityProfileId=profile, rootFolderPath=start,
                        monitored=False, seasonFolder=True, tags=[tags[tag], tags[SEED_TAG]],
                        addOptions={'monitor': 'none', 'searchForMissingEpisodes': False,
                                    'searchForCutoffUnmetEpisodes': False})
            series = arr.post('series', body)
        sid = series['id']
        folder = host_path(base, 'sonarr', series['path'])
        name = PurePosixPath(series['path']).name
        if not arr.get(f'episodefile?seriesId={sid}'):
            # Sonarr fetches episodes after adding; wait for S01E01 to exist before importing a file for it.
            wait_until(lambda: any(e.get('seasonNumber') == 1 and e.get('episodeNumber') == 1
                                   for e in arr.get(f'episode?seriesId={sid}')), f'Sonarr listing {title} episodes', 90)
            place(sample, folder / 'Season 01', f'{name} - S01E01.mkv')
            folder.chmod(0o2775)
            arr.post('command', {'name': 'RescanSeries', 'seriesId': sid})
            imported = wait_until(lambda: bool(arr.get(f'episodefile?seriesId={sid}')),
                                  f'Sonarr importing {title}')
        else:
            imported = True
        state['series'].append({'id': sid, 'title': title, 'year': year, 'tag': tag, 'folder': name,
                                'imported': bool(imported)})
        say(f'  series {title} ({year}): {series["path"]}  tag {tag}  '
            + ('imported' if imported else 'NOT imported'))


def seed_jellyfin(base):
    key_file, admin_file = base / 'jellyfin-api-key.txt', base / 'jellyfin-admin.txt'
    if key_file.is_file() and key_file.read_text().strip():
        say(f'  jellyfin: API key already in {key_file}')
        return
    status, info = http('GET', jellyfin_url('/System/Info/Public'))
    if status != 200:
        raise KitError(f'jellyfin is not answering (HTTP {status}); "./kit.py up" first')
    if not info.get('StartupWizardCompleted'):
        password = secrets.token_urlsafe(18)
        for method, path, body in (
                ('POST', '/Startup/Configuration', {'UICulture': 'en-US', 'MetadataCountryCode': 'US',
                                                    'PreferredMetadataLanguage': 'en'}),
                ('GET', '/Startup/User', None),
                ('POST', '/Startup/User', {'Name': JELLYFIN_USER, 'Password': password}),
                ('POST', '/Startup/RemoteAccess', {'EnableRemoteAccess': True, 'EnableAutomaticPortMapping': False}),
                ('POST', '/Startup/Complete', None)):
            code, data = http(method, jellyfin_url(path), body=body if method == 'POST' else None)
            if code not in (200, 204):
                raise KitError(f'jellyfin first-run step {path} answered HTTP {code}: {data}')
        write_private(admin_file, f'user: {JELLYFIN_USER}\npassword: {password}\n')
        say(f'  jellyfin: first-run setup done; admin login saved in {admin_file}')
    if not admin_file.is_file():
        raise KitError('jellyfin was already set up by hand. Make an API key in its Dashboard > API Keys '
                       f'and save it in {key_file}')
    lines = dict(line.split(': ', 1) for line in admin_file.read_text().splitlines() if ': ' in line)
    code, auth = http('POST', jellyfin_url('/Users/AuthenticateByName'), jellyfin_auth(),
                      {'Username': lines['user'], 'Pw': lines['password']})
    if code != 200:
        raise KitError(f'jellyfin login answered HTTP {code}')
    token = jellyfin_auth(auth['AccessToken'])
    code, data = http('POST', jellyfin_url('/Auth/Keys?app=' + JELLYFIN_APP), token)
    if code not in (200, 204):
        raise KitError(f'jellyfin: creating an API key answered HTTP {code}: {data}')
    code, data = http('GET', jellyfin_url('/Auth/Keys'), token)
    keys = [k['AccessToken'] for k in (data or {}).get('Items', []) if k.get('AppName') == JELLYFIN_APP]
    if code != 200 or not keys:
        raise KitError(f'jellyfin: could not read back the new API key (HTTP {code})')
    write_private(key_file, keys[-1] + '\n')
    say(f'  jellyfin: API key saved in {key_file} (paste it in the wizard)')


def write_private(path, text):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        f.write(text)
    path.chmod(0o600)


def cmd_seed(args):
    base = root()
    require_kit_folder(base)
    old = os.umask(0o002)
    try:
        say('Making the sample video...')
        sample = ensure_sample(base)
        state = {'movies': [], 'series': []}
        say('Radarr:')
        seed_movies(base, sample, state)
        say('Sonarr:')
        seed_series(base, sample, state)
        say('Jellyfin:')
        seed_jellyfin(base)
    finally:
        os.umask(old)
    (base / 'seeded.json').write_text(json.dumps(state, indent=2) + '\n')
    missing = [i['title'] for i in state['movies'] + state['series'] if not i['imported']]
    if missing:
        say('\n! Not imported: ' + ', '.join(missing) + '. Check Radarr/Sonarr > System > Logs, and send that back.')
    say('\nSeeded. Next: set up Migratarr (README step 4). "./kit.py wizard-values" prints what to type.')


def cmd_wizard_values(args):
    base = root()
    r, s, j = (SERVICES[x][1] for x in ('radarr', 'sonarr', 'jellyfin'))
    layout = {**LAYOUT['radarr'], **LAYOUT['sonarr']}
    disk_id = {'disk1': 'media01', 'disk2': 'media02'}
    say(f'''Migratarr setup wizard values for this kit ({base})

Step 1, Connect your services. Type these BEFORE pressing Detect (Detect only fills blank fields,
and the defaults name your real containers and ports):
  Radarr   URL http://localhost:{r}   Docker container {SERVICES["radarr"][0]}
           API key: Read automatically from the Docker container's config.xml
  Sonarr   URL http://localhost:{s}   Docker container {SERVICES["sonarr"][0]}
           API key: Read automatically from the Docker container's config.xml
  Jellyfin URL http://localhost:{j}   Docker container {SERVICES["jellyfin"][0]}
           API key: Paste the key here  -> the line in {base}/jellyfin-api-key.txt
  TMDB     API Read Access Token: Paste the key here -> your real TMDB read token
  Then press Detect: it should say it found config.xml in "{SERVICES["radarr"][0]}" and "{SERVICES["sonarr"][0]}".

Step 2, Where your disks are:
  Moves run:              On this machine's own disks (experimental)
  Migratarr data folder:  {base}/migratarr
  (leave the NAS fields empty)

Step 3, Disks and categories. Do NOT press Detect on this step (it suggests your real mounts):
  Default free-space reserve (GB): 1
  Media root as Radarr and Sonarr see it: {ARR_ROOT}
  Disk media01: Name "Test disk 1", Mounted at {base}/disk1, Holds Movies and TV, Keep free blank
  Add a disk:   Short id media02, Name "Test disk 2", Mounted at {base}/disk2, then Next
                (the page reloads with media02 added; tick Movies and TV for it)
  Category folders: keep the defaults (Movies/Common ... TV/Archive)
  Which disks hold each category:''')
    for media, label in (('Movies', 'Movies'), ('TV', 'TV')):
        say('    ' + label + ': ' + ', '.join(f'{c} = {disk_id[layout[f"{media}/{c}"]]}' for c in CATEGORIES[media]))
    say(f'''
Step 4, Streaming services: Region US (or yours); the services lists can stay empty.

Review: check every URL says localhost:{r}/{s}/{j} and every path starts with {base}, then Save.''')


def load_json(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise KitError(f'cannot read {path}: {type(exc).__name__}') from None


def cmd_check(args):
    """Fails if the test Migratarr's settings or environment could reach anything outside the kit."""
    base = root()
    problems, notes = [], []
    home = base / 'migratarr'
    secrets_dir = base / 'secrets'
    env = os.environ
    if Path(env.get('MIGRATARR_HOME', '')).expanduser() != home:
        problems.append(f'MIGRATARR_HOME is {env.get("MIGRATARR_HOME")!r}, expected {home} '
                        '(source activate.sh in this shell)')
    if Path(env.get('MIGRATARR_SECRETS_DIR', '')).expanduser() != secrets_dir:
        problems.append(f'MIGRATARR_SECRETS_DIR is {env.get("MIGRATARR_SECRETS_DIR")!r}, expected {secrets_dir}')
    for name in sorted(env):
        if name.startswith('MIGRATARR_') and name not in ('MIGRATARR_HOME', 'MIGRATARR_SECRETS_DIR'):
            problems.append(f'{name} is set; it could point Migratarr elsewhere (unset it)')
    exe = shutil.which('migratarr')
    if not exe or not Path(exe).resolve().is_relative_to(base.resolve()):
        problems.append(f'"migratarr" runs {exe}, not the copy in {base}/venv (source activate.sh)')
    cfg = home / 'config'
    if not (cfg / 'runtime.json').is_file():
        notes.append(f'{cfg}/runtime.json does not exist yet (finish the setup wizard, then run check again)')
    else:
        runtime = load_json(cfg / 'runtime.json')
        check_runtime(runtime, base, problems)
        targets_path = Path(runtime.get('storage_targets') or 'storage-targets.json')
        targets_path = targets_path if targets_path.is_absolute() else cfg / targets_path
        check_targets(load_json(targets_path), base, problems)
    for line in notes:
        say('  note: ' + line)
    # Not an isolation problem, so it doesn't fail check; but cross-disk moves need it.
    separate, line = describe_disks(disk_devices(base))
    say(('  disks: ' if separate else '  WARNING: ') + line)
    if problems:
        say('NOT ISOLATED. Fix these before running anything else:')
        for line in problems:
            say('  FAIL ' + line)
        return 1
    say(f'OK: Migratarr settings and this shell point only at the test kit in {base}.')
    return 0


def check_runtime(runtime, base, problems):
    if runtime.get('topology') != 'local':
        problems.append(f'runtime.json topology is {runtime.get("topology")!r}, expected "local"')
    if not Path(str(runtime.get('base_path', ''))).is_relative_to(base):
        problems.append(f'runtime.json base_path {runtime.get("base_path")!r} is outside {base}')
    if 'nas' in runtime:
        problems.append('runtime.json has a "nas" section')
    for service, (container, port, _) in SERVICES.items():
        url = str(runtime.get('urls', {}).get(service, ''))
        parsed = urllib.parse.urlsplit(url)
        if parsed.hostname not in ('localhost', '127.0.0.1') or parsed.port != port:
            problems.append(f'urls.{service} is {url!r}, expected http://localhost:{port}')
    containers = runtime.get('containers', {})
    for key in ('radarr', 'sonarr'):
        if containers.get(key) != SERVICES[key][0]:
            problems.append(f'containers.{key} is {containers.get(key)!r}, expected {SERVICES[key][0]}')
    for service, entry in (runtime.get('secrets') or {}).items():
        source = entry.get('source')
        if source == 'file' and not Path(str(entry.get('path'))).expanduser().is_relative_to(base):
            problems.append(f'secrets.{service} reads {entry.get("path")}, outside {base}')
        elif source in ('docker_config_xml', 'docker_file'):
            container = entry.get('container') or containers.get(
                {'radarr': 'radarr', 'sonarr': 'sonarr', 'jellyfin': 'homepage'}.get(service, ''))
            if not str(container).startswith('mtest-'):
                problems.append(f'secrets.{service} reads a key from container {container!r}, not a test container')
        elif source == 'config_xml' and not Path(str(entry.get('path'))).is_relative_to(base):
            problems.append(f'secrets.{service} reads {entry.get("path")}, outside {base}')
        elif source == 'env' and service != 'tmdb':
            problems.append(f'secrets.{service} reads an environment variable; paste the test key instead')
    for service, entry in (runtime.get('arr_file_checks') or {}).items():
        if entry.get('mode') == 'docker' and not str(entry.get('container') or containers.get(service)).startswith('mtest-'):
            problems.append(f'arr_file_checks.{service} uses a container that is not a test container')
        if entry.get('mode') == 'host':
            problems.append(f'arr_file_checks.{service} is "host"; the kit expects the default (docker)')


def check_targets(targets, base, problems):
    if targets.get('arr_root', ARR_ROOT) != ARR_ROOT:
        problems.append(f'storage-targets.json arr_root is {targets.get("arr_root")!r}, expected {ARR_ROOT}')
    for t in targets.get('targets', []):
        if not Path(str(t.get('path', ''))).is_relative_to(base):
            problems.append(f'disk {t.get("id")} is {t.get("path")}, outside {base}')
        if 'remote_path' in t:
            problems.append(f'disk {t.get("id")} has a remote_path')
    by_path = {str(t.get('path')): t.get('id') for t in targets.get('targets', [])}
    folders = targets.get('category_paths', {})
    for media, service in (('Movie', 'radarr'), ('TV', 'sonarr')):
        for cat, disk_ids in targets.get('placement', {}).get(media, {}).items():
            folder = folders.get(media, {}).get(cat)
            want = LAYOUT[service].get(folder)
            got = sorted(Path(p).name for p, d in by_path.items() if d in disk_ids)
            if want is None:
                problems.append(f'category folder {media}/{cat} = {folder!r} is not one the kit mounts')
            elif got != [want]:
                problems.append(f'placement {media}/{cat} is on {got or "no disk"}; the kit mounts {folder} '
                                f'from {want} only (Radarr/Sonarr could not see it elsewhere)')


def cmd_status(args):
    base = root()
    require_kit_folder(base)
    state = load_json(base / 'seeded.json')
    names = {i['folder'] for i in state['movies'] + state['series']}
    rows = []
    for service, kind, items, unit in (('radarr', 'movie', state['movies'], 'movieFile'),
                                       ('sonarr', 'series', state['series'], None)):
        arr = Arr(base, service)
        for item in items:
            data = arr.get(f'{kind}/{item["id"]}')
            if service == 'radarr':
                files = [data[unit]] if data.get('hasFile') and data.get(unit) else []
            else:
                files = arr.get(f'episodefile?seriesId={item["id"]}')
            arr_path = data['path']
            host = host_path(base, service, arr_path)
            on_disk = sorted(str(p.relative_to(base)) for p in base.glob(f'disk*/*/*/{glob_escape(item["folder"])}'))
            ok = host.is_dir() and on_disk == [str(host.relative_to(base))] and all(
                (host / f['relativePath']).is_file() for f in files)
            rows.append((ok, item, arr_path, host, on_disk, files))
    say(f'{"":3}{"title":28} {"Radarr/Sonarr path":50} {"files":5} on the test disks')
    for ok, item, arr_path, host, on_disk, files in rows:
        say(f'{"ok" if ok else "!!":3}{item["title"][:28]:28} {arr_path[:50]:50} {len(files):5} '
            + (', '.join(on_disk) or 'NOT FOUND'))
    strays = []
    for disk in DISKS:
        for media, cats in CATEGORIES.items():
            for cat in cats:
                folder = base / disk / media / cat
                strays += [str(p.relative_to(base)) for p in sorted(folder.iterdir()) if p.name not in names]
    say('')
    say('Anything else in the category folders (should be none): ' + (', '.join(strays) or 'none'))
    separate, line = describe_disks(disk_devices(base))
    say(('Disks: ' if separate else 'WARNING: ') + line)
    bad = [r for r in rows if not r[0]]
    if bad:
        say('"!!": the title is missing, is in more than one place, or Radarr/Sonarr points somewhere it is not.')
    return 1 if bad or strays else 0


def glob_escape(name):
    return ''.join(f'[{c}]' if c in '*?[' else c for c in name)


def cmd_report(args):
    """Everything to send back, in one file outside $MTEST_ROOT (so it survives teardown)."""
    base = root()
    require_kit_folder(base)
    out = Path(args.output).expanduser() if args.output else (
        Path.home() / time.strftime('mtest-report-%Y%m%d-%H%M%S.txt'))
    sections = [
        ('kit and code', [['git', '-C', str(base / 'src'), 'log', '-1', '--format=%H %s'],
                          ['git', '-C', str(base / 'src'), 'status', '--short'],
                          ['migratarr', '--version'], [sys.executable, '--version'], ['uname', '-sr'],
                          ['docker', '--version'], ['docker', 'compose', 'version']]),
        ('test containers', [['docker', 'ps', '-a', '--filter', 'name=mtest-', '--format',
                              '{{.Names}}\t{{.Image}}\t{{.Status}}']] +
         [['docker', 'inspect', '--format', '{{.Name}} {{index .Config.Labels "org.opencontainers.image.version"}}',
           c] for c, _, _ in SERVICES.values()]),
        ('isolation check', [[sys.executable, str(KIT / 'kit.py'), 'check']]),
        ('migratarr doctor', [['migratarr', 'doctor']]),
        ('test titles', [[sys.executable, str(KIT / 'kit.py'), 'status']]),
        ('runs', [['migratarr', 'status']]),
    ]
    latest = latest_run()
    if latest:
        sections.append(('latest run', [['migratarr', 'status', '--run', latest]]))
    text = [f'Migratarr local-disk test report, {time.strftime("%Y-%m-%d %H:%M %Z")}']
    for title, commands in sections:
        text.append('\n== ' + title + ' ==')
        for cmd in commands:
            text.append('$ ' + ' '.join(cmd))
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
                text.append((r.stdout + r.stderr).rstrip() + (f'\n(exit {r.returncode})' if r.returncode else ''))
            except (OSError, subprocess.SubprocessError) as exc:
                text.append(f'(could not run: {type(exc).__name__})')
    body = '\n'.join(text) + '\n'
    body = body.replace(str(Path.home()), '~')  # no home folder (user name) in what you paste
    out.write_text(body)
    say(f'Report written to {out}. Send that file (or paste it) back; add anything odd you noticed.')


def latest_run():
    try:
        r = subprocess.run(['migratarr', 'status', '--json'], capture_output=True, text=True, timeout=120)
        runs = json.loads(r.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    ids = sorted(str(x.get('run_id')) for x in runs if isinstance(x, dict) and x.get('run_id'))
    return ids[-1] if ids else None


def make_tree_writable(base):
    """Give the owner rwx on every folder under base that is theirs. Migratarr makes run snapshots
    ($MTEST_ROOT/migratarr/runs/<run>/) read-only on purpose; files in a read-only folder can't be
    deleted until the folder is writable again. Symlinks are never followed, so nothing outside
    base is touched. Folders owned by someone else (e.g. root, from a container) are left as they are."""
    uid = os.getuid()
    def fix(path):
        st = os.lstat(path)
        if st.st_uid == uid and (st.st_mode & 0o700) != 0o700:
            os.chmod(path, (st.st_mode & 0o7777) | 0o700)
    if base.is_symlink() or not base.is_dir():
        return
    fix(base)
    for folder, subfolders, _ in os.walk(base):  # top-down: a folder is fixed before it is entered
        for name in subfolders:
            path = os.path.join(folder, name)
            if not os.path.islink(path):
                fix(path)


def remove_tree(base):
    make_tree_writable(base)
    failed = []
    def report(func, path, exc):
        failed.append(path)
    if sys.version_info >= (3, 12):
        shutil.rmtree(base, onexc=report)
    else:
        shutil.rmtree(base, onerror=report)
    if not base.exists():
        return
    uid = os.getuid()
    others = []
    for path in failed:
        try:
            if uid not in (os.lstat(path).st_uid, os.lstat(os.path.dirname(path)).st_uid):
                others.append(path)
        except OSError:
            pass
    for path in (others or failed)[:10]:
        say(f'  could not remove {path}')
    if others:
        raise KitError(f'{len(others)} item(s) belong to another user (a container may have made them as root). '
                       f'Remove the rest with:  sudo rm -rf "{base}"')
    raise KitError(f'some files under {base} could not be removed (listed above)')


def cmd_down(args):
    base = root()
    require_kit_folder(base)
    compose(base, 'down', '--volumes', '--remove-orphans')
    say('Containers removed.')
    disk = base / LOOP_DISK
    if not args.delete:
        say(f'The test folder is still there: {base}. "./kit.py down --delete" removes it too.')
        if os.path.ismount(disk):
            say(f'{disk} stays mounted ("./kit.py up" uses it again; a reboot unmounts it).')
        return
    run_steps(umount_steps(base, mounted=os.path.ismount(disk)),
              f'{disk} is the mounted test disk image; it is unmounted before deleting.')
    if os.path.ismount(disk):
        raise KitError(f'{disk} is still mounted; nothing was deleted. Try:  sudo umount "{disk}"')
    remove_tree(base)
    say(f'Removed {base}. Nothing from the kit is left (the images stay cached: '
        'docker image rm lscr.io/linuxserver/radarr lscr.io/linuxserver/sonarr lscr.io/linuxserver/jellyfin '
        'only if your real stack does not use them).')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    sub = parser.add_subparsers(dest='command', required=True)
    for name, func, text in (('init', cmd_init, 'create the test folder tree'),
                             ('up', cmd_up, 'start the containers and wait for them'),
                             ('seed', cmd_seed, 'add root folders, tags, dummy titles, Jellyfin key'),
                             ('wizard-values', cmd_wizard_values, "what to type into Migratarr's setup wizard"),
                             ('check', cmd_check, 'prove the test Migratarr only reaches the kit'),
                             ('check-compose', cmd_check_compose, 'compose.yaml matches the kit layout'),
                             ('status', cmd_status, 'where each test title is'),
                             ('report', cmd_report, 'write everything to send back to one file'),
                             ('down', cmd_down, 'stop and remove the containers')):
        p = sub.add_parser(name, help=text)
        p.set_defaults(func=func)
        if name == 'report':
            p.add_argument('--output', help='file to write (default ~/mtest-report-<time>.txt)')
        if name == 'init':
            p.add_argument('--disk2-size', type=size_arg, default=parse_size(DEFAULT_IMAGE_SIZE),
                           metavar='SIZE', help='size of the disk2 image, e.g. 2G or 512M (default '
                           f'{DEFAULT_IMAGE_SIZE}; only used when the image is first made)')
            p.add_argument('--no-loop', action='store_true',
                           help='keep disk2 a plain folder (no sudo); cross-disk moves will then be refused')
        if name == 'down':
            p.add_argument('--delete', action='store_true', help='also delete $MTEST_ROOT')
    args = parser.parse_args(argv)
    try:
        if args.command in ('up', 'seed'):
            require_docker()
        return args.func(args) or 0
    except KitError as exc:
        print('kit: ' + str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == '__main__':
    sys.exit(main())
