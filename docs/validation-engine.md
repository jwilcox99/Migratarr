# Standalone validation engine

This branch adds `migratarr_validation/` and its tests. The existing planner,
placement scripts, snapshot and approval chain, and executors are unchanged.
The new package is **not wired into a live run**. The engine reads paths through
caller-supplied functions and does not move files, call Arr APIs, or write CSVs.
The separate snapshot command reads Arr tags and writes only its requested JSON file.

## Source and provenance

The behavior was checked against `build_move_plan.py` on `main` at commit
`cf6ec2485ee97397b2932ab891e4d6d6eb67fb8c` (file blob
`1dfb5b263dd8858240a1257e0196534a901b4e09`). The tests load selected
functions from that exact repository file via the Python AST, avoiding its
module-level Docker calls and file writes.

| Behavior in this package | Origin in `build_move_plan.py` | Treatment |
| --- | --- | --- |
| Manual lock and category tag precedence | `apply_override`, lines 471–500 | Ported decision order and strings |
| Source candidate search and ambiguity marker | `resolve_host_source`, lines 96–137 | Ported with configured source roots |
| Same-disk preference and projected-free choice | `choose_destination_root`, lines 175–220 | Ported with configured destination roots |
| Blockers, warnings, status, output fields, capacity reservation | `evaluate_move`, lines 223–391 | Ported in the same order |
| `HOLD`, unchanged category, and unknown category skips | movie and TV loops, lines 512–576 | Ported to `evaluate_candidates` |
| Final cumulative destination blocker | final capacity loop, lines 647–728 | Ported to `apply_cumulative_capacity`; retains rounded `size_gb` accounting and appends to already blocked rows |
| `ValidationPolicy`, `MoveRequest`, injected filesystem reads | No direct counterpart | New interface for testing and future configuration |
| Missing destination root handling | Planner calls `dest_root.exists()` after `dest_root` can be `None` | New engine returns `NO_ELIGIBLE_DESTINATION;DESTINATION_ROOT_MISSING` instead of raising |

The package does not copy the original file wholesale. Its conditional logic
and output strings are ported; the interface and dependency injection are new.
The fixture tests compare complete result dictionaries and projected capacity
state with the original functions for representative cases.

## Use

Construct `ValidationPolicy` with the actual source and destination roots,
then supply read-only `exists`, `size_bytes`, and `free_bytes` functions to
`ValidationEngine`. Evaluate `MoveRequest` values in the original CSV order.
Call `apply_cumulative_capacity` after evaluating all candidates. This package
has no default NAS paths, so it cannot silently use a machine-specific layout.

Run the tests from the repository root with:

```text
python -m unittest discover -s tests -v
```

## Read-only parity harness

`migratarr_validation.parity` reads saved movie and TV dry-run CSVs, runs the
original planner's candidate loops and final capacity block **in memory**, and
compares each complete row against the standalone engine. It parses the
original file and selects only those code sections. It does not import or run
the module-level Docker calls or either `move_plan.csv` write. The report goes
to standard output; no report file is created automatically.

Run this on the Linux host with the same `/mnt/nas/media01` through `media04`
mounts that `build_move_plan.py` expects:

```text
python -m migratarr_validation.parity \
  --movie-csv /path/to/saved/movie_dry_run.csv \
  --tv-csv /path/to/saved/tv_dry_run.csv \
  --overrides-json /path/to/saved/overrides.json
```

The override snapshot uses Arr item IDs and the lowercase Migratarr tag names
used by `load_arr_overrides`:

```json
{"Movie": {"123": ["migratarr-lock"]}, "TV": {"456": ["migratarr-rare"]}}
```

To capture that snapshot on the server, run this first from the repository
root. It calls the original planner's read-only Arr tag loader and creates a
new JSON file; it refuses to overwrite an existing one:

```text
python -m migratarr_validation.parity \
  --snapshot-overrides /path/to/new/overrides.json
```

Use `{"Movie": {}, "TV": {}}` only if the saved run had no relevant override
tags. The comparison mode does not retrieve live Arr data. It reports input SHA-256 hashes,
row counts, and field-level differences. Exit status `0` means parity, `1`
means differences, and `2` means the comparison could not complete. Run against
stable NAS state: the original and new evaluations read the same filesystem
sequentially, so intervening changes to files or free space can create a
transient difference.

The repository does not contain saved dry-run CSVs or a tag snapshot. The
harness has been tested with controlled fixtures, but a real-run result still
requires those inputs and access to the matching NAS mounts.

On 2026-09-17, the operator ran the branch on the server and reported 134
legacy rows, 134 engine rows, and zero differences. The reported input hashes
were `8db5ac2bdff3d243510ddd3cad7347274089e9861d63be8a9c3a84e82553d15c`
for movies and `a87e3c4e15d2b856b2ba32ce17dd5e622c573937f60616cbd176c35bc0ee5e3d`
for TV. This records the observed baseline; the input files are not committed.

## Configurable storage policy

`config/legacy-storage.json` reconstructs the planner's current destination
roots, source disks, category directories, and 50 GiB reserve. A test compares
its derived roots and reserve directly with constants loaded from
`build_move_plan.py`. This JSON file is a reference configuration, not an
input to the existing planner.

The version 1 fields are:

| Field | Meaning |
| --- | --- |
| `source_roots` | Absolute POSIX disk mount paths; final directory names are unique disk IDs |
| `category_paths` | Relative directory under each disk for each media type and logical category |
| `destination_disks` | Ordered list of eligible disk IDs for each destination category |
| `min_free_after_gb` | Whole-number GiB reserve after a proposed move |

The loader rejects unknown fields or versions, duplicate or unknown disk IDs,
overlapping source roots, duplicate category paths, absolute or escaping
category paths, and malformed reserve values. It does not access the filesystem
when loading configuration. To compare the reference file against the original
planner with the same saved inputs, add `--config`:

```text
python -m migratarr_validation.parity \
  --movie-csv /path/to/saved/movie_dry_run.csv \
  --tv-csv /path/to/saved/tv_dry_run.csv \
  --overrides-json /path/to/saved/overrides.json \
  --config config/legacy-storage.json
```

The report includes the configuration SHA-256 hash. A modified configuration
can intentionally produce differences; inspect those rows before considering
any live integration. This phase configures storage layout and reserve only.
The `Rare` and `Archive` review semantics, Migratarr override tag names, and
execution approval rules are still fixed to the existing planner behavior.

The operator reran parity on the server with `config/legacy-storage.json` and
the same saved CSVs and override snapshot. The reported result was 134 legacy
rows, 134 configured-engine rows, zero differences, and exit status `0`. The
reported configuration SHA-256 was
`c032edad73ccde937c73a09fd6b13498049c8e5459d1b7f087badc24901bd68b`,
which matches the committed file. This establishes parity for that saved run;
it does not validate other storage layouts or enable live execution.

## Explicit review and override rules

`config/legacy-rules.json` represents the planner's existing manual override
tags and review warnings. The default rule policy is equivalent to the
original `apply_override` tag precedence and warning order. The package has a
separate rule loader so the current storage configuration remains valid.

| Rule behavior | Existing source | Configurable behavior |
| --- | --- | --- |
| Lock tag wins over category tags and resets recommendation to current | `apply_override` | Tag name can change; lock still wins and the candidate is skipped |
| A single category tag changes the recommendation | `apply_override` | Tag-to-category mapping can change |
| Multiple category tags conflict | `apply_override` + `evaluate_move` | `CONFLICTING_MANUAL_OVERRIDES` remains a mandatory blocker |
| Manual lock and category override flags | `evaluate_move` | `WARN` or `IGNORE` for lock; `WARN`, `BLOCK`, or `IGNORE` for category override |
| Low confidence, Rare promotion/demotion, Archive review | `evaluate_move` | `WARN`, `BLOCK`, or `IGNORE`; review category names can change |

`IGNORE` suppresses only the named flag. It does not cancel a manual lock,
change an override, bypass a filesystem or capacity blocker, or alter the
executor's separate approval checks. The parser rejects unknown rule IDs,
invalid actions or tags, and attempts to downgrade a conflicting manual
override. The hard blockers for missing sources, destination collisions, and
capacity remain in the engine and are not configurable in this phase.

Compare the committed default rules to the original planner using the same
saved inputs:

```text
python -m migratarr_validation.parity \
  --movie-csv /path/to/saved/movie_dry_run.csv \
  --tv-csv /path/to/saved/tv_dry_run.csv \
  --overrides-json /path/to/saved/overrides.json \
  --config config/legacy-storage.json \
  --rules-config config/legacy-rules.json
```

For custom override tag names, capture a new snapshot with
`--snapshot-overrides /path/to/new.json --rules-config /path/to/custom-rules.json`.
That mode uses the configured tag names in read-only Radarr/Sonarr requests.
The original planner will still use its fixed tag names, so parity differences
from custom tags are expected and must be reviewed as policy changes.

The operator reran the saved server inputs with both committed default policy
files and reported 134 legacy rows, 134 configured-engine rows, zero
differences, and exit status `0`. The reported rule-file SHA-256 was
`827e79c2a874980a693a09ae8653b2d68874152c7a2ab52eb65cdffdba8c0839`,
matching the committed file. The movie and TV input hashes matched the earlier
server run. This verifies the default rule extraction for that saved run.

## Policy impact report

`migratarr_validation.impact` compares two configurations on the **same saved
placement CSVs**. It evaluates the standalone engine once with baseline
storage/rules and once with candidate storage/rules, including the final
cumulative capacity pass. The report matches items by media type and input
CSV record number, so a removed plan cannot shift the identity of later rows.
It reports added and removed plans, every changed plan field, added/removed
blockers and warnings, destination changes, status changes, and SHA-256 hashes
for all inputs. It writes JSON only to standard output.

Create a candidate rules file outside the repository by copying
`config/legacy-rules.json` and editing one action. Then run:

```text
python -m migratarr_validation.impact \
  --movie-csv /path/to/saved/movie_dry_run.csv \
  --tv-csv /path/to/saved/tv_dry_run.csv \
  --overrides-json /path/to/saved/overrides.json \
  --baseline-storage config/legacy-storage.json \
  --baseline-rules config/legacy-rules.json \
  --candidate-rules /path/to/candidate-rules.json
```

`config/examples/archive-review-block.json` is an illustrative candidate that
changes only `ARCHIVE_MOVE_REVIEW` from `WARN` to `BLOCK`. Pass it as
`--candidate-rules` to see which saved plans would be affected. This example
does not change the live planner or recommend that policy for production.

Use `--candidate-storage /path/to/candidate-storage.json` for storage changes;
omitted candidate files inherit their baseline counterpart. If candidate rule
tags differ from baseline tags, capture a matching candidate override snapshot
with the parity command and pass `--candidate-overrides-json`. The impact
command refuses to guess which tags apply. Exit status `0` means no plan
changes, `1` means the report contains changes, and `2` means it could not
complete. Status `1` is expected for an intentional policy change.

The report caches filesystem existence, size, and free-space reads shared by
both evaluations. It does not freeze the NAS; changes during the run can still
affect the result. It does not call Arr, write `move_plan.csv`, create a
manifest, approve anything, or move files. A zero-change report applies only
to the saved inputs and sampled filesystem state, not future libraries.

## Known boundaries from repository evidence

- The original planner skips a move if an override changes its recommendation
  back to its current category. The engine preserves that in
  `evaluate_candidates`.
- The original planner assumes source and destination disk free-space samples
  exist when reserving a cross-disk move, and assumes every disk sample exists
  during its final cumulative check. The package keeps those assumptions;
  callers must supply complete samples before using those paths.
- The source resolver's ambiguity marker is intentionally blocked by
  `SOURCE_MISSING`, just as in the planner.
- This branch does not change execution eligibility or introduce a new
  manifest format. Wiring the package into the running workflow requires a
  separately verified migration.
