# Setup

The setup wizard in the web UI is the normal way to set up Migratarr. This
page walks through it, then covers editing the settings files by hand.

## Before you start

Have these ready:

- Radarr, Sonarr and Jellyfin running, and their addresses.
- A [TMDB API Read Access Token](https://www.themoviedb.org/settings/api).
- Your disks mounted on the machine that will run Migratarr, with Radarr's
  and Sonarr's root folders pointing at their category folders (see
  [Media layout](docs/media-layout.md)).
- For a NAS: its host name, an SSH user, a private key used only by
  Migratarr, and the path to Python 3 on the NAS. Connect once by hand
  (`ssh -i <key> -l migratarr nas.example`) so the NAS is in your
  `known_hosts`: Migratarr refuses unknown hosts and never asks for a
  password.

Install with `pipx install migratarr`, or run the Docker image (see the [README](README.md#install)).
To see what Migratarr does before connecting anything, run `migratarr demo`:
it plans a made-up library offline and opens the UI read-only.

## Run the wizard

```sh
migratarr ui
```

Open `http://localhost:8765`. If Migratarr runs on a server, start it with
`migratarr ui --host 0.0.0.0` and open `http://<server>:8765`, creating the
login on the first visit, or use an SSH tunnel
(`ssh -L 8765:localhost:8765 media-host.example`). With no settings
yet, every page leads to the wizard. Nothing is written until the last step.

### 1. Connect your services

First say how Radarr and Sonarr run:

- **Radarr and Sonarr run in Docker containers** (the default). Migratarr
  reads their keys and, before and after every move, checks files inside
  their containers with `docker exec`. Name the containers below.
- **Radarr and Sonarr are installed directly on this machine.** Migratarr
  reads their keys from config.xml on this machine and checks files here.
  The container fields are hidden. For Radarr or Sonarr in a container with
  its media mounted at a different path, see
  [Arr file checks](docs/runtime-configuration.md#arr-file-checks).

Then, for each of Radarr, Sonarr, Jellyfin and TMDB, give the address (TMDB
has none) and where Migratarr reads the key from:

- **Read automatically from the Docker container's config.xml** (Radarr and
  Sonarr in Docker; the default). Name the container.
- **Read from config.xml on this machine**, for Radarr or Sonarr installed
  directly. Give the file's path, for example `/var/lib/radarr/config.xml`.
- **Read a file inside a Docker container**, for example a secret mounted
  into a dashboard container.
- **Read from a file on this machine.** The file must be readable by your user
  only (`chmod 600`).
- **Read from an environment variable.** TMDB's default is `TMDB_TOKEN`. The
  variable must be set wherever `migratarr ui` runs; planning jobs inherit it.
- **Paste the key here.** Saved to an owner-only file when you save, never
  shown again. See [SECURITY.md](SECURITY.md).

**Detect** reads Radarr's and Sonarr's keys and URL bases from their own
config when it can: inside the named containers, or, when they are installed
directly, from the path you gave or the usual places
(`/var/lib/radarr/config.xml`, `~/.config/Radarr/config.xml`, and the same
for Sonarr). If Docker isn't installed or running, Detect says so. If Radarr or Sonarr is served under a path (for example
behind a reverse proxy), put that path in the address or in **URL base**.

### 2. Where your disks are

- **On a NAS, over SSH:** Migratarr runs on a machine that mounts the NAS
  disks, and runs each move on the NAS. Enter the NAS host, the SSH user, the
  full path to the SSH key and the path to Python on the NAS.
- **On this machine's own disks (experimental):** moves run directly on disks
  attached to this machine. This has not yet been run end to end against a
  real Radarr and Sonarr.

**Migratarr data folder** is where runs, approvals and move journals are kept.
The default is the data folder (see [Where settings live](#where-settings-live)).

You choose NAS or this machine once: switching later means entering every
disk again, so run setup again (or edit the files by hand). The NAS details
and the data folder can be changed in **Settings & health**.

### 3. Disks and categories

**Detect** lists mounted disks and the root folders Radarr and Sonarr use, and
suggests which folder holds each category.

- **Default free-space reserve (GB):** Migratarr never fills a disk past
  this. Each disk can set its own.
- **Media root as Radarr and Sonarr see it:** inside Radarr and Sonarr, each
  title is at `<this>/<category folder>/<title>`, for example `/media`.
- **Each disk:** a short id (lowercase, such as `media01`), a name, where it is
  mounted on this machine, and, for a NAS, the same disk as the NAS sees it
  (for example `/volume1/media01`). Choose whether it holds movies, TV or
  both, and whether Migratarr may move media onto it. Add more disks with
  **Add a disk**. Removing a disk only stops Migratarr managing it; nothing on
  the disk is touched.
- **Category folders:** the folder, relative to each disk, for each category,
  for example `Movies/Rare`.
- **Which disks hold each category:** tick one or more disks per category.

### 4. Streaming services

- **Region:** two letters, such as `US`, `GB` or `CA`.
- **Services you pay for:** one per line, such as `Hulu`. Titles streaming on
  these count as easy to watch.
- **Free to you:** services TMDB lists as free that you really have (a library
  card service, say). Leave empty unless you're sure.

Outside the US, add your region's services to the provider groups by hand:
[Provider families](docs/planner-settings.md#provider-families).

### 5. Review and save

The review lists everything you set and runs the checks that don't need a
saved key. Pasted keys show only as "new key". **Save settings** writes the
three settings files and any pasted keys.

## Health checks

After saving, the wizard runs the health checks. The same checks run from
**Settings & health** in the UI and from the terminal:

```sh
migratarr doctor             # every check
migratarr doctor --offline   # settings files only; contacts nothing
```

| Check | Fails when | Warns when |
| --- | --- | --- |
| Settings files | a file has an error | `runtime.json` is still version 1 |
| Services | a key can't be read, a service can't be reached, or it rejects the key | Sonarr isn't v4 |
| Radarr/Sonarr root folders | a category folder isn't a Radarr or Sonarr root folder | |
| Disks | a disk path is missing; on this machine's own disks, not writable | NAS: not a mount point; free space below the reserve |
| NAS | SSH fails, Python on the NAS is older than 3.6, or a disk is missing or not writable on the NAS | |
| Permissions | | a category folder is missing, or it or a title folder in it lacks group write |

You can save settings with failing checks, but Migratarr won't make a plan
until none fail. Warnings are fine. `migratarr doctor` exits with status 1
when a check failed.

## Your first plan

In the UI, choose **Plan a new run**, or from the terminal:

```sh
migratarr plan --check   # the checks only
migratarr plan           # make a plan; nothing moves
```

Review the plan, approve one small move, run it, and check the result in
Radarr or Sonarr before approving more. Read the
[Safety model](README.md#safety-model) first.

## Changing settings later

**Settings & health** in the UI edits services, the NAS details, disks,
streaming, scoring and override tags. Each change shows exactly what will change in the file before
you save. Saving makes a backup in `config/backups/` and is refused while a
job or move is running.

## Where settings live

Everything is in the data folder: `$MIGRATARR_HOME` if set, otherwise
`~/.local/share/migratarr`. A git clone uses the clone itself. Check with:

```sh
migratarr doctor --offline
```

| File | Holds | Reference |
| --- | --- | --- |
| `config/runtime.json` | data folder, where moves run, NAS, containers, service addresses, where keys come from | [Runtime configuration](docs/runtime-configuration.md) |
| `config/storage-targets.json` | disks, category folders, placement, reserves | [Storage targets](docs/storage-targets.md) |
| `config/planner.json` | streaming, override tags, scoring | [Planner settings](docs/planner-settings.md) |

Pasted keys are not in the data folder: they are in
`~/.config/migratarr/secrets/` ([SECURITY.md](SECURITY.md)).

## Advanced: editing the files by hand

You can skip the wizard and write the three files yourself. Start from the
examples in the repository's `config/` folder:

- NAS over SSH: `runtime.example.json`, `storage-targets.example.json`
- This machine's own disks: `examples/runtime.local.example.json`,
  `examples/storage-targets.local.example.json`
- Both: `planner.example.json`. Its streaming services (Hulu, Peacock, `US`)
  are an example; replace them with yours.

Copy them into `config/` in the data folder without `.example` in the names,
replace every value with your own, then check them:

```sh
migratarr doctor --offline
```

Some settings exist only in the files: credential sources' extra options,
[Arr file checks](docs/runtime-configuration.md#arr-file-checks) other than
"all Docker" or "all on this machine", and environment overrides.
The [settings layer](docs/settings.md) explains how the three files fit
together.

### Already running from a clone

A clone keeps its settings in its own `config/` folder and works as before.
A version 1 `runtime.json` (with its own `storage` block) is migrated to
version 2 automatically, with a backup, the first time the UI starts, or on
request:

```sh
migratarr doctor --migrate
```

## What Migratarr still assumes

- One machine runs planning and moves, and sees every disk.
- With a NAS: one NAS, reached over SSH.
- Radarr and Sonarr see your media from the machine running Migratarr: by
  default inside their Docker containers, or on the host with
  [Arr file checks](docs/runtime-configuration.md#arr-file-checks). Radarr or
  Sonarr on another machine, with no view of the media, isn't supported.

## Getting help

See [CONTRIBUTING.md](CONTRIBUTING.md) to report a bug or ask for an
integration, and [SECURITY.md](SECURITY.md) for anything to report privately.
