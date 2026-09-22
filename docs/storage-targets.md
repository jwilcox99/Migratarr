# Storage target migration

The standalone `storage_targets.py` model implements steps 0–1 of the supplied
StorageTarget Phase 2 proposal. It has no live callers. Do not replace production
runtime or legacy policy configuration yet. The example is a migration fixture,
not another active source of storage settings.

Load an explicit file with `load_targets(filename)` or validate an object with
`parse_targets(data)`. `config/storage-targets.example.json` reproduces the four
current target IDs, local and remote roots, category directories, fallback order,
and 50 GiB reserves. Tests compare its derived values to both existing configs.

Targets contain declared configuration only. `TargetState` holds observed
capacity and free space in bytes; a future planner probe must supply those values.
This module performs no storage probes and writes no snapshots or media files.

Disabled targets remain source roots but cannot appear in placement lists.
Placement references must be enabled and support the requested media type.
Missing `remote_path` means plan-only; no transport authorization is inferred from
this model. If any remote path exists, the entire configuration retains the
`phase1-fixed-depth` local shape and shared-parent checks, plus the two-component
remote shape. Target IDs remain independent of paths in configurations with no
remote paths. Local roots cannot overlap; remote roots must be distinct.

Reserve values are nonnegative integer GiB, with a default and optional per-target
override. Priority defaults to 100 and accepts integers (including negative
values); priority, storage class and tags have no placement effect. Optional
remote paths must be omitted rather than null. An empty media-type list is valid
for a source-only target, but cannot satisfy any placement reference.

Next gates:

1. Replace planner storage constants with this model and prove byte-identical
   plans using the frozen real-data parity harness.
2. Derive manifest target IDs and prove unchanged manifest bytes.
3. Migrate runtime storage settings, keeping compatibility shape validation.
4. Generalize executor disk sets and the media04 pin in a separate safety review
   with real approved-manifest check-only evidence.

Adding a fifth entry currently proves model/config support only. It does not
make the live planner or executors support a fifth target. Recovery incident
scripts and frozen placement rules are unchanged.
