"""Shared monitored subprocess runner for executor verification commands."""
import subprocess
import time


def run_command(command, label, input_data, timeout, *, require, progress):
    started = time.monotonic()
    progress(label + ' started')
    with subprocess.Popen(
            command, stdin=subprocess.PIPE if input_data is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as process:
        try:
            while True:
                remaining = timeout - (time.monotonic() - started)
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(command, timeout)
                try:
                    stdout, stderr = process.communicate(
                        input=input_data, timeout=min(15, remaining))
                    break
                except subprocess.TimeoutExpired:
                    # communicate retains buffered input across timeout waits.
                    input_data = None
                    progress('%s still running; elapsed %.0fs'
                             % (label, time.monotonic() - started))
        except BaseException:
            process.kill()
            process.communicate()
            raise
        require(process.returncode == 0,
                label + ' failed or is uncertain: ' + stderr.strip())
    progress('%s complete; elapsed %.0fs' % (label, time.monotonic() - started))
    return stdout
