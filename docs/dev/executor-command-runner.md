# Shared monitored command runner

`executor_command.py` contains the subprocess monitoring loop shared by the NAS
Movie, TV, cross-disk Movie and cross-disk TV executors. Executor-local
`run_progress` signatures and their existing default timeouts remain unchanged:
1,800 seconds for same-disk executors and 7,200 seconds for cross-disk transfers.

Each wrapper passes its existing `require` and `progress` callbacks. That keeps
executor-specific `Refused` types, failure messages and progress output intact.
The shared runner has no knowledge of media, manifests, Arr, SSH, Docker or
filesystem mutation.

The runner retains these safety behaviors:

- stdin is sent once; after a timed wait, buffered input is not resent;
- status is reported every timed wait;
- the overall deadline is enforced independently of the polling interval;
- timeout or any unexpected communication error kills and reaps the child;
- a nonzero exit refuses as failed or uncertain and includes stripped stderr;
- successful stdout is returned unchanged.

Offline tests use an inert process double and cover all four wrappers, stdin
mode, timed polling, overall timeout, kill/reap, unexpected communication errors,
nonzero exits and executor-specific refusal types.

Deploy `executor_command.py` with all four updated executors. A partial install
fails closed during import. Run:

```sh
python3 -m unittest discover -s tests -v
```
