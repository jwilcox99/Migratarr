# Runtime configuration (Phase 2 checkpoint)

Runtime scripts require a version 1 JSON file. By default they load
`config/runtime.json` beside `runtime_config.py`, independent of the current
working directory. There is no automatic fallback to production settings.
The committed `config/runtime.example.json` represents the existing media host/NAS
layout; it contains connection metadata, not credentials. The deployment file
is ignored by Git. Python 3.10+ and the standard library are sufficient.

```sh
cd /opt/media-stack/migratarr
cp config/runtime.example.json config/runtime.json
# Review/edit every deployment-specific value before running any runtime script.
python3 -c 'from runtime_config import get_config; get_config(); print("Runtime config valid")'
```

The file requires every documented field. Unknown and duplicate fields, missing
NAS/Arr settings, invalid schema versions, noncanonical paths, ambiguous disk
roots, malformed URLs and unsafe SSH/container identifiers raise
`Runtime configuration: ...` errors before runtime scripts contact services or
write records. Validation does not prove that mounts, services, keys or files
exist. Keys expand `~` for the account running Python; environment variables
inside JSON strings are not interpolated. Credentials never go in this file: the
optional `secrets` section only says where each one is read from (see
[Service credentials](#service-credentials)).

## Extracted values and production mapping

| Setting | Existing deployment value |
| --- | --- |
| `base_path` | `/opt/media-stack/migratarr` |
| `nas.host`, `nas.user` | `nas.example`, `migratarr` |
| `nas.ssh_key`, `nas.python` | `~/.ssh/migratarr_nas`, `/usr/bin/python3` |
| `containers.radarr`, `.sonarr`, `.homepage` (optional) | `radarr`, `sonarr`, `homepage` |
| `urls.radarr`, `.sonarr`, `.jellyfin` | `http://localhost:7878`, `http://localhost:8989`, `http://localhost:8096` |
| `storage.media01` | `/mnt/nas/media01` → `/volume4/media01` |
| `storage.media02` | `/mnt/nas/media02` → `/volume1/media02` |
| `storage.media03` | `/mnt/nas/media03` → `/volume2/media03` |
| `storage.media04` | `/mnt/nas/media04` → `/volume3/media04` |

All of these settings are deployment-specific.

### Service URLs

`urls.radarr`, `.sonarr` and `.jellyfin` are HTTP(S) URLs with an optional
base path, for services served under a prefix (for example behind a reverse
proxy): `http://localhost:7878` or `https://proxy.example/radarr`. No
credentials, query, fragment or trailing slash. Every script builds its API
calls from one API root per service (`service_keys.service_endpoint`):

- A base path written in `urls` is used as-is.
- Otherwise Radarr and Sonarr append their `UrlBase`, read from `config.xml`
  (or the `url_base` of an `env`/`file` credential source), so a Radarr
  configured with URL base `/radarr` works with `urls.radarr` left at
  `http://localhost:7878`.
- Jellyfin uses its URL as given; put a Jellyfin base URL in `urls.jellyfin`.

Before this, only `execute_cross_movie.py`, `execute_cross_tv.py` and
`execute_tv_nas.py` applied `UrlBase`; the planners, `build_move_plan.py`,
`audit_overrides.py`, `execute_movie.py` and `execute_movie_nas.py` ignored
it, and `urls` refused any path. With no base path anywhere (this deployment),
every URL is unchanged.

Real-data result on the media host (2026-09-24, candidate `ab32449`):
`secrets_parity` reported every service's new API root identical to the URL
each old call site built (`endpoint_identical_to_urls` and, for Radarr/Sonarr,
`endpoint_identical_to_url_base_executors`), so no `UrlBase` is set there; the
item #3 recordings replayed byte-identically for movies and TV with no misses;
the offline suite passed.

## Service credentials

`service_keys.py` reads every API key and the TMDB token, for the planners,
`build_move_plan.py`, `audit_overrides.py` and all executors. Without a
`secrets` section each service uses the source it always has:

```json
"secrets": {
  "radarr":   {"source": "docker_config_xml"},
  "sonarr":   {"source": "docker_config_xml"},
  "jellyfin": {"source": "docker_file",
               "paths": ["/run/secrets/jellyfin_api_key", "/run/secrets/jellyfin_key"]},
  "tmdb":     {"source": "env", "variable": "TMDB_TOKEN"}
}
```

List only the services you want to change. Sources:

| `source` | Fields | Reads |
|---|---|---|
| `docker_config_xml` | `container`, `path` (both optional) | `ApiKey` and `UrlBase` from `docker exec <container> cat <path>`; container defaults to `containers.radarr`/`.sonarr`, path to `/config/config.xml`. Radarr/Sonarr only. |
| `config_xml` | `path` | The same from a host-side `config.xml`, for non-Docker installs. Radarr/Sonarr only. |
| `docker_file` | `paths`, `container` (optional) | The first nonempty file of `paths` in the container; defaults to `containers.homepage` for Jellyfin. |
| `file` | `path`, `url_base` (optional) | A file you provision. Refused unless it is owner-only (`chmod 600`) and outside the repository. |
| `env` | `variable`, `url_base` (optional) | An environment variable. |

`url_base` applies to Radarr/Sonarr sources that don't read `config.xml`.
Lookups run `docker exec ... cat` without a shell. A missing or empty
credential stops the script before it contacts any service, and error messages
name the source, never the value. `containers.homepage` is only required when
Jellyfin uses the default source.

For example, to keep the TMDB token in a file instead of the environment:

```sh
install -m 600 /dev/null ~/.config/migratarr/tmdb_token
$EDITOR ~/.config/migratarr/tmdb_token
```

```json
"secrets": {"tmdb": {"source": "file", "path": "~/.config/migratarr/tmdb_token"}}
```

### Migration evidence

Before `service_keys.py`, the planners, `build_move_plan.py` and
`audit_overrides.py` ran `docker exec <c> sh -c "sed ... /config/config.xml"`,
the executors ran `docker exec <c> cat /config/config.xml` and parsed the XML
(three of them also reading `UrlBase`), the movie planner tried two Jellyfin
secret paths and the TV planner one. `migratarr_validation/secrets_parity.py`
holds those lookups verbatim (cited at `260746d`).

- `tests/test_service_keys.py` runs old and new side by side against a fake
  `docker exec` (which also emulates the planners' `sed`): identical keys and
  `UrlBase` for standard, URL-base, single-line and CRLF `config.xml`, and
  identical Jellyfin keys. Two deliberate differences are pinned: a
  `config.xml` without `ApiKey` now stops the planners up front (they used to
  send an empty key), and the TV planner now falls back to
  `/run/secrets/jellyfin_key` as the movie planner always did. The same file
  covers every source type, owner-only and outside-repository file rules,
  schema validation, and that errors never contain a secret value.
- `dry_run_parity` replays substitute key lookups on both sides, so the item
  #3 recordings still compare the planners' scoring.
- Real data on the media host: `python3 -m migratarr_validation.secrets_parity`
  compares the real old and new lookups for every service and prints only
  booleans.

Real-data result on the media host (2026-09-24, candidate `82f4210`, no `secrets`
section, so every service on its default source): `secrets_parity` reported
the real old and new lookups identical for Radarr and Sonarr (planner key,
executor key and `UrlBase`), Jellyfin (both planners' lookups) and TMDB, with
every lookup succeeding (`"identical": true`). Replaying the item #3
recordings stayed byte-identical for movies and TV with no misses, and the
offline suite passed.

## Environment and command-line precedence

`MIGRATARR_CONFIG` selects another file (prefer an absolute path). The complete
file is validated first, then the following optional overrides are applied and
validated again. Empty overrides are errors.

- `MIGRATARR_BASE_PATH`
- `MIGRATARR_NAS_HOST`, `MIGRATARR_NAS_USER`, `MIGRATARR_NAS_SSH_KEY`
- `MIGRATARR_RADARR_CONTAINER`, `MIGRATARR_SONARR_CONTAINER`, `MIGRATARR_HOMEPAGE_CONTAINER`
- `MIGRATARR_RADARR_URL`, `MIGRATARR_SONARR_URL`, `MIGRATARR_JELLYFIN_URL`

Existing executor `--base` arguments still override the config's base directory.
The batch runner uses the shared base for approval and both execution stages,
pins the absolute config filename in child environments, and refuses changes to
effective settings between commands. Its approval/check/live sequence is unchanged.
Keep configuration and environment stable for the entire run; do not change disk
mappings between approval and execution. Configuration is cached within a process.

Snapshots now include the runtime module and effective non-secret config in
their existing SHA256SUMS. Old snapshots/manifests are not rewritten; approvals
remain bound to the same manifest bytes. Runtime settings are not a new approval
contract. Retain config snapshots when investigating old runs.

## Deliberately unchanged boundaries

- `config/legacy-storage.json` and `legacy-rules.json` remain independent
  planner/validation policy. No validation-engine cutover is included.
- Root depth, the shared local parent, category folder names and the
  Radarr/Sonarr-visible root are no longer Phase One contracts: see
  `docs/media-layout.md`. Roots must still be canonical absolute paths, and no
  local root (nor remote root) may equal or contain another. The *shape* an
  executor accepts (one disk root, one category folder, one item folder) is
  unchanged.
- Disk *count* is no longer a fixed Phase One contract (see
  `docs/storage-targets.md` gate 4): `runtime.json`'s `storage` block accepts
  any number of validly-shaped disk IDs, not only `media01`–`media04`, and the
  same-disk NAS executors (`execute_movie_nas.py`, `execute_tv_nas.py`) resolve
  the disk from each approved manifest row instead of assuming `media04`.
- Recovery IDs, titles, fingerprints, journal guards, and the 0102 Moneyball
  literal NAS source/destination paths are incident evidence. They intentionally
  stay fixed: that recovery refuses a remapped incident rather than interpreting
  it as permission to recover different paths.
- Frozen `movie_placement_v1.py` / `tv_placement_v1.py` and their checksum files
  remain byte-for-byte historical references. Use `dry_run_movies.py` and
  `dry_run_tv.py` for configured runtime operation.
- Timeouts, retries, SSH keepalive/strict-host-key options, lock filenames
  and reserve space remain fixed protocol/safety/policy values. In particular, changing an operation
  timeout can change uncertain-execution behavior; it is deliberately excluded.

Offline parity continues to characterize the example production layout, explicitly
loading the example config without deployment overrides. Live override capture
uses deployed runtime config. Capture's reviewed source hashes and narrowly scoped
output/cache rewrite support the new placement path expressions.

## Migration checklist and risks

1. Stop scheduling new runs and preserve the deployed code, config, manifests,
   approvals and journals. Reconcile any uncertain live execution first.
2. Install all changed Python files together, including `runtime_config.py` and
   the updated capture/parity adapters. Copy and review the example as above.
3. Run config validation under the actual service/operator account. Confirm the
   expanded SSH key, NAS identity, Docker names, HTTP origins and all four mount
   mappings match the server. Keep the current values for the initial rollout.
4. Run the safe tests, then normal status/check-only commands on the server.
   Inspect output and journals before explicitly authorizing any live execution.
5. Keep the reviewed config/environment unchanged throughout planning, approval
   and execution. Archive effective config alongside run records.

The intentional behavior change is refusal when runtime config is absent or
invalid. A syntactically valid but incorrect endpoint/mount mapping remains an
operator deployment risk; unit tests cannot validate physical mount identity.
No live NAS, Docker, Radarr/Sonarr calls or media moves were run for this change.
Execution ordering, hashes, collision checks, deletion safeguards, locking,
check-only defaults and recovery refusal guards remain in place.

```sh
python3 -m unittest discover -s tests -v
```

GitHub Actions runs only this safe unit suite on Ubuntu/Python 3.10. Tests use
temporary directories and mocks, without installed runtime config or credentials.
