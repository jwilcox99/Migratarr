"""examples/local-test/kit.py: the disk2 loop-mount plan and the filesystem check, without sudo."""
import contextlib
import importlib.util
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

KIT_PATH = Path(__file__).resolve().parent.parent / 'examples' / 'local-test' / 'kit.py'
_spec = importlib.util.spec_from_file_location('mtest_kit', KIT_PATH)
kit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(kit)

BASE = Path('/srv/mtest')
GIB = 1 << 30


def quiet():
    return contextlib.redirect_stdout(io.StringIO())


class ParseSize(unittest.TestCase):
    def test_units(self):
        self.assertEqual(kit.parse_size('2G'), 2 * GIB)
        self.assertEqual(kit.parse_size('2GiB'), 2 * GIB)
        self.assertEqual(kit.parse_size('2gb'), 2 * GIB)
        self.assertEqual(kit.parse_size('512M'), 512 << 20)
        self.assertEqual(kit.parse_size('1.5G'), 3 * GIB // 2)
        self.assertEqual(kit.parse_size(str(GIB)), GIB)

    def test_rejects_nonsense_and_tiny(self):
        for bad in ('', 'abc', 'G', '1M', '-2G'):
            with self.assertRaises(ValueError, msg=bad):
                kit.parse_size(bad)

    def test_cli_default_is_2_gib(self):
        with mock.patch.object(kit, 'cmd_init', return_value=0) as init:
            kit.main(['init'])
        args = init.call_args.args[0]
        self.assertEqual((args.disk2_size, args.no_loop), (2 * GIB, False))
        with mock.patch.object(kit, 'cmd_init', return_value=0) as init:
            kit.main(['init', '--disk2-size', '1G', '--no-loop'])
        args = init.call_args.args[0]
        self.assertEqual((args.disk2_size, args.no_loop), (GIB, True))


class Plans(unittest.TestCase):
    def test_fresh_image(self):
        steps = kit.mount_steps(BASE, image_exists=False, mounted=False, size=2 * GIB, mkfs='/usr/sbin/mkfs.ext4')
        self.assertEqual(steps, [
            kit.Step(['truncate', '-s', str(2 * GIB), '/srv/mtest/disk2.img'], False, ''),
            kit.Step(['/usr/sbin/mkfs.ext4', '-q', '-F', '/srv/mtest/disk2.img'], False, ''),
            kit.Step(['mount', '-o', 'loop,nosuid,nodev', '/srv/mtest/disk2.img', '/srv/mtest/disk2'], True, ''),
        ])
        self.assertTrue(all(s.why for s in steps))

    def test_after_reboot_only_mounts(self):
        steps = kit.mount_steps(BASE, image_exists=True, mounted=False, size=GIB)
        self.assertEqual([s.argv[0] for s in steps], ['mount'])
        self.assertTrue(steps[0].sudo)

    def test_already_mounted_does_nothing(self):
        self.assertEqual(kit.mount_steps(BASE, image_exists=True, mounted=True, size=GIB), [])

    def test_fixups(self):
        disk = BASE / 'disk2'
        fresh = kit.fixup_steps(disk, owner=0, group=0, has_lost_found=True, uid=1000, gid=100)
        self.assertEqual(fresh, [kit.Step(['rmdir', '/srv/mtest/disk2/lost+found'], True, ''),
                                 kit.Step(['chown', '1000:100', '/srv/mtest/disk2'], True, '')])
        self.assertEqual(kit.fixup_steps(disk, owner=1000, group=100, has_lost_found=False,
                                         uid=1000, gid=100), [])

    def test_umount_only_when_mounted(self):
        self.assertEqual(kit.umount_steps(BASE, mounted=False), [])
        self.assertEqual(kit.umount_steps(BASE, mounted=True),
                         [kit.Step(['umount', '/srv/mtest/disk2'], True, '')])


class DescribeDisks(unittest.TestCase):
    def test_same_filesystem_is_refused(self):
        ok, line = kit.describe_disks({'disk1': os.makedev(8, 1), 'disk2': os.makedev(8, 1)})
        self.assertFalse(ok)
        self.assertIn('SAME filesystem (device 8:1)', line)
        self.assertIn('not distinct', line)

    def test_separate_filesystems(self):
        ok, line = kit.describe_disks({'disk1': os.makedev(8, 1), 'disk2': os.makedev(7, 3)})
        self.assertTrue(ok)
        self.assertIn('8:1 and 7:3', line)

    def test_missing_disk(self):
        ok, line = kit.describe_disks({'disk1': 1})
        self.assertFalse(ok)
        self.assertIn('disk2 missing', line)


class FakeSystem:
    """Records commands; 'mount'/'umount' flip whether disk2 counts as mounted."""

    def __init__(self, disk, mounted=False):
        self.disk, self.mounted, self.commands = str(disk), mounted, []

    def run(self, argv, check=True):
        self.commands.append(list(argv))
        if 'mount' in argv:
            self.mounted = True
        if 'umount' in argv:
            self.mounted = False

    def ismount(self, path):
        return str(path) == self.disk and self.mounted

    def patches(self, euid=1000):
        return [mock.patch.object(kit.subprocess, 'run', self.run),
                mock.patch.object(kit.os.path, 'ismount', self.ismount),
                mock.patch.object(kit.os, 'geteuid', return_value=euid),
                mock.patch.object(kit.shutil, 'which', lambda name: '/usr/bin/' + name)]


class KitFolder(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name) / 'mtest' / 'root'
        env = {'MTEST_ROOT': str(self.base), 'MTEST_PUID': str(os.getuid()), 'MTEST_PGID': str(os.getgid())}
        self.env = mock.patch.dict(os.environ, env)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def run_with(self, fake, func, *args, **kw):
        with contextlib.ExitStack() as stack:
            for p in fake.patches():
                stack.enter_context(p)
            out = io.StringIO()
            stack.enter_context(contextlib.redirect_stdout(out))
            stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
            result = func(*args, **kw)
        return result, out.getvalue()

    def test_init_no_loop_warns_about_cross_disk(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(kit.main(['init', '--no-loop']), 0)
        self.assertTrue((self.base / 'disk2' / 'TV' / 'Current').is_dir())
        self.assertEqual((self.base / 'disk2').stat().st_mode & 0o7777, 0o2775)
        self.assertIn('WARNING: disk1 and disk2 are on the SAME filesystem', out.getvalue())

    def test_init_makes_and_mounts_image_announcing_sudo_first(self):
        fake = FakeSystem(self.base / 'disk2')
        with mock.patch.object(kit, 'find_mkfs', return_value='/usr/sbin/mkfs.ext4'):
            code, out = self.run_with(fake, kit.main, ['init', '--disk2-size', '1G'])
        self.assertEqual(code, 0)
        image = str(self.base / 'disk2.img')
        self.assertEqual(fake.commands, [
            ['truncate', '-s', str(GIB), image],
            ['/usr/sbin/mkfs.ext4', '-q', '-F', image],
            ['sudo', 'mount', '-o', 'loop,nosuid,nodev', image, str(self.base / 'disk2')]])
        self.assertIn('sudo mount -o loop,nosuid,nodev', out)
        self.assertIn('separate filesystem', out)

    def test_init_twice_leaves_the_mount(self):
        fake = FakeSystem(self.base / 'disk2', mounted=True)
        code, out = self.run_with(fake, kit.main, ['init'])
        self.assertEqual(code, 0)
        self.assertEqual(fake.commands, [])
        self.assertIn('already a mounted filesystem; leaving it', out)

    def test_init_refuses_to_hide_files_in_old_disk2(self):
        with quiet():
            kit.main(['init', '--no-loop'])
        (self.base / 'disk2' / 'Movies' / 'Library' / 'x.mkv').write_text('x')
        fake = FakeSystem(self.base / 'disk2')
        with mock.patch.object(kit, 'find_mkfs', return_value='mkfs.ext4'):
            code, _ = self.run_with(fake, kit.main, ['init'])
        self.assertEqual(code, 1)
        self.assertEqual(fake.commands, [])

    def test_init_replaces_an_old_empty_folder_tree(self):
        with quiet():
            kit.main(['init', '--no-loop'])
        fake = FakeSystem(self.base / 'disk2')
        with mock.patch.object(kit, 'find_mkfs', return_value='mkfs.ext4'):
            code, _ = self.run_with(fake, kit.main, ['init'])
        self.assertEqual(code, 0)
        self.assertEqual(fake.commands[-1][:2], ['sudo', 'mount'])

    def test_up_after_reboot_remounts(self):
        self.base.mkdir(parents=True)
        (self.base / 'disk2').mkdir()
        (self.base / 'disk2.img').write_bytes(b'')
        fake = FakeSystem(self.base / 'disk2')
        self.assertTrue(self.run_with(fake, kit.ensure_loop_disk, self.base)[0])
        self.assertEqual([c[:2] for c in fake.commands], [['sudo', 'mount']])

    def test_up_refuses_to_mount_under_running_containers(self):
        self.base.mkdir(parents=True)
        (self.base / 'disk2.img').write_bytes(b'')
        fake = FakeSystem(self.base / 'disk2')
        with self.assertRaises(kit.KitError):
            self.run_with(fake, kit.ensure_loop_disk, self.base, running={'mtest-radarr'})
        self.assertEqual(fake.commands, [])

    def test_up_without_image_does_nothing(self):
        self.base.mkdir(parents=True)
        fake = FakeSystem(self.base / 'disk2')
        self.assertFalse(self.run_with(fake, kit.ensure_loop_disk, self.base)[0])
        self.assertEqual(fake.commands, [])

    def test_down_delete_unmounts_before_deleting(self):
        with quiet():
            kit.main(['init', '--no-loop'])
        fake = FakeSystem(self.base / 'disk2', mounted=True)
        with mock.patch.object(kit, 'compose'):
            code, _ = self.run_with(fake, kit.main, ['down', '--delete'])
        self.assertEqual(code, 0)
        self.assertEqual(fake.commands, [['sudo', 'umount', str(self.base / 'disk2')]])
        self.assertFalse(self.base.exists())

    def test_plain_down_leaves_the_mount(self):
        with quiet():
            kit.main(['init', '--no-loop'])
        fake = FakeSystem(self.base / 'disk2', mounted=True)
        with mock.patch.object(kit, 'compose'):
            code, out = self.run_with(fake, kit.main, ['down'])
        self.assertEqual(code, 0)
        self.assertEqual(fake.commands, [])
        self.assertIn('stays mounted', out)
        self.assertTrue(self.base.exists())


class DockerCheck(unittest.TestCase):
    """up and seed say what to do when Docker is missing or needs sudo (not a traceback)."""

    def check(self, which, returncode=0, stderr=''):
        result = mock.Mock(returncode=returncode, stderr=stderr, stdout='')
        return lambda: kit.require_docker(run=lambda *a, **k: result, which=lambda name: which)

    def test_missing(self):
        with self.assertRaisesRegex(kit.KitError, 'apt install docker.io docker-compose-v2'):
            self.check(None)()

    def test_needs_sudo(self):
        with self.assertRaisesRegex(kit.KitError, 'log out of this machine and back in'):
            self.check('/usr/bin/docker', 1, 'permission denied while trying to connect to the docker API')()

    def test_not_running(self):
        with self.assertRaisesRegex(kit.KitError, 'not answering'):
            self.check('/usr/bin/docker', 1, 'Cannot connect to the Docker daemon')()

    def test_fine(self):
        self.assertIsNone(self.check('/usr/bin/docker')())

    def test_seed_reports_instead_of_crashing(self):
        err = io.StringIO()
        with mock.patch.object(kit, 'require_docker', side_effect=kit.KitError('Docker is not installed.')), \
                contextlib.redirect_stderr(err):
            self.assertEqual(kit.main(['seed']), 1)
        self.assertIn('kit: Docker is not installed.', err.getvalue())


class RemoveReadOnlyTree(unittest.TestCase):
    """Run snapshots are read-only on purpose; down --delete must still remove them without sudo."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.cleanup)
        self.base = Path(self.tmp.name) / 'root'
        run = self.base / 'migratarr' / 'runs' / '20261029-120000'
        (run / 'code').mkdir(parents=True)
        for f in (run / 'move_plan.csv', run / 'SHA256SUMS', run / 'code' / 'plan.py'):
            f.write_text('x')
            f.chmod(0o444)
        for d in (run / 'code', run, run.parent):
            d.chmod(0o555)
        self.run_dir = run
        self.outside = Path(self.tmp.name) / 'outside'
        self.outside.mkdir()
        self.outside.chmod(0o555)
        (self.base / 'link-out').symlink_to(self.outside)

    def cleanup(self):
        for folder, subfolders, _ in os.walk(self.tmp.name):
            for name in subfolders:
                path = os.path.join(folder, name)
                if not os.path.islink(path):
                    os.chmod(path, 0o755)
        self.tmp.cleanup()

    def test_folders_become_owner_writable_but_not_through_symlinks(self):
        kit.make_tree_writable(self.base)
        self.assertEqual(self.run_dir.stat().st_mode & 0o777, 0o755)
        self.assertEqual((self.run_dir / 'code').stat().st_mode & 0o777, 0o755)
        self.assertEqual(self.outside.stat().st_mode & 0o777, 0o555)

    def test_remove_tree_deletes_read_only_snapshot(self):
        with quiet():
            kit.remove_tree(self.base)
        self.assertFalse(self.base.exists())
        self.assertTrue(self.outside.is_dir())
        self.assertEqual(self.outside.stat().st_mode & 0o777, 0o555)


if __name__ == '__main__':
    unittest.main()
