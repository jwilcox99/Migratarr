# Setup

Migratarr is not yet a "clone it and it adapts to you" tool. Deployment
location, NAS/SSH details, container names, service URLs, and disk layout are
config-driven (see [Configuration](README.md#configuration) and
[Runtime configuration](docs/runtime-configuration.md)). Everything in this
doc is what's *left over* after that config: assumptions still baked into
Python source rather than exposed as a setting, and the deployment-specific
quirks of the one real environment this has ever run against. Read this
before deciding whether to run it against a library you care about, and
expect to edit code, not just JSON, to adapt some of these.

## 1. What config already covers

Follow [Configuration](README.md#configuration) first:

```sh
cp config/runtime.example.json config/runtime.json
cp config/storage-targets.example.json config/storage-targets.json
cp config/planner.example.json config/planner.json
# edit all three for your deployment
python3 -c 'from runtime_config import get_config; get_config(); print("Runtime config valid")'
python3 -c 'from planner_settings import load_settings; load_settings(); print("Planner settings valid")'
```

That covers: base path, NAS host/user/SSH key, Docker container names,
Radarr/Sonarr/Jellyfin URLs, disk IDs and local+remote roots of any depth,
and category folder names (see [Media layout](docs/media-layout.md)). Keep
`runtime.json`'s `storage` block and `storage-targets.json`'s targets in
agreement; `storage_targets.check_runtime_consistency()` refuses a deployment
where they disagree.

`planner.json` holds your streaming subscriptions and TMDB watch-provider
region, and optionally your override tag names and scoring tuning (see
[Planner settings](docs/planner-settings.md)). The example's
values (Hulu, Peacock, `US`) are this deployment's, not defaults: change them,
or streaming scarcity will be scored against someone else's subscriptions.
Family names must match what the planners' `provider_family()`/`family()`
return (e.g. `"Max"`, `"Disney+"`, `"Prime Video"`); anything else is the raw
TMDB provider name.

You'll also need a [TMDB Read Access
Token](https://www.themoviedb.org/settings/api), by default exported as
`TMDB_TOKEN`. API keys and the TMDB token can instead come from other sources
set in `runtime.json` "secrets" (for example an owner-only file, so the token
survives new shells); see
[Service credentials](docs/runtime-configuration.md#service-credentials).

## 2. What still assumes this deployment's shape

Every value SETUP.md used to list as a Python literal (streaming
subscriptions and region, override tags, scoring, NAS layout and category
folders, where API keys come from) is now configuration. What remains is
topology rather than values:

- **One host, one NAS.** Planning and execution run on one host (here
  "the media host") that sees the NAS disks locally and reaches the NAS over SSH
  for cross-disk and same-disk moves.
- **Radarr and Sonarr in Docker.** Even with API keys from `env` or `file`
  sources, the executors run `docker exec <container> test` / `sha256sum` to
  verify media as Radarr/Sonarr see it, so both must be containers named in
  `runtime.json` `containers`.
- **Arr URL base.** `execute_cross_movie.py`, `execute_cross_tv.py` and
  `execute_tv_nas.py` honor a Radarr/Sonarr `UrlBase`; `execute_movie.py`,
  `execute_movie_nas.py` and the planners assume none. Serve Radarr/Sonarr at
  the root of their `urls` origin.

## 3. Recommended first run

Once the above is addressed for your environment, follow
[Usage](README.md#usage) — dry-run, plan, snapshot, manifest, approve — and
run every executor check-only (the default; no `--execute`) before trusting
any of it, on the smallest, least-important item you can find. Read
[Safety model](README.md#safety-model) in full first.

## 4. Getting help

See [CONTRIBUTING.md](CONTRIBUTING.md) for the branch/PR workflow and
[SECURITY.md](SECURITY.md) for anything you'd rather report privately.
