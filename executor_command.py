"""Shared monitored subprocess runner for executor verification commands."""
import subprocess
import threading
import time

# NAS operations report progress on stderr (every 15 s while hashing, once per
# copied file). A NAS call is treated as stalled only after this long with no
# output at all; there is no total limit, because the work scales with the data
# (a copy re-hashes the source and the copy several times).
NAS_IDLE_TIMEOUT = 1800


def run_command(command, label, input_data, timeout, *, require, progress, idle_timeout=None):
    if idle_timeout is not None:
        return _run_until_idle(command, label, input_data, timeout, idle_timeout,
                               require=require, progress=progress)
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


def _run_until_idle(command, label, input_data, timeout, idle_timeout, *, require, progress,
                    poll=1.0, tail_lines=20):
    """Run ``command`` and forward its stderr lines as progress.

    Killed (and ``subprocess.TimeoutExpired`` raised, naming only ``label`` so
    journals stay readable) when stderr has been silent for ``idle_timeout``
    seconds, or when the optional total ``timeout`` elapses.
    """
    started = time.monotonic()
    progress(label + ' started')
    state = dict(last=started, tail=[])
    lock = threading.Lock()
    with subprocess.Popen(
            command, stdin=subprocess.PIPE if input_data is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as process:
        out = []

        def read_stdout():
            out.append(process.stdout.read())

        def read_stderr():
            for line in process.stderr:
                line = line.rstrip('\n')
                with lock:
                    state['last'] = time.monotonic()
                    state['tail'] = (state['tail'] + [line])[-tail_lines:]
                if line.strip():
                    progress(label + ': ' + line.strip())

        readers = [threading.Thread(target=read_stdout, daemon=True),
                   threading.Thread(target=read_stderr, daemon=True)]
        try:
            for reader in readers:
                reader.start()
            if input_data is not None:
                try:
                    process.stdin.write(input_data)
                    process.stdin.close()
                except BrokenPipeError:
                    pass  # the command exited early; its return code decides below
            while process.poll() is None:
                now = time.monotonic()
                with lock:
                    idle = now - state['last']
                if idle >= idle_timeout:
                    raise subprocess.TimeoutExpired(label, idle_timeout,
                                                    stderr='no progress for %.0fs' % idle)
                if timeout is not None and now - started >= timeout:
                    raise subprocess.TimeoutExpired(label, timeout)
                time.sleep(poll)
            for reader in readers:
                reader.join()
        except BaseException:
            process.kill()
            process.wait()
            raise
        with lock:
            tail = '\n'.join(state['tail'])
        require(process.returncode == 0, label + ' failed or is uncertain: ' + tail.strip())
    progress('%s complete; elapsed %.0fs' % (label, time.monotonic() - started))
    return out[0] if out else ''
