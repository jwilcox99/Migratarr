"""Idle-based timeout for NAS calls: progress keeps a long call alive, silence kills it.

Runs real short-lived Python subprocesses (no NAS, Docker or network).
"""
import importlib
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

import executor_command
import test_executor_safety as safety

REAL_SLEEP = time.sleep
FAST_POLL = dict(side_effect=lambda seconds: REAL_SLEEP(0.05))


class Refused(RuntimeError):
    pass


def require(ok, message):
    if not ok:
        raise Refused(message)


def script(body):
    return [sys.executable, '-c', 'import sys, time\n' + body]


class IdleTimeoutTests(unittest.TestCase):
    def run_cmd(self, command, *, input_data=None, timeout=None, idle=1.0):
        events = []
        result = executor_command.run_command(command, 'NAS copy', input_data, timeout, require=require,
                                              progress=events.append, idle_timeout=idle)
        return result, events

    def test_long_call_with_steady_progress_is_not_killed(self):
        # Runs ~2.4 s, three times the idle limit, but reports every 0.3 s.
        command = script('data = sys.stdin.read()\n'
                         'for i in range(8):\n'
                         '    print("Hashed %d" % i, file=sys.stderr, flush=True); time.sleep(0.3)\n'
                         'print(data.upper())')
        with patch.object(executor_command.time, 'sleep', **FAST_POLL):
            output, events = self.run_cmd(command, input_data='ok', idle=0.8)
        self.assertEqual(output.strip(), 'OK')
        self.assertIn('NAS copy: Hashed 7', events)
        self.assertTrue(events[-1].startswith('NAS copy complete'))

    def test_silence_kills_and_raises_short_timeout(self):
        command = script('print("start", file=sys.stderr, flush=True); time.sleep(30)')
        started = time.monotonic()
        with patch.object(executor_command.time, 'sleep', **FAST_POLL):
            with self.assertRaises(subprocess.TimeoutExpired) as caught:
                self.run_cmd(command, idle=0.5)
        self.assertLess(time.monotonic() - started, 10)
        # Journals record str(exc): it must name the label, not the whole shipped program.
        self.assertEqual(str(caught.exception), "Command 'NAS copy' timed out after 0.5 seconds")

    def test_optional_total_timeout_still_applies(self):
        command = script('while True:\n    print(".", file=sys.stderr, flush=True); time.sleep(0.1)')
        with patch.object(executor_command.time, 'sleep', **FAST_POLL):
            with self.assertRaises(subprocess.TimeoutExpired):
                self.run_cmd(command, timeout=0.6, idle=5)

    def test_failure_reports_last_stderr_lines(self):
        command = script('print("hashing", file=sys.stderr)\nprint("Refused: Destination exists", file=sys.stderr)\n'
                         'sys.exit(1)')
        with patch.object(executor_command.time, 'sleep', **FAST_POLL):
            with self.assertRaisesRegex(Refused, 'NAS copy failed or is uncertain: hashing\nRefused: Destination exists'):
                self.run_cmd(command)

    def test_refusal_traceback_becomes_one_line(self):
        command = script('print("hashing", file=sys.stderr, flush=True)\n'
                         'class Refused(RuntimeError): pass\n'
                         'def check():\n    raise Refused("Destination exists")\n'
                         'check()')
        with patch.object(executor_command.time, 'sleep', **FAST_POLL):
            with self.assertRaises(Refused) as caught:
                executor_command.run_command(command, 'NAS copy', None, None, require=require,
                                             progress=(events := []).append, idle_timeout=5)
        self.assertEqual(str(caught.exception), 'NAS copy failed or is uncertain: Refused: Destination exists '
                                                '[exit status 1]')
        self.assertEqual(events[1:3], ['NAS copy: hashing', 'NAS copy: Refused: Destination exists'])
        self.assertFalse(any('Traceback' in e or 'File "' in e for e in events), events)

    def test_known_refusal_says_what_to_do(self):
        command = script('class Refused(RuntimeError): pass\nraise Refused("NAS filesystems are not distinct")')
        with patch.object(executor_command.time, 'sleep', **FAST_POLL):
            with self.assertRaisesRegex(Refused, r"^NAS check refused: the source and target disks are on the "
                                                 r"same filesystem, so a cross-disk move can't run between them\. "
                                                 r"Use two different disks, or put both categories on one disk\.$"):
                executor_command.run_command(command, 'NAS check', None, None, require=require,
                                             progress=lambda m: None, idle_timeout=5)

    def test_other_errors_keep_their_traceback(self):
        command = script('def f():\n    raise OSError(28, "No space left on device")\nf()')
        with patch.object(executor_command.time, 'sleep', **FAST_POLL):
            with self.assertRaises(Refused) as caught:
                executor_command.run_command(command, 'NAS copy', None, None, require=require,
                                             progress=(events := []).append, idle_timeout=5)
        self.assertIn('Traceback (most recent call last):', str(caught.exception))
        self.assertIn('OSError: [Errno 28] No space left on device', str(caught.exception))
        self.assertIn('NAS copy: Traceback (most recent call last):', events)
        self.assertIn('NAS copy: OSError: [Errno 28] No space left on device', events)

    def test_plain_reason_for_journals_written_before(self):
        old = ('NAS check failed or is uncertain: Traceback (most recent call last):\n'
               '  File "<string>", line 9, in <module>\n  File "<string>", line 4, in nas_operation\n'
               'Refused: NAS filesystems are not distinct [exit status 1]')
        self.assertTrue(executor_command.plain_reason(old).startswith('NAS check refused: the source and target'))
        for unchanged in ('NAS copy failed or is uncertain: hashing\nRefused: Destination exists [exit status 1]',
                          'Destination content mismatch', None):
            self.assertEqual(executor_command.plain_reason(unchanged), unchanged)

    def test_early_exit_while_sending_input_is_reported_not_crashed(self):
        command = script('sys.exit(3)')
        with patch.object(executor_command.time, 'sleep', **FAST_POLL):
            with self.assertRaises(Refused):
                self.run_cmd(command, input_data='x' * 1_000_000)


class NasTransportUsesIdleTimeoutTests(safety.OfflineTest):
    """Every executor's NAS call uses the idle limit and no total limit; local checks keep theirs."""

    def test_all_four_nas_transports(self):
        cases = {
            'execute_cross_movie': ('/mnt/nas/media04/Movies/Library/X', '/mnt/nas/media01/Movies/Common/X'),
            'execute_cross_tv': ('/mnt/nas/media04/TV/Library/X', '/mnt/nas/media01/TV/Current/X'),
            'execute_movie_nas': ('/mnt/nas/media04/Movies/Library/X', '/mnt/nas/media04/Movies/Common/X'),
            'execute_tv_nas': ('/mnt/nas/media04/TV/Library/X', '/mnt/nas/media04/TV/Current/X'),
        }
        from pathlib import Path
        for name, (src, dst) in cases.items():
            module = self.modules[name]
            with self.subTest(executor=name):
                transport = object.__new__(module.NasTransport)
                transport.options = []
                calls = []

                def fake_run(command, label, input=None, **kwargs):
                    calls.append(kwargs)
                    return '{"result": "OK", "operation": "check"}'
                with patch.object(module, 'run_progress', side_effect=fake_run), \
                        patch.object(module, 'remote_program', return_value='program'):
                    if 'cross' in name:
                        transport.call('check', Path(src), Path(dst), {}, safety.EXECUTION)
                    else:
                        transport.call('check', Path(src), Path(dst), {})
                self.assertEqual(calls, [dict(timeout=None, idle_timeout=executor_command.NAS_IDLE_TIMEOUT)])
                # Local (docker/host) Arr checks are unchanged: fixed total limit, no idle mode.
                self.assertEqual(module.run_progress.__defaults__[-1], None)


if __name__ == '__main__':
    unittest.main()
