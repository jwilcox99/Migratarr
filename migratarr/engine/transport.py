"""Where an executor's NAS-side program runs: on the NAS over SSH, or on this host.

Every executor builds one standard-library program (nas_side.py plus its own
helpers, pinned by tests/golden/nas_programs.json) and pipes a JSON request to
it on stdin. The transport only decides how that program is started:

  nas_ssh  ssh <options> <nas.user>@<nas.host> <nas.python> -c <program>
           through one SSH control master per executor run (unchanged from
           before this module: same options, same commands)
  local    <this Python> -c <program>, with the "remote" disk roots equal to the
           local ones (runtime_config.storage_from_targets), so the same
           program renames, copies and deletes on this host's own disks

runtime.json `topology` picks one. The program, its requests, receipts,
locks and every journal event are the same in both; only the process that
runs the program differs.
"""
import shlex
import subprocess
import sys
import tempfile

SSH_OPTIONS = ('-o', 'IdentitiesOnly=yes',
               '-o', 'BatchMode=yes',
               '-o', 'StrictHostKeyChecking=yes', '-o', 'ConnectTimeout=15',
               '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=3')


class NasChannel:
    """Base for each executor's NasTransport. Subclasses define `runtime` and `call()`.

    `runtime` is read at use time (a property returning the executor's RUNTIME),
    so tests that patch an executor's RUNTIME are honored.
    """
    runtime = None
    temp = None

    @property
    def local(self):
        return self.runtime.topology == 'local'

    def __enter__(self):
        if self.local:
            self.temp, self.options = None, []
            return self
        self.temp = tempfile.TemporaryDirectory(prefix='migratarr-ssh-')
        self.options = ['-i', self.runtime.ssh_key, *SSH_OPTIONS,
                        '-o', 'ControlPath=' + self.temp.name + '/socket']
        try:
            # Dedicated key only; never wait for a terminal password prompt.
            subprocess.run(['ssh', *self.options, '-M', '-N', '-f',
                            '-o', 'ControlPersist=yes', self.runtime.ssh_target], check=True, timeout=120)
        except BaseException:
            self.temp.cleanup()
            raise
        return self

    def __exit__(self, *exc):
        if self.temp is None:
            return
        try:
            subprocess.run(['ssh', *self.options, '-O', 'exit', self.runtime.ssh_target],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
        finally:
            self.temp.cleanup()

    def command(self, program):
        """argv that runs `program` where moves happen."""
        if self.local:
            # -I: ignore PYTHON* variables and the user site folder, so the program
            # sees only the standard library, as on the NAS.
            return [sys.executable, '-I', '-c', program]
        return ['ssh', *self.options, '-o', 'BatchMode=yes', self.runtime.ssh_target,
                shlex.quote(self.runtime.remote_python) + ' -c ' + shlex.quote(program)]

    def where(self):
        return 'this host' if self.local else 'the NAS'
