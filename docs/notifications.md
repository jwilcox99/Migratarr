# Schedule and notifications

Settings & health > **Schedule & notifications**: plan automatically, and get a
message when a batch is ready, moves finish or something fails. Approving and
moving always stay with you, in the UI.

## Schedule

Pick the days and a time (local time of the machine running Migratarr; in Docker,
set `TZ`). At that time the UI starts the same planning job as "Plan a new run".
Planning never approves or moves anything. The scheduled plan is skipped, and you
get a message instead, when:

- **the last batch is still waiting for you:** nothing in it approved or moved yet,
  or approved moves not run yet. One reminder per scheduled time. When you've
  approved and run what you want, or want none of it, use **Mark run done** on the
  run page (or **Mark done** on the Runs page); the next scheduled time then plans.
  Reopen it on its page if you change your mind;
- **a health check fails** (Settings & health > Run health checks shows which).

A job that is running when the time comes is waited for. A time missed while the UI
was stopped is caught up once if it's less than 12 hours old; times before you
switched the schedule on (or changed it) never count.

The schedule runs inside `migratarr ui`, so the UI has to be running: in Docker it
always is; with pipx, run it as a service (for example a systemd user unit).

## Messages

| Turn on | Sent when |
| --- | --- |
| A batch is ready to review | a plan finishes with moves (a plan with nothing to move sends nothing), or a scheduled plan is skipped because the last batch is still waiting |
| Moves finished | a move job ends: how many moved, how many were skipped or held |
| Something failed | planning or a move job failed, or a scheduled plan was skipped because a health check fails |

A job stopped with "Stop" sends nothing: you asked for it. **UI address** is where
links in messages point (for example `http://192.168.1.20:8765`); it's prefilled
with the address you opened the page with. Blank: messages have no links.

## Channels

- **Discord:** the channel's webhook URL (channel settings > Integrations >
  Webhooks). Mentions are switched off, so messages never ping anyone.
- **Webhook:** a JSON POST to any URL, for ntfy, Home Assistant, Gotify and similar:

  ```json
  {"event": "batch_ready", "title": "Migratarr: 16 moves (577.6 GB) ready to review",
   "text": "…", "url": "http://192.168.1.20:8765/run/20261029T173659Z",
   "details": {"run_id": "20261029T173659Z", "moves": 16, "size_gb": 577.6},
   "sent_utc": "2026-10-30T09:00:12Z"}
  ```

  `event` is `batch_ready`, `run_finished`, `failed` or `test`.
- **Email:** SMTP with STARTTLS (587), SSL/TLS (465) or none (a local relay only),
  and an optional login (for Gmail and similar, an app password).

**Send a test** sends to every channel turned on in the saved settings and shows
what happened for each.

## Where it's kept

`config/notifications.json` in the data folder (owner-only: webhook URLs and the mail
password are secrets) and `config/schedule-state.json` (the last scheduled time
handled, and batches marked done). In a git clone both are git-ignored. Saved
secrets are never shown back in the page; leave the field blank to keep one, or tick
"Forget the saved one". Redirects are never followed, and error messages never
include a URL or password.
