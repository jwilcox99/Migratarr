# Local-disk test kit

A throwaway Radarr, Sonarr and Jellyfin with two folder "disks", for running Migratarr's
**local-disk moves** ("On this machine's own disks", experimental in 0.1.0) end to end,
without touching your real library, your real Radarr/Sonarr/Jellyfin or your real
Migratarr settings. When you're done, one command removes all of it.

You'll do two real moves, one of each kind Migratarr makes:

| Move | Title | From | To | Kind |
|---|---|---|---|---|
| 1 | Night of the Living Dead (1968) | Movies/Common on disk1 | Movies/Rare on disk1 | same-disk rename |
| 2 | Nosferatu (1922) | Movies/Common on disk1 | Movies/Library on disk2 | cross-disk transfer (copy, verify, delete) |
| 3, 4 (optional) | The Twilight Zone, Bonanza | TV/Current on disk2 | TV/Rare on disk2, TV/Library on disk1 | rename, transfer |

The titles are 20-second dummy videos (about 0.5 MB each). Radarr tags force the plan:
`migratarr-rare` and `migratarr-library` pin a title to that category, and The General
and The Andy Griffith Show carry `migratarr-lock` so they must never move
([override tags](../../docs/planner-settings.md)).

## What it creates, and what keeps it separate

Everything is under one folder you choose, `$MTEST_ROOT` (e.g. `~/migratarr-test`):

| Under `$MTEST_ROOT` | What |
|---|---|
| `src/`, `venv/` | a separate copy of Migratarr, installed into its own Python environment |
| `disk1/`, `disk2/` | the two test disks: `Movies/<category>` and `TV/<category>` folders |
| `config/` | the test containers' settings |
| `migratarr/` | the test Migratarr's data folder (settings, runs, logs) |
| `secrets/` | keys you paste into the test wizard |
| `seed/`, `jellyfin-*.txt`, `seeded.json` | the sample video, the test Jellyfin login and API key, what was seeded |

- Containers `mtest-radarr`, `mtest-sonarr`, `mtest-jellyfin` (compose project `mtest`) on
  ports 17878, 18989, 18096, bound to 127.0.0.1 only. Your real ones use 7878, 8989, 8096.
- Inside the test Radarr and Sonarr, each category folder is mounted at
  `/media/<category folder>`, the "media root as Radarr and Sonarr see it". That's why a
  movie category can live on either test disk ([compose.yaml](compose.yaml)).
- `activate.sh` points `migratarr` in your shell at the test copy, data folder and key
  folder, and clears any `MIGRATARR_*` or `TMDB_TOKEN` setting.
- `./kit.py check` refuses unless the test Migratarr's settings name only test ports, test
  containers and folders under `$MTEST_ROOT`. Run it before every Migratarr command that
  contacts anything.
- The test Migratarr UI uses port 8766 (your real one uses 8765).

## You need

- Linux with Docker, usable without `sudo` (`docker ps` works).
- `git`, and Python 3.10 or newer with `venv` (`python3 -m venv --help` works; on Ubuntu,
  `sudo apt install python3-venv` if not). Python 3.14 hasn't been tested with Migratarr
  yet, so note anything odd.
- Internet access, to pull the three images, install Migratarr's build tool from PyPI, let
  Radarr/Sonarr look up the test titles, and reach TMDB.
- Your real **TMDB API Read Access Token** (<https://www.themoviedb.org/settings/api>). It
  is read-only. Pasted into the test wizard, it's stored only in `$MTEST_ROOT/secrets`.
- About 1 GB free disk space, mostly for the images.
- The user and group IDs your real Radarr runs as (`docker inspect radarr --format
  '{{range .Config.Env}}{{println .}}{{end}}' | grep -E 'PUID|PGID'`). Your user must be in
  that group (`id -nG`).

Use `tmux` or `screen` on the media host: step 5 keeps a terminal busy with the UI.

## 1. Choose the folder and get the code

```bash
export MTEST_ROOT="$HOME/migratarr-test"   # a new folder; must not exist yet, or be empty
export MTEST_PUID=1000 MTEST_PGID=100      # your real Radarr's PUID and PGID

mkdir -p "$MTEST_ROOT"
git clone --branch examples/local-test-kit https://github.com/jwilcox99/migratarr "$MTEST_ROOT/src"
cd "$MTEST_ROOT/src/examples/local-test"
```

(Once the kit is merged, use `--branch main`.) Every new shell needs those three `export`s
again, then `cd "$MTEST_ROOT/src/examples/local-test"`.

## 2. Install the test Migratarr

```bash
python3 -m venv "$MTEST_ROOT/venv"
"$MTEST_ROOT/venv/bin/pip" install "$MTEST_ROOT/src"
```

This is an ordinary install into its own environment. Your real clone isn't touched.

## 3. Create the test disks and start the containers

```bash
./kit.py init     # folder tree, group-writable (mode 2775, group $MTEST_PGID)
./kit.py up       # checks compose.yaml against the kit, starts the containers, waits until they answer
./kit.py seed     # root folders, tags, the dummy titles, a Jellyfin admin and API key
```

`seed` should finish with every title marked `imported`. If one says `NOT imported`, open
that app's System > Logs (step 5 shows how to reach it) and include what it says in what
you send back.

## 4. Point your shell at the test Migratarr

```bash
. ./activate.sh
./kit.py check
```

`activate.sh` prints the data folder and the `migratarr` command it now uses. Both must be
under `$MTEST_ROOT`. `check` says Migratarr isn't set up yet; that's expected.

## 5. Run the setup wizard

Start the UI in this shell (inside tmux):

```bash
migratarr ui --port 8766
```

The UI only listens on the media host itself. From your PC, open a tunnel that also
reaches the test Radarr, Sonarr and Jellyfin:

```bash
ssh -L 8766:localhost:8766 -L 17878:localhost:17878 -L 18989:localhost:18989 -L 18096:localhost:18096 <you>@<media-host>
```

Then open <http://localhost:8766> on your PC. In a second shell on the media host (the
three `export`s, `cd`, `. ./activate.sh`), print the values to type:

```bash
./kit.py wizard-values
```

Two things matter most:

- **Services step: type the test URLs and container names before you press Detect.**
  The defaults are your real ones (`radarr`, `localhost:7878`, …), and Detect only fills
  blank fields. After Detect, it should say it found config.xml in `mtest-radarr` and
  `mtest-sonarr`. Paste the Jellyfin key from `$MTEST_ROOT/jellyfin-api-key.txt` and your
  TMDB token.
- **Disks step: don't press Detect.** It suggests the real disks mounted on the host.
  Type the two test disk paths, set the reserve to 1 GB, and set which disk holds each
  category exactly as `wizard-values` lists it.

On the Review page, every URL should say `localhost:17878`, `18989` or `18096`, and every
path should start with your `$MTEST_ROOT`. Then Save.

Right after saving, in the second shell:

```bash
./kit.py check
```

It must print `OK`. If it prints `NOT ISOLATED`, stop the UI (Ctrl-C) and fix what it
names on the Settings page. If you're unsure, delete `$MTEST_ROOT/migratarr/config` and
`$MTEST_ROOT/secrets` and run the wizard again.

## 6. Get every health check passing

```bash
migratarr doctor
```

Every line should be `ok`, apart from NAS, which shows `skip`. If something fails:

| Fails | Usually |
|---|---|
| Radarr/Sonarr: cannot reach | the container isn't up: `docker ps --filter name=mtest-`; `./kit.py up` |
| Radarr/Sonarr root folders: missing | run `./kit.py seed` again (it adds them), or the wizard's category folders aren't the defaults |
| Jellyfin: rejected the key | paste the key from `jellyfin-api-key.txt` again (Settings > Services) |
| TMDB: rejected the key | it needs the long **API Read Access Token**, not the short API key |
| Disk: below its reserve | the reserve isn't 1 GB (Settings > Disks) |
| Permissions: lack group write | `chmod -R g+w "$MTEST_ROOT"/disk1 "$MTEST_ROOT"/disk2` |

## 7. Make a plan

In the UI: **Make a first plan** (after setup) or **Plan a new run** (Runs page), or
`migratarr plan` in the shell. Nothing moves. Planning reads the test Radarr, Sonarr and
Jellyfin, and asks TMDB about streaming.

The new run should list four moves ready for review (the table at the top) and the two
locked titles kept in place. If it doesn't, don't approve anything. Send back what it shows.

## 8. Move 1: the same-disk rename

1. In the run, tick **only** Night of the Living Dead → **Review selected** →
   **Approve 1 move**.
2. **Run 1 approved move** → **Start moving**. Wait for the job page to say it finished.
3. Check:

   ```bash
   ./kit.py status
   ```

   Night of the Living Dead should be `ok`, with Radarr's path
   `/media/Movies/Rare/Night of the Living Dead (1968)`, found once, at
   `disk1/Movies/Rare/…`. The last line should say `none`: nothing left behind in any
   category folder. In the test Radarr (<http://localhost:17878> through the tunnel), the
   movie's path should be the new one, with its file present.

## 9. Move 2: the cross-disk transfer

Same steps for **Nosferatu**. Afterwards, `./kit.py status` should show it at
`disk2/Movies/Library/…` only, with Radarr's path `/media/Movies/Library/Nosferatu (1922)`,
and nothing left in `disk1/Movies/Common`.

Optionally, do the same for **The Twilight Zone** (rename) and **Bonanza** (transfer); the
check then uses Sonarr's path. The General and The Andy Griffith Show must never be
offered for approval.

## 10. What to send back

```bash
./kit.py report
```

This writes `~/mtest-report-<time>.txt`, outside the test folder so it survives teardown.
It holds the code version, container versions, `kit.py check`, `migratarr doctor`,
`kit.py status` and the latest run's status, with your home folder shown as `~`. Send that
file, plus:

- whether each move finished, and anything the job page or UI showed that surprised you
- anything odd: errors, warnings, slow steps, a wizard step that was confusing
- if a move stopped: `migratarr status --execution <its ID>` (the ID is on the run page)

## 11. Teardown

Stop the UI (Ctrl-C), then:

```bash
./kit.py down --delete
```

This removes the three containers and their network, then deletes `$MTEST_ROOT`, including
the test Migratarr, its settings and the pasted keys. The images stay cached; your real
stack uses the same ones. Close the shells you activated, or run `exec bash` in them.

## Troubleshooting

- **`./kit.py up` says a port is in use:** something else already listens on 17878, 18989
  or 18096. Find it with `ss -ltnp | grep -E '17878|18989|18096'`.
- **`pip install` fails fetching `setuptools`:** the host can't reach PyPI. Install it
  where it can, or ask in the project chat.
- **Starting over:** `./kit.py down --delete`, then go back to step 1.
- **Containers stopped (reboot):** `./kit.py up` again. They don't restart by themselves.
