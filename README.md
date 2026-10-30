<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/jwilcox99/Migratarr/main/assets/migratarr-lockup-dark.svg">
    <img alt="Migratarr" src="https://raw.githubusercontent.com/jwilcox99/Migratarr/main/assets/migratarr-lockup-light.svg" width="360">
  </picture>
</p>

# Migratarr

Migratarr decides *where* your media should live, and moves it there once you
approve. It works alongside Radarr and Sonarr (which manage what's in your library)
and Jellyfin (which plays it), and sorts movies and TV across your disks by how
hard each title would be to get again, whether it's on a streaming service you
pay for, and how your household actually watches it.

Every move is planned first. Nothing moves until you review the plan and
approve it.

> **Status: 0.1.0, the first public release.** Migratarr was built for one
> home media stack and has moved real libraries there over a NAS. Moving
> files between disks attached to the same machine is new in 0.1.0 and
> **experimental**. Linux only. Read [Safety model](#safety-model) before
> pointing it at a library you care about.

## Who it's for

You run Radarr and Sonarr (and Jellyfin) at home, your media is spread over
several disks, and you'd like the titles you'd struggle to replace on the
disks you trust most, without moving thousands of folders by hand.

## What it does

For each movie and series, Migratarr looks at:

- **Replacement difficulty:** how many releases Radarr or Sonarr can still
  find. An estimate, not a guarantee.
- **Streaming:** whether it's on a service you subscribe to, free with ads,
  rent/buy only, or not streaming in your region (from TMDB).
- **Usage:** Jellyfin plays, how recent, repeat plays and how many people in
  the household watched it, with a grace period for new additions.
- **Collections (movies):** a small bonus when related films are already
  protected.

From that it picks a placement category:

| Movies | TV |
| --- | --- |
| Common | Current |
| Library | Library |
| Rare | Rare |
| Archive | Archive |

You decide which disks hold each category and which folder each one uses. A
title that's hard to replace and isn't streaming anywhere stays in Rare even
if nobody has watched it lately; a widely available title nobody plays may
become an Archive candidate.

<p align="center">
  <img alt="A plan in the web UI: each move with its categories, disks, status, approval and the reasons behind it" src="https://raw.githubusercontent.com/jwilcox99/Migratarr/main/assets/screenshots/plan.png" width="800">
</p>
<p align="center"><sub>A plan from <code>migratarr demo</code>: nothing moves until each row is approved.</sub></p>

<p align="center">
  <img alt="Settings and health page: services, where moves run, disks and categories, streaming, scoring and override tags, each marked OK" src="https://raw.githubusercontent.com/jwilcox99/Migratarr/main/assets/screenshots/settings.png" width="800">
</p>
<p align="center"><sub>Settings &amp; health: set up in the browser; planning waits until every check passes.</sub></p>

## Requirements

- A Linux machine with Python 3.10 or newer that can see your media disks
  (Migratarr uses only the standard library), or Docker.
- Radarr and Sonarr (Sonarr v4 recommended), Jellyfin, and a
  [TMDB API Read Access Token](https://www.themoviedb.org/settings/api).
- Radarr's and Sonarr's root folders already pointing at the disks and
  category folders Migratarr will use.
- To move files, by default, permission to run `docker exec` in the Radarr and
  Sonarr containers: before and after every move Migratarr checks the files
  where Radarr and Sonarr see them. For Radarr and Sonarr installed without
  Docker, choose that during setup and Migratarr checks on the host instead;
  see [Arr file checks](https://github.com/jwilcox99/Migratarr/blob/main/docs/runtime-configuration.md#arr-file-checks).
- For moves on a NAS: SSH access to the NAS with a key used only by Migratarr,
  and Python 3 on the NAS.

## Install

```sh
pip install migratarr
```

Install it into a virtual environment, or with `pipx install migratarr`, if
your system Python doesn't allow `pip install`. Check it worked with
`migratarr --version`.

Or run it in Docker next to Radarr and Sonarr, with nothing to install on the host:

```sh
docker run -d --name migratarr -p 8765:8765 -e PUID=1000 -e PGID=1000 \
  -v ./migratarr:/config -v /srv/data:/data ghcr.io/jwilcox99/migratarr:latest
```

Mount your media at the same path as in your Radarr and Sonarr containers. See
[Docker](https://github.com/jwilcox99/Migratarr/blob/main/docs/docker.md) for a
compose example and setup.

## Try it first

```sh
migratarr demo
```

Plans a made-up library with the real planning pipeline and opens the web UI
read-only at `http://localhost:8767`, so you can see what a plan looks like
before connecting anything. It needs no Radarr, Sonarr, Jellyfin or TMDB,
uses no network, and never reads or writes your own settings. Everything
goes in a new temporary folder (`--dir` to choose one; delete it when done).

## First run

Run Migratarr as a user that can read and write your media folders, then:

```sh
migratarr ui
```

It prints the address, `http://localhost:8765`. By default the UI listens on
this machine only (127.0.0.1). If Migratarr runs on a server, either:

- **use it on your network** with a sign-in, like Radarr and Sonarr:

  ```sh
  migratarr ui --host 0.0.0.0
  ```

  Then browse to `http://<server>:8765`. The first visit asks you to create a
  login, so open it right away. Change it later with `migratarr password`.
  Don't expose the port to the internet.
- or keep it local and open an SSH tunnel from your own computer, then browse
  to `http://localhost:8765`:

  ```sh
  ssh -L 8765:localhost:8765 media-host.example
  ```

`MIGRATARR_HOST` and `MIGRATARR_PORT` set the same as `--host` and `--port`.

With no settings yet, every page leads to the setup wizard:

1. **Connect your services:** Radarr, Sonarr, Jellyfin and TMDB. Detect reads
   Radarr's and Sonarr's keys from their own config; or paste a key.
2. **Where your disks are:** on a NAS over SSH, or on this machine's own
   disks (experimental). Also the data folder.
3. **Disks and categories:** your disks, the folder for each category, and
   which disks hold each category.
4. **Streaming services:** your TMDB region and the services you pay for.
5. **Review and save:** nothing is written until you save.

After saving, the wizard runs the health checks. Fix anything that failed in
**Settings & health**, then make your first plan. [SETUP.md](https://github.com/jwilcox99/Migratarr/blob/main/SETUP.md) walks
through each step.

## Where moves run

| | How it works | Status |
| --- | --- | --- |
| **NAS over SSH** | Migratarr runs on a machine that mounts the NAS disks and sends each move to the NAS over SSH, so files are copied on the NAS rather than over the network | Used on a real library |
| **This machine's own disks** | Moves run directly on disks attached to the machine running Migratarr | **Experimental**: not yet run end to end against a real Radarr and Sonarr |

Both use the same plan, approval and safety checks.

## Using it

The web UI covers the whole cycle:

1. **Plan:** "Plan a new run". Planning is read-only: it scores your library,
   proposes moves and freezes the result as a run. A large library can take a
   while.
2. **Review and approve:** pick moves (or "Review all"), check sizes, free
   space afterwards and folder permissions, then approve them.
3. **Run:** "Run N approved moves", check the order, then "Start moving".
   Each move is checked again just before it runs. You can stop after the
   current move.

Jobs keep running if you close the browser or restart the UI.
`migratarr ui --read-only` shows everything with every action switched off.

## Safety model

- **Plan, review, approve, run.** Planning never touches media files. Only an
  approved move can run, and you start it.
- **No plan while a health check fails.** `migratarr plan` and the UI refuse
  to plan until every check passes (warnings are fine).
- **Plans are frozen.** Each run is a read-only, checksummed snapshot of the
  plan and the code that made it.
- **Approvals are tied to the exact plan.** If the plan changes after you
  approve, the move refuses to run.
- **Every move is checked again first:** the plan and approval, what Radarr or
  Sonarr say about that item right now, and a SHA-256 hash of every file.
- **Moves don't overwrite.** A move on one disk is a rename that never
  replaces an existing folder. A move between disks copies to a staging
  folder, verifies, publishes, updates Radarr or Sonarr, and deletes the
  source only after checking the copy.
- **Everything is journaled.** Each move writes a journal before and after
  every change. A move that fails part way is never retried automatically:
  you check it and record what you found with `migratarr reconcile`.
- **Settings changes are careful.** Saving makes a backup, and is refused
  while a job or move is running.

There is no undo. Start with your smallest, least important titles.

## Commands

| Command | What it does |
| --- | --- |
| `migratarr demo [--dir DIR] [--port N] [--no-ui]` | Try it on a made-up library: plans offline, UI read-only |
| `migratarr ui [--port N] [--read-only]` | The web UI: setup, settings, plan, approve, run |
| `migratarr plan [--check]` | Make a new plan; nothing moves. `--check` runs the checks only |
| `migratarr status [--run RUN \| --execution ID] [--json]` | Runs, moves and their journals (read-only) |
| `migratarr approve ...` | Approve moves from the terminal: `--list`, `--approve ID`, `--approve-batch --media TV --transfer CROSS_DISK_TRANSFER [--limit N] [--yes]`, `--revoke ID`, `--status` |
| `migratarr doctor [--offline] [--json] [--migrate]` | Check settings, services, disks and the NAS; shows the data folder |
| `migratarr reconcile ID --outcome ... --note ...` | Record that a move flagged for checking was checked by hand |

`migratarr <command> --help` lists each command's options. Moves are started
from the web UI.

## Where settings and data live

Everything goes in one **data folder**: `$MIGRATARR_HOME` if set, otherwise
`~/.local/share/migratarr`. (Running from a git clone, the clone itself.)
`migratarr doctor` prints which folder is in use. It holds `config/` (the
three settings files and their backups), runs, approvals and move journals.

Pasted API keys are kept apart, in `~/.config/migratarr/secrets/`, readable by
your user only; see [SECURITY.md](https://github.com/jwilcox99/Migratarr/blob/main/SECURITY.md).

## Limitations

- Linux only.
- Moves on this machine's own disks are experimental.
- One machine runs Migratarr, and it must see the disks. With a NAS, that's
  one NAS reached over SSH.
- Radarr and Sonarr must see the media under the same folder layout: one root,
  then a category folder, then the title.
- Radarr or Sonarr on a different machine, with no view of their media from
  the machine running Migratarr, isn't supported.
- Moves run from the web UI; there's no command that runs them.
- Streaming groups are written for the US; elsewhere, add your region's
  services ([Provider families](https://github.com/jwilcox99/Migratarr/blob/main/docs/planner-settings.md#provider-families)).
- 0.x: settings files and commands may still change between minor versions,
  always with a migration or a note in the [changelog](https://github.com/jwilcox99/Migratarr/blob/main/CHANGELOG.md).

## Documentation

- [SETUP.md](https://github.com/jwilcox99/Migratarr/blob/main/SETUP.md): the setup wizard step by step, and editing settings by hand
- [Docker](https://github.com/jwilcox99/Migratarr/blob/main/docs/docker.md): running Migratarr in a container
- [Settings](https://github.com/jwilcox99/Migratarr/blob/main/docs/settings.md): the settings files, version 2 and the health checks
- [Runtime configuration](https://github.com/jwilcox99/Migratarr/blob/main/docs/runtime-configuration.md): `runtime.json` reference
- [Storage targets](https://github.com/jwilcox99/Migratarr/blob/main/docs/storage-targets.md): disks, categories and placement
- [Planner settings](https://github.com/jwilcox99/Migratarr/blob/main/docs/planner-settings.md): streaming, override tags and scoring
- [Media layout](https://github.com/jwilcox99/Migratarr/blob/main/docs/media-layout.md): where Migratarr expects each title to be
- [UI actions](https://github.com/jwilcox99/Migratarr/blob/main/docs/ui-actions.md): what each button does and checks
- [Changelog](https://github.com/jwilcox99/Migratarr/blob/main/CHANGELOG.md)

## Contributing

See [CONTRIBUTING.md](https://github.com/jwilcox99/Migratarr/blob/main/CONTRIBUTING.md).

## Security

See [SECURITY.md](https://github.com/jwilcox99/Migratarr/blob/main/SECURITY.md). Report vulnerabilities privately through
GitHub, not in a public issue.

## License

[MIT](https://github.com/jwilcox99/Migratarr/blob/main/LICENSE).
