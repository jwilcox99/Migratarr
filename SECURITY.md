# Security Policy

Migratarr moves real files on real storage and talks to Radarr, Sonarr,
Jellyfin, and a NAS over SSH with real credentials. Treat anything here
seriously.

## Reporting a vulnerability

Please report suspected vulnerabilities privately rather than as a public
issue, using GitHub's private vulnerability reporting: the **Security** tab →
**Report a vulnerability** on this repository. Include what you found,
how to reproduce it, and — if it relates to a specific run — the relevant
`execution_logs/<execution_id>.jsonl` entry with any secrets redacted.

## How secrets are currently handled

- Every API key and the TMDB token are read at run time through
  `service_keys.py`, from the source named in `runtime.json` "secrets"
  ([Service credentials](docs/runtime-configuration.md#service-credentials)).
  By default: Radarr/Sonarr `ApiKey` from each container's
  `/config/config.xml` (`docker exec ... cat`, no shell, XML-parsed); the
  Jellyfin key from a secrets file mounted into the `homepage` dashboard
  container, not Jellyfin's own (`/run/secrets/jellyfin_api_key`, then
  `/run/secrets/jellyfin_key`); the TMDB token from `TMDB_TOKEN`.
- The pipeline never persists credentials it reads, and must not: new code
  gets keys from `service_keys`, not by caching them in a file, a snapshot, a
  journal or a parity recording (`dry_run_parity` replaces key lookups with a
  placeholder). Error messages name a credential's source, never its value.
- A `file` source is a secret *you* provision, like an SSH key. It is refused
  unless the file is owner-only (`chmod 600`) and outside the repository.
  Do not log a credential's value or include it in reports.
- The NAS SSH key is referenced by path (`~/.ssh/migratarr_nas`) and is
  expected to live outside the repository entirely, authenticated with
  `IdentitiesOnly` and `BatchMode` so it never falls back to a password
  prompt. Don't add code that reads or logs key material.
- `execute_movie_nas.py`'s Radarr client explicitly disables HTTP
  redirects (`NoRedirect`) so an API key can't be forwarded to a
  redirected host. Preserve this if you touch that client.

## Risk surface specific to this project

- **File operations are one-way in practice.** Renames use `renameat2`
  with `RENAME_NOREPLACE` (atomic, collision-safe, no partial overwrite),
  but there is no automatic undo — recovery from a failed live attempt is
  manual, guided by the JSON journal.
- **The executor has broad filesystem access** by design (it has to, to
  move media) and broad Radarr/Sonarr write access (to update paths after
  a move). A bug in path validation is a data-loss or data-corruption
  bug, not just a logic bug. Treat PRs touching `execute_*.py`,
  `paths()`, `filesystem_ready()`, or the NAS transport as security-review
  scope, not routine review.
- **`docker exec` access implies host-level trust.** Anything that can
  run this pipeline can already read your Radarr/Sonarr API keys and
  reach your NAS. Migratarr doesn't change that trust boundary, but it's
  worth stating plainly for anyone evaluating whether to run it.

## Supported versions

`main` is the supported development line. Milestone tags exist, but they
are not separate maintained release lines. See the README's
[Safety model](README.md#safety-model) and
[Known limitations](README.md#known-limitations), plus the
[Phase One closeout](docs/phase-one-closeout.md), for current boundaries.
