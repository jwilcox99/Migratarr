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
# edit both for your deployment
python3 -c 'from runtime_config import get_config; get_config(); print("Runtime config valid")'
```

That covers: base path, NAS host/user/SSH key, Docker container names,
Radarr/Sonarr/Jellyfin URLs, and disk IDs/local+remote paths (disk *count* is
no longer fixed to four — see `docs/storage-targets.md` gate 4). Keep
`runtime.json`'s `storage` block and `storage-targets.json`'s targets in
agreement; `storage_targets.check_runtime_consistency()` refuses a deployment
where they disagree.

You'll also need a [TMDB Read Access
Token](https://www.themoviedb.org/settings/api) exported as `TMDB_TOKEN`.

## 2. What isn't config yet — you have to edit source

None of the following are read from `runtime.json` or any other config file.
They're Python literals, and the planner will silently apply *this*
deployment's values to yours unless you change them.

### Streaming subscriptions and region

[`dry_run_movies.py:35`](dry_run_movies.py:35) (and the equivalent in
`dry_run_tv.py`):

```python
# Known subscriptions for this test.
# Add/remove these later through Migratarr's user profile.
SUBSCRIBED = {
    "Hulu",
    "Peacock",
}
```

That comment is the project's own acknowledgment that this was meant to
become a real setting and never did. Edit the set directly for your
subscriptions in both files.

Separately, the TMDB watch-provider lookup
([`dry_run_movies.py:290`](dry_run_movies.py:290)) reads only the `"US"`
region from the response:

```python
us = data.get("results", {}).get("US", {})
```

Outside the US, this silently returns no streaming data for every title
(read as "streams nowhere," not an error) rather than failing loudly — the
one place in this codebase that guesses instead of refusing. Change the key
to your TMDB region code, or scoring for streaming availability will be
wrong for every item.

### Logical category names and override tags

The four Movie categories (`Common`, `Library`, `Rare`, `Archive`), the four
TV categories (`Current`, `Library`, `Rare`, `Archive`), and their
`migratarr-common` / `migratarr-lock` / etc. manual-override tag names are
string literals repeated across `dry_run_movies.py`, `dry_run_tv.py`, and
`build_move_plan.py` — not sourced from any config file.

An equivalent schema already exists — just not wired into the live planner.
`config/legacy-rules.json` (read by the read-only `migratarr_validation/`
package, see `docs/validation-engine.md`) expresses exactly this as JSON:

```json
{
  "lock_tag": "migratarr-lock",
  "category_override_tags": {
    "migratarr-common": "Common",
    "migratarr-rare": "Rare"
  }
}
```

If you want different tag names or category labels, you're changing the
live planner's string literals directly (and keeping them consistent across
all three files), not editing that JSON — it's a preview of a shape the live
path doesn't use yet, not a working input to it.

### Scoring weights and thresholds

Point values for replacement-difficulty tiers, subscription-family
counts, and similar tuning are inline numeric literals inside the scoring
functions in `dry_run_movies.py` / `dry_run_tv.py`, not named constants or
config fields. If you disagree with how aggressively this protects
hard-to-replace titles versus watched-often titles, that's a code change to
the scoring function itself, reasoned about and tested the way
[CONTRIBUTING.md](CONTRIBUTING.md#the-parity-testing-pattern) describes for
planner logic changes.

### NAS mount-layout shape

`runtime_config.py` accepts any disk *IDs* and *paths* you give it, but the
path *shape* is still fixed (`docs/runtime-configuration.md`, "Deliberately
unchanged boundaries"): local roots need a shared parent in the form
`/component/component/<id>`, and remote roots need exactly two path
components. That's this deployment's Synology-style
`/mnt/nas/media0N` → `/volumeN/media0N` pattern. A NAS with a different
mount depth or a non-Synology remote-path convention needs a reviewed code
change to `runtime_config.py`'s path validation, not a config edit.

### Docker-exec secret extraction

`docker_output()` in `dry_run_movies.py` / `dry_run_tv.py` assumes Radarr and
Sonarr run in Docker containers with a `sh` shell and API keys at
`/config/config.xml` inside them. Jellyfin's key is read from a *different*
container's secrets file — `docker exec homepage cat
/run/secrets/jellyfin_api_key` — not Jellyfin's own container (see
[SECURITY.md](SECURITY.md)). If your Radarr/Sonarr/Jellyfin aren't
Dockerized, don't expose `/config/config.xml` the same way, or don't run a
`homepage` dashboard container holding the Jellyfin secret, this code needs
to change, not just the container *names* in `runtime.json`.

## 3. Recommended first run

Once the above is addressed for your environment, follow
[Usage](README.md#usage) — dry-run, plan, snapshot, manifest, approve — and
run every executor check-only (the default; no `--execute`) before trusting
any of it, on the smallest, least-important item you can find. Read
[Safety model](README.md#safety-model) in full first.

## 4. Getting help

See [CONTRIBUTING.md](CONTRIBUTING.md) for the branch/PR workflow and
[SECURITY.md](SECURITY.md) for anything you'd rather report privately.
