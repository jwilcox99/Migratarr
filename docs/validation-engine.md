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
