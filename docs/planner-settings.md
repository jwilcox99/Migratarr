# Planner settings

`config/planner.json` holds preferences the dry-run planners
(`dry_run_movies.py`, `dry_run_tv.py`) apply when scoring streaming scarcity.
It also names the Radarr/Sonarr tags that lock an item or pin its category,
which the planner (`build_move_plan.py`) and every executor read.
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
  },
  "overrides": {
    "lock_tag": "migratarr-lock",
    "category_tags": {
      "migratarr-common": "Common",
      "migratarr-current": "Current",
      "migratarr-library": "Library",
      "migratarr-rare": "Rare",
      "migratarr-archive": "Archive"
    }
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

## Override tags

`overrides` is optional; leaving it out means exactly the `migratarr-*` tags
shown above.

| Field | Meaning |
|---|---|
| `overrides.lock_tag` | An item carrying this tag is never moved: the planner keeps it in place (`LOCK`) and every executor refuses it at preflight. |
| `overrides.category_tags` | Tag → category. One matching tag forces that category (`CATEGORY`); two or more is a `CONFLICT`. Executors refuse a move whose tags name a category other than the manifest's recommendation. |

Tags must be lowercase with no spaces, commas or semicolons (Arr labels are
lowercased before comparison, so an uppercase tag could never match).
Categories must be one of `Common`, `Current`, `Library`, `Rare`, `Archive`:
those are the planners' fixed category IDs, not folder names. Each category
may have at most one tag, and the lock tag can't also be a category tag.

**Change tags between runs, not during one.** Executors check the tags
configured when *they* run against the manifest the planner wrote earlier.
If you rename a tag in Radarr/Sonarr, rename it here at the same time;
otherwise items carrying the renamed tag are neither locked nor pinned.

`config/legacy-rules.json` (the read-only validation layer's policy) carries
its own copy of these tags; keep it in step unless you are deliberately
previewing a different policy (see `docs/validation-engine.md`).

## Migration evidence

### Streaming (item #1)

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

Real-data result on the media host (2026-09-23, candidate `e4427e0`, example
settings copied to `config/planner.json`, production cache): every cached
response scored identically.

```json
{
  "baseline_sha256": "3eab027eaaab3dba3d549867f3e254d9c75bb40f5228314020a524f568c02f41",
  "byte_identical": true,
  "candidate_sha256": "3eab027eaaab3dba3d549867f3e254d9c75bb40f5228314020a524f568c02f41",
  "differences": [],
  "movie_responses": 167,
  "tv_responses": 209,
  "tv_series": 42
}
```

The full offline suite (153 tests) also passed on the media host.

### Override tags (item #2)

The lock tag and category tags were literals in `build_move_plan.py`
(`OVERRIDE_TAGS`, `LOCK_TAG`, and a `"migratarr-lock"` reason string) and in
the preflight of all seven executors (`execute_movie.py`,
`execute_movie_nas.py`, `execute_tv_nas.py`, `execute_cross_movie.py`,
`execute_cross_tv.py`, `recover_cross_0102.py`, `recover_cross_0116.py`),
which also derived the expected tag as `'migratarr-' + recommended.lower()`.

1. `tests/test_override_tags.py` pinned the executors' inline check verbatim
   and proved `OverrideTags.locked()`/`agrees()` with default tags equal it
   for all 3,584 combinations of tag sets and recommended values, before
   any executor changed.
2. The planner and executors were changed to read `overrides`. The same test
   file now also fails if any live script reintroduces a tag literal, and
   `tests/test_executor_safety.py` proves both cross-disk executors refuse
   on configured lock/category tags and ignore `migratarr-lock` once it is
   configured away.
3. Real-data planner parity uses the existing
   `migratarr_validation.planner_parity` (the pinned `c31afc6` baseline
   carries the literal tags) with a fresh live override snapshot.

Real-data result on the media host (2026-09-23, candidate `9868c18`, deployed
`runtime.json`, `storage-targets.json` and `planner.json` without an
`overrides` section, current dry-run CSVs). The live snapshot held one
`migratarr-lock` and one `migratarr-rare` movie; `--capture-facts` and
`--replay-facts` both reported:

```json
{
  "byte_identical": true,
  "baseline_rows": 16,
  "candidate_rows": 16,
  "baseline_csv_sha256": "883898df75d992b32579d1c8b9fe317e635df6ffe5ce5a0196899394d86a71d1",
  "candidate_csv_sha256": "883898df75d992b32579d1c8b9fe317e635df6ffe5ce5a0196899394d86a71d1",
  "facts_sha256": "55c3fcc420746cf666eb2f1b9ecafd8db3fa9e20cb66f9e313f866c598716379"
}
```

The full offline suite (162 tests) also passed on the media host.
