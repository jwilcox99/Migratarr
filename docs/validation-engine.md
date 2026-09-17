# Standalone validation engine

This branch adds `migratarr_validation/` and its tests. The existing planner,
placement scripts, snapshot and approval chain, and executors are unchanged.
The new package is **not wired into a live run**. It reads paths through
caller-supplied functions and does not move files, call Arr APIs, or write CSVs.

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
