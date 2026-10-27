# Storage targets (`storage-targets.json`)

`storage-targets.json` declares your disks (storage targets), the folder each
placement category uses, and which disks may hold each category. With
settings version 2 it is the **only** place disks are declared: `runtime.json`
says how moves run, not which disks exist.

The setup wizard writes this file (step 3, *Disks and categories*), and
**Settings & health → Disks** edits it, including adding and removing disks.
This page is the reference for reading or editing it by hand.

**Location:** `$MIGRATARR_STORAGE_TARGETS` if set; otherwise the
`storage_targets` path in `runtime.json` (absolute, or relative to
`runtime.json`'s folder); otherwise `storage-targets.json` beside
`runtime.json`, which is `config/storage-targets.json` in the data folder.
An older version 1 `runtime.json` instead reads
`<base_path>/config/storage-targets.json`. There is no fallback to an example
file: a missing or invalid file stops Migratarr before it contacts any service
or writes a plan. `migratarr doctor --offline` checks it.

The file's own `schema_version` is `1`. (Version 2 is the `runtime.json`
format; this file's format didn't change.)

## Example

Two disks on a NAS (`topology: nas_ssh`). Full four-disk examples are in the
repository: `config/storage-targets.example.json` (NAS) and
`config/examples/storage-targets.local.example.json` (this machine's own
disks).

```json
{
  "schema_version": 1,
  "defaults": {"minimum_free_space_gb": 50},
  "arr_root": "/media",
  "targets": [
    {
      "id": "media01",
      "name": "NAS disk 1",
      "path": "/mnt/nas/media01",
      "remote_path": "/volume1/media01",
      "enabled": true,
      "media_types": ["Movie", "TV"]
    },
    {
      "id": "media02",
      "name": "NAS disk 2",
      "path": "/mnt/nas/media02",
      "remote_path": "/volume2/media02",
      "enabled": true,
      "media_types": ["Movie", "TV"],
      "minimum_free_space_gb": 100
    }
  ],
  "category_paths": {
    "Movie": {"Common": "Movies/Common", "Rare": "Movies/Rare", "Library": "Movies/Library", "Archive": "Movies/Archive"},
    "TV": {"Current": "TV/Current", "Rare": "TV/Rare", "Library": "TV/Library", "Archive": "TV/Archive"}
  },
  "placement": {
    "Movie": {"Common": ["media01"], "Rare": ["media02"], "Library": ["media01", "media02"], "Archive": ["media02"]},
    "TV": {"Current": ["media01"], "Rare": ["media02"], "Library": ["media01", "media02"], "Archive": ["media02"]}
  }
}
```

For this machine's own disks (`topology: local`, experimental), leave out
every `remote_path`.

## Fields

| Field | Required | Meaning |
| --- | --- | --- |
| `schema_version` | yes | `1` |
| `defaults.minimum_free_space_gb` | yes | Default reserve in GiB (whole number, 0 or more). Migratarr never plans a move that would leave a disk with less free space |
| `arr_root` | no (default `/media`) | Where Radarr and Sonarr see the disks: a title is at `<arr_root>/<category folder>/<title>` inside them |
| `targets` | yes | One entry per disk, below. At least one |
| `category_paths` | yes | `Movie` and `TV`: the folder for each category, relative to each disk root |
| `placement` | yes | `Movie` and `TV`: for each category, the disks that may hold it |

### Each target (disk)

| Field | Required | Meaning |
| --- | --- | --- |
| `id` | yes | Short id: lowercase letter, then up to 31 lowercase letters, digits, `-` or `_` (`media01`, `disk2`). Unique |
| `name` | yes | Display name |
| `path` | yes | The disk's root on the machine running Migratarr |
| `remote_path` | `nas_ssh`: required; `local`: not allowed | The same root as the NAS sees it |
| `enabled` | yes | `false` keeps the disk as a source (titles on it are still planned) but it can't be a destination |
| `media_types` | yes | `["Movie"]`, `["TV"]`, both, or `[]` for a source-only disk |
| `minimum_free_space_gb` | no | This disk's reserve; default from `defaults` |
| `priority`, `storage_class`, `tags` | no | Recorded only; they don't affect placement. `storage_class` is one of `HOT`, `PRIMARY`, `BULK`, `PROTECTED`, `ARCHIVE` or `null` |

Leave out optional fields rather than setting them to `null` (except
`storage_class`).

## Rules

- Roots are canonical absolute paths of any depth. They needn't share a parent
  or end in the disk id.
- No disk's `path` may equal or contain another's, and the same for
  `remote_path`: exactly one disk must match any title's path.
- Category ids are fixed (Movies: `Common`, `Rare`, `Library`, `Archive`; TV:
  `Current`, `Rare`, `Library`, `Archive`); the scoring depends on them. Their
  folders are yours to name. Each folder is relative, unique within its media
  type, and can't be `.`, `..` or `HOLD`.
- Every `placement` category must have a folder in `category_paths`, and list
  at least one disk that exists, is enabled and holds that media type.
- Radarr and Sonarr need each `<arr_root>/<category folder>` as a root folder.
  `migratarr doctor` checks this, and moves into a category folder they don't
  know are refused.
- With `topology: nas_ssh`, every disk needs `remote_path`; with `local`, none
  may have one.

Free space is measured when planning; this file only declares disks.

## Adding or removing a disk

In the UI: **Settings & health → Disks**, then **Add a disk**, or tick
**Remove this disk** on one. Removing a disk only stops Migratarr managing it;
nothing on the disk is touched.

By hand: add or remove the target, update `placement`, and check with
`migratarr doctor`. Don't change disks between planning, approving and running
a plan: a move checks that its disk still matches the plan and refuses if not.

## Older setups (`runtime.json` version 1)

Version 1 declared each disk twice: here, and in `runtime.json`'s `storage`
block (`local_path` / `remote_path`). Both had to agree exactly, and planning
and snapshots refused when they didn't. The automatic migration to version 2
removes the `storage` block and keeps the disks here; it refuses if this file
declares a disk `runtime.json` doesn't, rather than silently adding a move
destination. See
[Runtime configuration](runtime-configuration.md#version-1-older-files).

## History

Disks were first hard-coded (four NAS disks). They moved into this file in
September 2026 in four steps, each checked against real data: the planner
produced byte-identical plans, the manifest report listed every configured
disk, `runtime.json` and this file were checked for agreement, and the
executors were changed to work out the disk from each approved move instead
of assuming one. Cross-disk TV moves and the batch runners were then
validated on a real library. Settings version 2 (October 2026) made this file
the only place disks are declared. Details and evidence are in the git
history; the parity tools are in the `migratarr_validation` package.
