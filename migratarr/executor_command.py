"""Shared monitored subprocess runner for executor verification commands."""
import re
import subprocess
import threading
import time

# NAS operations report progress on stderr (every 15 s while hashing, once per
# copied file). A NAS call is treated as stalled only after this long with no
# output at all; there is no total limit, because the work scales with the data
# (a copy re-hashes the source and the copy several times).
NAS_IDLE_TIMEOUT = 1800


def exit_text(returncode):
    """How a command ended, for the STOPPED reason: a signal is a cause in itself.

    Incident 20261023T212605Z-0031 (NAS copy ended with no output) could not be
    diagnosed afterwards because only stderr was kept.
    """
    import signal
    def name(number):
        try:
            return signal.Signals(number).name
        except ValueError:
            return 'signal ' + str(number)
    if returncode < 0:
        return 'killed by ' + name(-returncode) + ' on this host'
    if returncode == 255:
        return 'exit status 255: an SSH error, or the NAS program ended by a signal'
    if returncode > 128:
        return f'exit status {returncode}, which a shell reports for {name(returncode - 128)}'
    return f'exit status {returncode}'


def with_status(output, returncode):
    output = output.strip()
    return (output + ' ' if output else '') + '[' + exit_text(returncode) + ']'


TRACEBACK = 'Traceback (most recent call last):'
# Refusals the shipped program makes before it changes anything, in words for the user. The program's
# own text is pinned byte for byte (tests/test_nas_program_parity.py), so it is translated here.
PLAIN_REFUSALS = {
    'NAS filesystems are not distinct':
        "the source and target disks are on the same filesystem, so a cross-disk move can't run between "
        'them. Use two different disks, or put both categories on one disk.',
}
_FAILED = ' failed or is uncertain: '
_REFUSED_AT_END = re.compile(r'(?s)(.*?)' + re.escape(_FAILED) + r'(.*)\n(?:[\w.]+\.)?Refused: ([^\n]*?)'
                             r'( \[[^\]\n]*\])?')


def is_refusal_line(line):
    """The last line of a traceback for the shipped program's own Refused: its message, else None."""
    found = re.fullmatch(r'(?:[\w.]+\.)?Refused: (.*)', line.strip())
    return found.group(1) if found else None


def plain_reason(reason):
    """A STOPPED reason without the Python traceback of a shipped program's deliberate refusal.

    "<label> failed or is uncertain: <traceback> Refused: <message> [exit status 1]" becomes
    "<label> failed or is uncertain: Refused: <message> [exit status 1]", or, for a refusal listed in
    PLAIN_REFUSALS, "<label> refused: <what to do>". Anything else is returned unchanged (other errors
    keep their traceback: it is what diagnoses them). Also used to show journals written before this.
    """
    if not isinstance(reason, str):
        return reason
    found = _REFUSED_AT_END.fullmatch(reason.strip())
    if not found:
        return reason
    label, before, message, status = found.groups()
    if TRACEBACK not in before and '\n  File "' not in '\n' + before:
        return reason  # plain output that merely ends in a Refused line: already one reason per line
    if message in PLAIN_REFUSALS:
        return label + ' refused: ' + PLAIN_REFUSALS[message]
    return label + _FAILED + 'Refused: ' + message + (status or '')


def failure(label, output, returncode):
    return plain_reason(label + _FAILED + with_status(output, returncode))


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
        require(process.returncode == 0, failure(label, stderr, process.returncode))
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
            # A traceback is held back until its last line: for the program's own refusal only that line
            # is shown (the failure reason says the same); any other error is shown in full.
            held = []
            for line in process.stderr:
                line = line.rstrip('\n')
                with lock:
                    state['last'] = time.monotonic()
                    state['tail'] = (state['tail'] + [line])[-tail_lines:]
                if line.strip() == TRACEBACK or (held and (not line.strip() or line[:1].isspace())):
                    held.append(line)
                    continue
                if held and is_refusal_line(line) is not None:
                    held = []
                for shown in held + [line]:
                    if shown.strip():
                        progress(label + ': ' + shown.strip())
                held = []
            for shown in held:
                if shown.strip():
                    progress(label + ': ' + shown.strip())

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
        require(process.returncode == 0, failure(label, tail, process.returncode))
    progress('%s complete; elapsed %.0fs' % (label, time.monotonic() - started))
    return out[0] if out else ''
