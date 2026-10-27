# Security policy

Migratarr moves real files on real disks, and uses real API keys for Radarr,
Sonarr, Jellyfin and TMDB and an SSH key for your NAS. This page covers how
it handles them and how to report a problem.

## Reporting a vulnerability

Report suspected vulnerabilities privately, not as a public issue: on this
repository, open the **Security** tab and choose **Report a vulnerability**
(GitHub private vulnerability reporting). Include what you found, how to
reproduce it and, if it relates to a move, that move's journal
(`migratarr status --execution <id>`) with any keys, host names and paths you
consider private removed.

## API keys

### Where keys come from

You choose a source for each service in the setup wizard or in **Settings &
health**. `runtime.json` records only the source (a container, a file path or
a variable name), never a key.

| Source | Reads |
| --- | --- |
| Docker container's `config.xml` (Radarr and Sonarr default) | `docker exec <container> cat /config/config.xml`, run without a shell |
| `config.xml` on this machine | Radarr's or Sonarr's own config file, for installs without Docker |
| A file inside a Docker container | The first non-empty file of a list, such as a secret mounted into a dashboard container |
| A file on this machine | A file you create. Refused unless only its owner can read it (`chmod 600`) and it is outside the Migratarr code folder |
| An environment variable | Such as `TMDB_TOKEN` (TMDB's default) |
| Pasted into the UI | Stored as below |

### Pasted keys

A key you paste into the wizard or the settings pages is written, when you
save, to an owner-only file:

```
~/.config/migratarr/secrets/<service>     folder 0700, file 0600
```

`MIGRATARR_SECRETS_DIR` changes the folder. Migratarr refuses a folder inside
its own code folder, and refuses anything that isn't a single line of
printable text. `runtime.json` then records only the path to that file (a
`file` source), and the key is read back from it with the same owner-only
check. Until you save, a pasted key is held only in the UI's memory.

Switching a service to another source doesn't delete its file; delete
`~/.config/migratarr/secrets/<service>` yourself if you no longer need it.

### Where keys never appear

- **Pages:** a saved key is never put back into a page or form. Reviews and
  change previews show "new key", not the key.
- **Health checks:** details hold only HTTP status codes and error type names,
  never a response body.
- **Logs, journals and error messages:** errors name where a key comes from,
  never its value. Run snapshots record settings without keys.

The move executors and the health checks don't follow HTTP redirects, so a
key can't be forwarded to another host.

## The web UI

- It listens on 127.0.0.1 only. To use it from another computer, use an SSH
  tunnel: `ssh -L 8765:localhost:8765 media-host.example`.
- It answers only requests addressed to `localhost`, `127.0.0.1` or `::1`, and
  refuses any other `Host` header (protection against DNS rebinding).
- Every action is a form carrying a token made when the UI starts. Requests
  from other sites are refused.
- **There is no login.** Anyone who can connect to that port on the machine,
  including other users of it, can use the UI. Run it on a machine you don't
  share, or with `migratarr ui --read-only`, which switches every action off.

## The NAS SSH key

The key is referenced by path in `runtime.json` and should be a key used only
by Migratarr, kept outside the data folder. Migratarr connects with that key
only (`IdentitiesOnly`), never prompts for a password (`BatchMode`), and
refuses a NAS that isn't already in your `known_hosts`
(`StrictHostKeyChecking`).

## Settings files

`runtime.json`, `storage-targets.json` and `planner.json` hold no keys. Each
save first copies the old file to `config/backups/<time>/` (folder 0700) and
keeps the file's permissions.

## What running Migratarr means

- **There is no undo.** Moves don't overwrite anything and are checked before
  and after, but a completed move stays done. A move that stops part way is
  recovered by hand, guided by its journal.
- **Migratarr needs broad access by design:** it reads and writes your media
  folders and updates paths in Radarr and Sonarr. A path-checking bug could
  lose or damage data, so treat changes to the executors, path checks or the
  NAS connection as security-sensitive in review.
- **`docker exec` access is host-level trust.** By default Migratarr reads
  keys and checks files inside the Radarr and Sonarr containers. Anything
  that can run Migratarr as that user can already read those keys and reach
  your NAS. Migratarr doesn't change that, but consider it before running it.

## Supported versions

Security fixes go into the latest release. 0.x versions are not maintained in
parallel.
