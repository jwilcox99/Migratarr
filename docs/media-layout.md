# Media layout

Where every executor and planner expects a media item to live, and how each
side of a move names it. Implemented once in `media_layout.py`.

| View | Path | Configured by |
|---|---|---|
| Local (the media host) | `<local_path>/<category dir>/<item>` | `runtime.json` `storage.<disk>.local_path` |
| Remote (NAS) | `<remote_path>/<category dir>/<item>` | `runtime.json` `storage.<disk>.remote_path` |
| Radarr/Sonarr | `<arr_root>/<category dir>/<item>` | `storage-targets.json` `arr_root` (default `/media`) |

`<category dir>` comes from `storage-targets.json` `category_paths`, for
example `"Movie": {"Common": "Movies/Common"}`. Category *IDs* (`Common`,
`Current`, `Library`, `Rare`, `Archive`) are fixed code identities the
scoring branches on; the folder each one lives in is configuration.

This deployment is `/mnt/nas/media01/Movies/Common/<item>` locally,
`/volume4/media01/Movies/Common/<item>` on the NAS and
`/media/Movies/Common/<item>` in Radarr. A layout such as
`/srv/pool/fast/films/everyday/<item>` on the host, `/mnt/fast/films/everyday/<item>`
on the NAS and `/library/films/everyday/<item>` in Radarr is equally valid.

## The safety contract

Configuration moves the roots and renames the folders; it does not loosen what
an executor accepts. `split_media_path()` accepts a path only if it is exactly
one configured root, plus one configured category folder, plus one item
folder, with no `..`. Executors additionally require manifest paths to be
canonical and to agree with the row's disk and category columns. The NAS-side
programs receive a copy of `split_media_path()` and the NAS roots and category
folders (`NAS_LAYOUT`) and run the same check before touching anything.

Config validation requires canonical absolute roots, and that no local root
(and no remote root) equals or contains another, so exactly one root can match
any path. `runtime.json` and `storage-targets.json` must still agree on every
disk's roots (`check_runtime_consistency`), and executors now check that too,
since they read category folders from `storage-targets.json`.

The planners' `current_bucket()` now reads an item's category from the same
layout: a Radarr/Sonarr path that isn't `<arr_root>/<category dir>/<item>` is
`Unknown` (previously a case-insensitive search for `/rare/`, `/library/`,
etc. anywhere in the path, which could name a category for a path no executor
would accept).

The recovery scripts' literal incident paths stay fixed on purpose
(`docs/runtime-configuration.md`).

## Migration evidence

Baselines: the executors and planners at `e11681b`, pinned in
`tests/fixtures/*_before_media_layout.py`.

1. `tests/test_media_layout.py` runs the pinned executors' own `paths()` and
   `remote_path()` and the new ones over every same-disk and cross-disk row
   for the example layout plus 17 malformed variants of each path (wrong
   depth, trailing slash, doubled slash, relative, other mount, unknown disk,
   wrong media folder, lowercase or unknown category, `..`, `.`) and
   mismatched disk/category/name columns: identical accept/refuse, refusal
   message and derived local, remote and Arr paths. The NAS-side checks
   (transcribed verbatim from the pinned programs) and the new `nas_pair()`
   agree on generated remote path pairs, and the planners' `current_bucket()`
   agrees on every well-formed Arr path.
2. The same file drives the real executors, the shipped NAS program and the
   planner through a non-Synology layout (uneven root depths, renamed
   category folders, `/library` Arr root) and proves old Synology-shaped rows
   are refused under it. Mutating the cross-disk pair check or the path
   splitter makes these tests fail.
3. Real data on the media host:
   `python3 -m migratarr_validation.layout_parity` replays every row of every
   manifest under `<base>/manifests/` through old and new checks, and
   `migratarr_validation.dry_run_parity compare` replays the item #3
   recordings through the pinned and current planners.

Real-data result on the media host: *pending.*
