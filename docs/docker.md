# Running Migratarr in Docker

Migratarr runs as one container, like Radarr and Sonarr: the web UI on port 8765,
its settings and history in `/config`, and your media mounted at the same path as
in your Radarr and Sonarr containers. No Python or virtual environment on the host.

Image: `ghcr.io/jwilcox99/migratarr` for amd64 and arm64.

| Tag | What |
| --- | --- |
| `latest` | the newest release |
| `0.2`, `0.2.0` | a release line, or one exact release |
| `edge` | the newest code on `main`: for trying changes before a release |

## Quick start

A complete example with Radarr and Sonarr is in
[`examples/docker/compose.yaml`](https://github.com/jwilcox99/Migratarr/blob/main/examples/docker/compose.yaml).
The Migratarr part:

```yaml
services:
  migratarr:
    image: ghcr.io/jwilcox99/migratarr:latest
    container_name: migratarr
    environment:
      - PUID=1000
      - PGID=1000
      - TZ=America/Denver
    volumes:
      - ./migratarr:/config
      - /srv/data:/data
    ports:
      - 8765:8765
    restart: unless-stopped
    stop_grace_period: 30s
```

```sh
docker compose up -d
```

Then open `http://<your server>:8765` **right away**: the first visit creates the
login (username and password). After that, setup continues in the browser.

## The one rule: same media path everywhere

Migratarr moves folders and then tells Radarr or Sonarr the new path. That only
works when all three containers see your media at **the same path**:

```yaml
# migratarr, radarr and sonarr all have:
    volumes:
      - /srv/data:/data
```

and Radarr's and Sonarr's root folders are under it (for example
`/data/disk1/Movies/Common`). If Radarr sees `/movies` while Migratarr sees
`/data/disk1/Movies`, moves are refused by the checks, not half-done.

Each disk is a folder under that mount (`/data/disk1`, `/data/disk2`, …). For moves
between disks, they must be separate filesystems; the health checks say so if not.

## Setting up in the browser

In **Services**:
- Radarr `http://radarr:7878`, Sonarr `http://sonarr:8989` (the compose service
  names), Jellyfin likewise. Paste each API key (Radarr and Sonarr: Settings >
  General): Detect can't read another container's files.
- "Where Radarr and Sonarr run": choose **installed directly on this machine**.
  Migratarr then checks files through its own `/data` mount, which is the same
  path Radarr and Sonarr see. (The Docker option uses `docker exec`, which the
  image deliberately doesn't have.)

In **Where moves run**, choose "this machine's own disks" for disks mounted into the
container, or the NAS (below).

## Settings

| Variable | Default | |
| --- | --- | --- |
| `PUID`, `PGID` | `1000` | the user and group Migratarr runs as: the owner of your media (`id -u`, `id -g`) |
| `UMASK` | `002` | new files and folders group-writable, like Radarr's defaults |
| `TZ` | `Etc/UTC` | time zone for the times shown in the UI |
| `MIGRATARR_PORT` | `8765` | port inside the container |

Everything Migratarr keeps is in `/config`: settings (`/config/config/`), the login,
runs, approvals, execution journals and job logs. On start the container gives
`/config` to `PUID:PGID`; nothing outside it is changed.

## Commands

Run any `migratarr` command inside the container; it runs as `PUID:PGID`:

```sh
docker exec -it migratarr migratarr password    # set or change the login
docker exec migratarr migratarr doctor          # health checks
docker exec migratarr migratarr status          # runs and moves
```

## NAS over SSH

The image has an SSH client. Put a key used only by Migratarr and the NAS's
`known_hosts` entry in a folder, mount it read-only at `/config/.ssh`, and owned by
`PUID`:

```yaml
    volumes:
      - ./migratarr-ssh:/config/.ssh:ro
```

In **Where moves run**, set the SSH key to `/config/.ssh/<key file>`. Host keys are
checked strictly, so `known_hosts` must already list the NAS.

## Updating and backups

```sh
docker compose pull migratarr && docker compose up -d migratarr
```

Update when no move job is running (the UI shows one at the top of every page).
Stopping the container stops a running move; its journal records where it stopped,
and the UI shows it as needing attention, as after any interruption. Back up the
`./migratarr` folder (`/config`) to keep settings and history.

## Safety

- The login is required as soon as the container starts listening; set it on the
  first visit. Use it on your home network only: don't forward the port to the
  internet. For access from elsewhere, use a VPN or a reverse proxy with TLS.
- The image has no Docker CLI and needs no Docker socket.
