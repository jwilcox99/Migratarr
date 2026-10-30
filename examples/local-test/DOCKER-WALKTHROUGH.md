# Try Migratarr in Docker with the test kit

A walkthrough for testing the Migratarr Docker image before using it on a real library.
It sets up a throwaway Radarr, Sonarr and Jellyfin with a few tiny made-up titles, runs
Migratarr in Docker next to them, and does two real moves: one on the same disk and one
between two disks. Nothing outside one test folder is touched, and the last step removes
all of it.

**Time:** about an hour. **You need:**

- A Linux machine you can use `sudo` on: Ubuntu 24.04 or newer, or Debian 12 or newer.
  A virtual machine is ideal. 5 GB of free disk space.
- A web browser that can reach that machine.
- A free TMDB account, for its **API Read Access Token**: sign up at themoviedb.org, then
  Settings > API > request an API key (personal use). The token is the long one
  starting with `eyJ`, not the shorter "API Key".
- The image tag to test, from whoever sent you this (for example `0.2.0rc1`).

If something doesn't match what a step says to expect, stop there and send the output.

## 1. Install Docker and the tools

```bash
sudo apt update
sudo apt install -y git python3 e2fsprogs docker.io docker-compose-v2
sudo usermod -aG docker "$USER"
```

Then **log out of the machine completely and log back in** (or reboot). Docker only works
without `sudo` in a new login. Check:

```bash
id -nG | tr ' ' '\n' | grep -x docker          # expect: docker
docker run --rm hello-world | head -2          # expect: Hello from Docker!  (no sudo)
docker compose version                         # expect: Docker Compose version v2…
```

If you already had Docker from **snap** (`snap list docker` shows it), remove it first
(`sudo snap remove --purge docker`) and install as above: the snap version can't see
everything the kit uses.

## 2. Get the kit

Every new terminal needs the `export` line again.

```bash
export MTEST_ROOT="$HOME/migratarr-test" MTEST_PUID=$(id -u) MTEST_PGID=$(id -g) TZ=$(timedatectl show -p Timezone --value 2>/dev/null || echo Etc/UTC)
export IMAGE=ghcr.io/jwilcox99/migratarr:0.2.0rc1      # the tag you were given
git clone https://github.com/jwilcox99/Migratarr "$MTEST_ROOT/src"
cd "$MTEST_ROOT/src/examples/local-test"
```

## 3. Test disks, Radarr, Sonarr and Jellyfin

```bash
./kit.py init
```

It asks for your password once: it makes the second test disk (a 2 GB file that only uses
space as it fills) and mounts it. Expect the last lines to say
`disk1 and disk2 are separate filesystems`.

```bash
./kit.py up       # starts mtest-radarr, mtest-sonarr, mtest-jellyfin; waits until they answer
./kit.py seed     # adds 6 tiny titles; expect every one "imported"
```

## 4. The keys you'll paste in setup

```bash
echo "Radarr:   $(grep -o '<ApiKey>[^<]*' "$MTEST_ROOT/config/radarr/config.xml" | cut -d'>' -f2)"
echo "Sonarr:   $(grep -o '<ApiKey>[^<]*' "$MTEST_ROOT/config/sonarr/config.xml" | cut -d'>' -f2)"
echo "Jellyfin: $(cat "$MTEST_ROOT/jellyfin-api-key.txt")"
```

Keep this terminal open. You'll also need your TMDB token.

## 5. Start Migratarr

Migratarr has to see the media at the **same paths** as Radarr and Sonarr. In the kit,
those are the category folders under `/media`. It also needs the two disks themselves, so
they're mounted at the same paths they have on this machine.

```bash
R="$MTEST_ROOT"
mkdir -p "$R/migratarr-docker"
docker run -d --name mtest-migratarr --network mtest_default \
  -e PUID=$MTEST_PUID -e PGID=$MTEST_PGID -e TZ=$TZ \
  -v "$R/migratarr-docker:/config" \
  -v "$R/disk1:$R/disk1" -v "$R/disk2:$R/disk2" \
  -v "$R/disk1/Movies/Common:/media/Movies/Common" -v "$R/disk1/Movies/Rare:/media/Movies/Rare" \
  -v "$R/disk2/Movies/Library:/media/Movies/Library" -v "$R/disk2/Movies/Archive:/media/Movies/Archive" \
  -v "$R/disk2/TV/Current:/media/TV/Current" -v "$R/disk2/TV/Rare:/media/TV/Rare" \
  -v "$R/disk1/TV/Library:/media/TV/Library" -v "$R/disk1/TV/Archive:/media/TV/Archive" \
  -p 127.0.0.1:8768:8765 "$IMAGE"
```

Wait about 30 seconds, then check:

```bash
docker ps --filter name=mtest-migratarr --format '{{.Status}}'      # expect: Up … (healthy)
docker exec mtest-migratarr migratarr --version                      # expect: the version you were given
docker exec mtest-migratarr ls /media/Movies/Common                  # expect: The General, Night of the Living Dead, Nosferatu
```

## 6. Open it and set it up

Open **http://localhost:8768** in a browser on this machine. From another computer, first
run `ssh -L 8768:localhost:8768 <you>@<this machine>` there and open the same address.

1. **Create your login**: any username and a password of 8 or more characters. You'll
   use them from now on.
2. **Connect your services.** Change the three addresses to the kit's container names, and
   paste each key from step 4 (each "API key" dropdown should say "Paste the key here"):

   | Service | URL | Key |
   | --- | --- | --- |
   | Radarr | `http://mtest-radarr:7878` | Radarr key |
   | Sonarr | `http://mtest-sonarr:8989` | Sonarr key |
   | Jellyfin | `http://mtest-jellyfin:8096` | Jellyfin key |
   | TMDB | (none) | your TMDB API Read Access Token |

   "How Radarr and Sonarr run" stays at "Same media path as Migratarr". Press **Next**.
3. **Where your disks are:** choose **On this machine's own disks**. Leave the data folder
   as `/config`. Next.
4. **Disks and categories** (don't press Detect on this step):
   - Default reserve: `1`. Media root as Radarr and Sonarr see it: `/media`.
   - Disk media01: name `Test disk 1`, mounted at the full path printed by
     `echo $MTEST_ROOT/disk1`, holds Movies and TV.
   - Add a disk: id `media02`, name `Test disk 2`, mounted at `echo $MTEST_ROOT/disk2`,
     then Next. The page reloads with media02; tick Movies and TV for it.
   - Which disk holds each category:

     | | media01 | media02 |
     | --- | --- | --- |
     | Movies | Common, Rare | Library, Archive |
     | TV | Library, Archive | Current, Rare |

5. **Streaming:** your region (for example US); the lists can stay empty. Next.
6. **Review:** the three URLs say `mtest-…`, the disk paths start with your
   `$MTEST_ROOT`, and TMDB shows a new key. **Save**.

## 7. Health checks

**Settings & health** > **Run health checks**. Expect everything OK; the NAS group is
skipped. If something fails, the line says why; see Troubleshooting below.

## 8. Plan

Go to **Runs** > **Plan a new run** and wait for it to finish (a minute or two). Then
**Review the new run**. Expect **4 moves** to review, and **The General** and **The Andy
Griffith Show** kept in place (they're locked on purpose). Nothing has moved yet.

## 9. Move 1: on the same disk

In the run, tick only **Night of the Living Dead** > **Review selected** > **Approve and
start moving**. The job page shows the output, newest line first. When it says
**Finished**:

```bash
./kit.py status
```

Expect Night of the Living Dead `ok`, at `disk1/Movies/Rare/…`, and the last line `none`.

## 10. Move 2: between disks

Same steps for **Nosferatu**. Then `./kit.py status`: Nosferatu `ok`, at
`disk2/Movies/Library/…` only, last line `none`.

## 11. Who owns the moved files

```bash
id -un
stat -c '%U:%G %a %n' "$R/disk2/Movies/Library/Nosferatu (1922)" "$R/disk2/Movies/Library/Nosferatu (1922)"/*
stat -c '%U:%G %a %n' "$R/disk1/Movies/Rare/Night of the Living Dead (1968)" "$R/disk1/Movies/Rare/Night of the Living Dead (1968)"/*
find "$R/migratarr-docker" -user root | head      # expect: nothing
```

Expect your own user and group (the name `id -un` printed) on every line.

## 12. Optional: a notification

**Settings & health** > **Schedule & notifications**: tick **Send to Discord**, paste a
webhook URL from a test channel (channel settings > Integrations > Webhooks), **Save**, then
**Send a test**. Then move one TV show (The Twilight Zone) and look for "1 move done" in
Discord.

## 13. Send back

```bash
{ ./kit.py status; echo; id -un; id -u; id -g
  stat -c '%U:%G %u:%g %a %n' "$R/disk2/Movies/Library/Nosferatu (1922)"/* "$R/disk1/Movies/Rare/Night of the Living Dead (1968)"/*
  echo; find "$R/migratarr-docker" -user root | head
  echo; docker logs --tail 80 mtest-migratarr; } > ~/migratarr-docker-test.txt 2>&1
```

Send `~/migratarr-docker-test.txt` and a few words on anything that was confusing, even if
it worked in the end. That matters as much as what broke.

## 14. Remove everything

```bash
docker rm -f mtest-migratarr
chmod -R u+w "$MTEST_ROOT/migratarr-docker"     # its run snapshots are read-only on purpose
./kit.py down --delete                          # containers, the test disk and $MTEST_ROOT
```

## Troubleshooting

| What you see | What to do |
| --- | --- |
| `kit: Docker is not installed` / `needs sudo` | Step 1; after `usermod`, log out completely and back in |
| `./kit.py up`: a port is in use | Something already uses 17878, 18989 or 18096: stop it, or use another machine |
| `docker run`: `network mtest_default not found` | `./kit.py up` didn't finish; run it again |
| The container isn't `(healthy)` | `docker logs mtest-migratarr`, and send that |
| Health check: Radarr/Sonarr "cannot reach" | The URL must be `http://mtest-radarr:7878` (the container name, not localhost) |
| Health check: two disks "on the same filesystem" | `mountpoint "$MTEST_ROOT/disk2"`; if it isn't one, `./kit.py init` again |
| Health check: Arr file checks fail | The `-v …:/media/…` lines in step 5 are missing or mistyped; `docker rm -f mtest-migratarr` and run step 5 again |
| After a reboot | `./kit.py init` (remounts disk2), `./kit.py up`, `docker start mtest-migratarr` |
