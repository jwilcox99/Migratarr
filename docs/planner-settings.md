# Planner settings

`config/planner.json` holds preferences the dry-run planners
(`dry_run_movies.py`, `dry_run_tv.py`) apply when scoring streaming scarcity.
It is separate from `runtime.json` on purpose: `runtime.json` describes the
host (paths, containers, NAS), this file describes the owner. It is required,
ignored by Git, and validated by `planner_settings.py` before either planner
contacts any service.

```json
{
  "schema_version": 1,
  "streaming": {
    "region": "US",
    "subscribed": ["Hulu", "Peacock"],
    "user_free_access": []
  }
}
```

| Field | Meaning |
|---|---|
| `streaming.region` | Two-letter uppercase TMDB watch-provider region (ISO 3166-1), e.g. `US`, `GB`, `CA`. Only this key of TMDB's `results` object is scored; a title with no entry for it scores 100 (`No <region> availability found`), and a TV season without it falls back to series-level data. |
| `streaming.subscribed` | Provider families you pay for. A title streaming on any of them scores 0. |
| `streaming.user_free_access` | Providers TMDB lists as conditionally `free` that you actually have (a library card service, a bundled perk). Leave empty unless confirmed. |

Family names must be what `provider_family()` (movies) / `family()` (TV)
return: `Paramount+`, `Prime Video`, `Apple TV`, `Disney+`, `Hulu`, `MGM+`,
`Peacock`, `Starz`, `Max`, or otherwise the raw TMDB `provider_name`. A name
that matches nothing is not an error; it simply never matches.

Set `MIGRATARR_PLANNER_CONFIG` to use a file elsewhere. Validation refuses
unknown or missing fields, duplicate JSON keys, duplicate names, lowercase or
three-letter regions and non-string entries.

## Migration evidence

These values were Python literals in both planners (`SUBSCRIBED`,
`USER_FREE_ACCESS`, and five `"US"` lookups) through `6dc21bd`. The change
followed the [parity-testing pattern](../CONTRIBUTING.md#the-parity-testing-pattern):

1. `tests/test_planner_streaming.py` pinned every scoring branch of both
   planners, and was committed and passing *before* the planners changed.
   The same cases also run against the pinned pre-change copies in
   `tests/fixtures/dry_run_*_before_planner_settings.py`.
2. The planners were changed to read `planner.json`.
3. `migratarr_validation.streaming_parity` replays every cached TMDB
   watch-provider response in a planner cache directory through both the
   pinned baseline and the candidate, and reports `byte_identical`:

```sh
git show 6dc21bd:dry_run_movies.py > /tmp/dry_run_movies.baseline.py
git show 6dc21bd:dry_run_tv.py > /tmp/dry_run_tv.baseline.py
python3 -m migratarr_validation.streaming_parity \
  --baseline-movies /tmp/dry_run_movies.baseline.py \
  --baseline-tv /tmp/dry_run_tv.baseline.py \
  --cache-dir /opt/media-stack/migratarr/cache \
  --planner-config config/planner.json
```

`migratarr_validation.planner_parity` does not cover this change: it compares
`build_move_plan.py`, which consumes the dry-run CSVs rather than producing
them.

Real-data result on the media host: *pending.*
