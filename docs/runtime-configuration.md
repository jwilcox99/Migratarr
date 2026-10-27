# Runtime configuration (`runtime.json`)

`runtime.json` says where Migratarr keeps its records, where moves run, how to
reach the NAS, Radarr, Sonarr and Jellyfin, and where each API key comes from.
It holds no keys. Disks are not declared here: they are in
[storage-targets.json](storage-targets.md).

The setup wizard writes this file, and **Settings & health** in the UI edits
it. This page is the reference for reading or editing it by hand. How the
three settings files fit together: [Settings](settings.md).

**Location:** `$MIGRATARR_CONFIG` if set (use an absolute path), otherwise
`config/runtime.json` in the data folder. The data folder is `$MIGRATARR_HOME`,
else the git clone the code runs from, else `~/.local/share/migratarr`.
`migratarr doctor --offline` prints both and checks the file.

The current format is **version 2**. Version 1 files still load and are
migrated automatically; see [Version 1](#version-1-older-files).

## Examples

NAS over SSH (`config/runtime.example.json` in the repository):

```json
{
  "schema_version": 2,
  "base_path": "/opt/media-stack/migratarr",
  "topology": "nas_ssh",
  "containers": {"radarr": "radarr", "sonarr": "sonarr", "homepage": "homepage"},
  "urls": {
    "radarr": "http://localhost:7878",
    "sonarr": "http://localhost:8989",
    "jellyfin": "http://localhost:8096"
  },
  "nas": {
    "host": "nas.example",
    "user": "migratarr",
    "ssh_key": "~/.ssh/migratarr_nas",
    "python": "/usr/bin/python3"
  }
}
```

This machine's own disks, **experimental** (`config/examples/runtime.local.example.json`):

```json
{
  "schema_version": 2,
  "base_path": "/opt/migratarr",
  "topology": "local",
  "containers": {"radarr": "radarr", "sonarr": "sonarr", "homepage": "homepage"},
  "urls": {
    "radarr": "http://localhost:7878",
    "sonarr": "http://localhost:8989",
    "jellyfin": "http://localhost:8096"
  },
  "storage_targets": "storage-targets.local.example.json"
}
```

Every value above is an example. Replace each one with your own.

## Fields

| Field | Required | Meaning |
| --- | --- | --- |
| `schema_version` | yes | `2` |
| `base_path` | yes | Where runs, manifests, approvals, jobs and move journals go. The wizard sets it to the data folder |
| `topology` | yes | `nas_ssh` or `local`; see [Topology](#topology) |
| `containers` | yes (may be `{}`) | Docker container names: `radarr`, `sonarr`, `homepage`. Each is needed only where a Docker key source or [Arr file check](#arr-file-checks) uses it |
| `urls` | yes | `radarr`, `sonarr`, `jellyfin`; see [Service URLs](#service-urls) |
| `nas` | with `nas_ssh` only | `host`, `user`, `ssh_key`, `python`; see [NAS](#nas) |
| `storage_targets` | no | Path to storage-targets.json, absolute or relative to this file's folder. Default: `storage-targets.json` beside this file |
| `secrets` | no | Where each key comes from; see [Service credentials](#service-credentials) |
| `arr_file_checks` | no | How file checks reach Radarr's and Sonarr's view; see [Arr file checks](#arr-file-checks) |

A `storage` block is refused in version 2: disks belong in storage-targets.json.

The file is checked before Migratarr contacts any service or writes anything.
Unknown or duplicate fields, a missing required field, an unsupported
`schema_version`, non-canonical paths, malformed URLs and unsafe host, user or
container names are refused with a `Runtime configuration: ...` message. These
checks don't prove that disks, services or keys exist; `migratarr doctor`
does that. Paths expand `~` for the user running Migratarr; environment
variables inside values are not expanded.

## Topology

| `topology` | Moves run | `nas` | Each storage target's `remote_path` |
| --- | --- | --- | --- |
| `nas_ssh` | On the NAS, over SSH. Migratarr runs on a machine that mounts the NAS disks | required | required: the disk's root as the NAS sees it |
| `local` (experimental) | On this machine's own disks | not allowed | not allowed |

Both use the same move program, checks and journals; only where the program
runs differs. Moves on `local` have not yet been run end to end against a real
Radarr and Sonarr.

## NAS

| Field | Example | Rules |
| --- | --- | --- |
| `host` | `nas.example` | Host name or IP address |
| `user` | `migratarr` | The account moves run as on the NAS |
| `ssh_key` | `~/.ssh/migratarr_nas` | Private key used only by Migratarr; absolute after `~` expands |
| `python` | `/usr/bin/python3` | Python 3.6 or newer on the NAS, absolute path |

SSH uses only that key, never prompts for a password, and refuses a host not
already in `known_hosts`. `migratarr doctor` checks the connection, the NAS's
Python and that each disk exists and is writable on the NAS.

## Service URLs

`urls.radarr`, `.sonarr` and `.jellyfin` are HTTP(S) URLs with an optional
base path, for services served under a prefix (behind a reverse proxy, say):
`http://localhost:7878` or `https://proxy.example/radarr`. No credentials,
query, fragment or trailing slash.

- A base path written in `urls` is used as is.
- Otherwise Radarr and Sonarr add their own URL base, read from `config.xml`
  (or the `url_base` of a `file` or `env` key source). A Radarr with URL base
  `/radarr` works with `urls.radarr` left at `http://localhost:7878`.
- Jellyfin uses its URL as given; put any base path in `urls.jellyfin`.

## Service credentials

Without a `secrets` section, each service uses its default source:

```json
"secrets": {
  "radarr":   {"source": "docker_config_xml"},
  "sonarr":   {"source": "docker_config_xml"},
  "jellyfin": {"source": "docker_file",
               "paths": ["/run/secrets/jellyfin_api_key", "/run/secrets/jellyfin_key"]},
  "tmdb":     {"source": "env", "variable": "TMDB_TOKEN"}
}
```

List only the services you want to change.

| `source` | Fields | Reads |
| --- | --- | --- |
| `docker_config_xml` | `container`, `path` (both optional) | `ApiKey` and `UrlBase` from `docker exec <container> cat <path>`. Container defaults to `containers.radarr` / `.sonarr`, path to `/config/config.xml`. Radarr and Sonarr only |
| `config_xml` | `path` | The same from a `config.xml` on this machine, for installs without Docker. Radarr and Sonarr only |
| `docker_file` | `paths`, `container` (optional) | The first non-empty file of `paths` inside the container. Jellyfin's container defaults to `containers.homepage` |
| `file` | `path`, `url_base` (optional) | A file on this machine. Refused unless owner-only (`chmod 600`) and outside the Migratarr code folder |
| `env` | `variable`, `url_base` (optional) | An environment variable |

`url_base` is for Radarr and Sonarr sources that don't read `config.xml`.
Commands run inside containers without a shell. A missing or empty key stops
Migratarr before it contacts any service, and errors name the source, never
the value.

A key pasted into the UI is saved to `~/.config/migratarr/secrets/<service>`
and recorded here as a `file` source:

```json
"secrets": {"radarr": {"source": "file", "path": "/home/you/.config/migratarr/secrets/radarr"}}
```

To keep the TMDB token in a file yourself:

```sh
install -m 600 /dev/null ~/.config/migratarr/tmdb_token
$EDITOR ~/.config/migratarr/tmdb_token
```

```json
"secrets": {"tmdb": {"source": "file", "path": "~/.config/migratarr/tmdb_token"}}
```

More on how keys are kept: [SECURITY.md](../SECURITY.md).

## Arr file checks

Before and after every move, Migratarr confirms that Radarr or Sonarr can see
each file at the path it will use (under storage-targets.json `arr_root`) and
that the file hashes as expected. Without an `arr_file_checks` section both
services use `docker`:

```json
"arr_file_checks": {
  "radarr": {"mode": "docker"},
  "sonarr": {"mode": "host", "host_root": "/srv/pool/media"}
}
```

| `mode` | Fields | Checks |
| --- | --- | --- |
| `docker` | `container` (optional, default `containers.radarr` / `.sonarr`) | `test` and `sha256sum` inside the Radarr or Sonarr container |
| `host` | none | Radarr or Sonarr runs directly on this machine, so its paths are this machine's paths |
| `host` | `host_root` | Radarr or Sonarr sees `host_root` as `arr_root` (a bind mount); paths are translated and checked on this machine |

`docker` is the strongest check: it proves the container's own mounts show the
file. Use it when you can. `host` with `host_root` proves only that this
machine sees the file; a container mounted wrongly would go unnoticed. A path
outside `arr_root`, a missing file or a hash mismatch stops the move.

These settings are not in the UI; edit the file to change them.

## Environment overrides

After the file is checked, these environment variables replace single values
(and the result is checked again). An empty value is an error.

- `MIGRATARR_BASE_PATH`
- `MIGRATARR_NAS_HOST`, `MIGRATARR_NAS_USER`, `MIGRATARR_NAS_SSH_KEY` (ignored
  with `topology: local`)
- `MIGRATARR_RADARR_CONTAINER`, `MIGRATARR_SONARR_CONTAINER`, `MIGRATARR_HOMEPAGE_CONTAINER`
- `MIGRATARR_RADARR_URL`, `MIGRATARR_SONARR_URL`, `MIGRATARR_JELLYFIN_URL`

Other variables that pick files: `MIGRATARR_HOME` (data folder),
`MIGRATARR_CONFIG` (this file), `MIGRATARR_STORAGE_TARGETS`,
`MIGRATARR_PLANNER_CONFIG` and `MIGRATARR_SECRETS_DIR`. The `--base` option of
`migratarr ui`, `status` and `reconcile` overrides `base_path`.

## Keep settings steady while moves run

Settings are read once per process. Don't change disks or where they are
mounted between planning, approval and running a move. The UI refuses to save
settings while a job or move is running, and move jobs refuse to continue if
the effective settings changed since they started. Run snapshots record the
settings they used (without keys).

## Version 1 (older files)

Version 1 declared every disk twice, in `runtime.json` `storage` and in
storage-targets.json, and refused to run when they disagreed:

```json
{
  "schema_version": 1,
  "base_path": "/opt/media-stack/migratarr",
  "containers": {"radarr": "radarr", "sonarr": "sonarr", "homepage": "homepage"},
  "urls": {"radarr": "http://localhost:7878", "sonarr": "http://localhost:8989", "jellyfin": "http://localhost:8096"},
  "nas": {"host": "nas.example", "user": "migratarr", "ssh_key": "~/.ssh/migratarr_nas", "python": "/usr/bin/python3"},
  "storage": {
    "media01": {"local_path": "/mnt/nas/media01", "remote_path": "/volume1/media01"}
  }
}
```

Version 1 has no `topology` (it is always a NAS over SSH) and finds
storage-targets.json at `<base_path>/config/storage-targets.json`. Full
example: `config/examples/runtime.v1.example.json`.

Version 1 files keep working, and `migratarr doctor` warns about them. They are
migrated to version 2 automatically, once, when the UI starts, or on request
with `migratarr doctor --migrate`. Migration:

1. Requires both files to be valid and to agree, and refuses if
   storage-targets.json declares a disk `runtime.json` doesn't (it would
   become a move source and destination), naming the disk.
2. Removes `storage`, adds `topology: nas_ssh`, and adds `storage_targets`
   only when storage-targets.json isn't beside `runtime.json`.
3. Checks that the effective settings are identical before and after. If not,
   nothing is written.
4. Saves with a backup in `config/backups/<time>/`, and is refused while a job
   or move is running.

If migration can't run, version 1 stays in place and keeps working; the UI
says why when it starts. To undo, copy `config/backups/<time>/runtime.json`
back.

## Boundaries that stay fixed

- Disk roots must be canonical absolute paths, and no root may equal or
  contain another (on this machine, and on the NAS). Each title must be at
  disk root, category folder, title folder; see [Media layout](media-layout.md).
- Timeouts, retries, SSH options, lock files and reserve handling are fixed
  safety values, not settings.
- The incident recovery scripts keep their recorded paths on purpose: they
  refuse a remapped incident rather than recover different paths.
- The frozen placement modules (`movie_placement_v1`, `tv_placement_v1`) are
  historical references and don't read these settings.

## History

The settings in this file were moved out of the code in September 2026, each
with tests and real-data parity checks showing the deployed behavior didn't
change (`runtime_config`, `service_keys`, `arr_files`; the
`migratarr_validation` package holds the parity tools). Version 2 and the
settings layer followed in October 2026. Details are in the git history and
[Settings](settings.md).
