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
inside JSON strings are not interpolated. API keys retain their existing Docker
secret/config lookup and TMDB retains `TMDB_TOKEN`; never put credentials in this file.

## Extracted values and production mapping

| Setting | Existing deployment value |
| --- | --- |
| `base_path` | `/opt/media-stack/migratarr` |
| `nas.host`, `nas.user` | `nas.example`, `migratarr` |
| `nas.ssh_key`, `nas.python` | `~/.ssh/migratarr_nas`, `/usr/bin/python3` |
| `containers.radarr`, `.sonarr`, `.homepage` | `radarr`, `sonarr`, `homepage` |
| `urls.radarr`, `.sonarr`, `.jellyfin` | `http://localhost:7878`, `http://localhost:8989`, `http://localhost:8096` |
| `storage.media01` | `/mnt/nas/media01` → `/volume4/media01` |
| `storage.media02` | `/mnt/nas/media02` → `/volume1/media02` |
| `storage.media03` | `/mnt/nas/media03` → `/volume2/media03` |
| `storage.media04` | `/mnt/nas/media04` → `/volume3/media04` |

All of these settings are deployment-specific. URL settings are origins only,
without credentials or a path. Existing per-executor UrlBase behavior is
unchanged: cross-disk Movies and TV read it from Arr config; older same-disk
Movie executors continue using `/api/v3/` directly.

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
- Timeouts, retries, SSH keepalive/strict-host-key options, lock filenames,
  `/config/config.xml`, `/run/secrets/...`, reserve space and scoring constants
  remain fixed protocol/safety/policy values. In particular, changing an operation
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
