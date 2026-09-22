"""Offline characterization of monitored executor subprocess handling."""
import subprocess
import unittest
from unittest.mock import patch

from test_executor_safety import OfflineTest


class FakeProcess:
    def __init__(self, outcomes, returncode=0):
        self.outcomes = list(outcomes)
        self.returncode = returncode
        self.calls = []
        self.killed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def communicate(self, input=None, timeout=None):
        self.calls.append((input, timeout))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def kill(self):
        self.killed = True


class CommandRunnerTests(OfflineTest):
    def setUp(self):
        super().setUp()
        self.events = []
        for module in self.modules.values():
            if hasattr(module, 'progress'):
                module.progress.side_effect = self.events.append

    def run_with(self, module, process, *, input_data=None, timeout=30, monotonic=(0, 0, 1)):
        with patch('executor_command.subprocess.Popen', return_value=process) as popen, \
                patch('executor_command.time.monotonic', side_effect=monotonic):
            result = module.run_progress(['verify'], 'verification', input=input_data,
                                         timeout=timeout)
        return result, popen

    def test_all_wrappers_return_stdout_and_preserve_input_mode(self):
        for name in ('execute_movie_nas', 'execute_tv_nas', 'execute_cross_movie'):
            process = FakeProcess([('verified\n', '')])
            with self.subTest(executor=name):
                output, popen = self.run_with(self.modules[name], process, input_data='payload')
                self.assertEqual(output, 'verified\n')
                self.assertEqual(process.calls, [('payload', 15)])
                self.assertIs(popen.call_args.kwargs['stdin'], subprocess.PIPE)
                self.assertEqual(self.events[-2:],
                                 ['verification started', 'verification complete; elapsed 1s'])

    def test_timeout_wait_reuses_buffer_without_resending_input(self):
        timeout = subprocess.TimeoutExpired(['verify'], 15)
        process = FakeProcess([timeout, ('done', '')])
        output, _ = self.run_with(self.modules['execute_movie_nas'], process,
                                  input_data='payload', monotonic=(0, 0, 3, 3, 4))
        self.assertEqual(output, 'done')
        self.assertEqual(process.calls, [('payload', 15), (None, 15)])
        self.assertIn('verification still running; elapsed 3s', self.events)
        self.assertFalse(process.killed)

    def test_total_timeout_kills_reaps_and_reraises(self):
        first = subprocess.TimeoutExpired(['verify'], 1)
        process = FakeProcess([first, ('', '')])
        with patch('executor_command.subprocess.Popen', return_value=process), \
                patch('executor_command.time.monotonic', side_effect=(0, 0, 1, 2)):
            with self.assertRaises(subprocess.TimeoutExpired):
                self.modules['execute_tv_nas'].run_progress(
                    ['verify'], 'verification', input='payload', timeout=1)
        self.assertTrue(process.killed)
        self.assertEqual(process.calls, [('payload', 1), (None, None)])

    def test_nonzero_exit_uses_executor_refusal_type_and_stderr(self):
        for name in ('execute_movie_nas', 'execute_tv_nas', 'execute_cross_movie'):
            process = FakeProcess([('', 'uncertain state')], returncode=7)
            with self.subTest(executor=name), \
                    patch('executor_command.subprocess.Popen', return_value=process), \
                    patch('executor_command.time.monotonic', side_effect=(0, 0)):
                with self.assertRaisesRegex(self.modules[name].Refused,
                                            'failed or is uncertain: uncertain state'):
                    self.modules[name].run_progress(['verify'], 'verification')
            self.assertFalse(process.killed)

    def test_unexpected_communicate_error_kills_and_reaps(self):
        process = FakeProcess([OSError('read failed'), ('', '')])
        with patch('executor_command.subprocess.Popen', return_value=process), \
                patch('executor_command.time.monotonic', side_effect=(0, 0)):
            with self.assertRaises(OSError):
                self.modules['execute_cross_movie'].run_progress(['verify'], 'verification')
        self.assertTrue(process.killed)
        self.assertEqual(process.calls[-1], (None, None))


if __name__ == '__main__':
    unittest.main()
