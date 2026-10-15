# 🐦 pigeon

> *This project was created with AI assistance — built by a human with the
> [pi agent](https://github.com/earendil-works/pi).*

A dead-simple webhook server that connects [Nextcloud Talk](https://nextcloud-talk.readthedocs.io/en/latest/bots/) bots to the [pi coding agent](https://github.com/earendil-works/pi).

```
Nextcloud Talk bot ──POST /webhook/<bot>──> pigeon ──spawns──> pi (one folder per bot + room)
        ^                                                          │
        └──── posts thinking + answer back to the chat <───────────┘
```

## How it works (KISS)

* `POST /webhook/<bot>` receives Talk bot messages (e.g. `/webhook/bot1`).
  Nextcloud (>= 34) signs every webhook with
  `X-Nextcloud-Talk-Random`/`X-Nextcloud-Talk-Signature`
  (HMAC-SHA256 over `random + raw body`, key = bot secret); pigeon
  verifies it. The W3C-style payload already contains the room name and
  room token, so no extra API calls are needed.
  Each message is put in a per-room queue; **one worker thread per room**
  runs pi, so sessions are never interleaved.
* Every Talk room gets its own folder under `bots/<bot>/rooms/`, named
  after the room (sanitised). pi runs in the room's `workspace/`
  subfolder **and always in the room's stable pi session**
  (`pi --session-id <bot>_<room folder>` — created on first use), so the bot
  keeps the full conversation context of each room, across messages and
  restarts. Every pi run is wrapped in a bubblewrap sandbox (read-only
  root, only the room's `workspace/` and the pi home writable, full
  network access — no fallback: a broken sandbox fails the run,
  `SANDBOX=0` disables it).
* pi is invoked as `pi --mode json --session-id <bot>_<room> "<message>"`. The
  JSONL event stream is
  parsed; the assistant's **thinking** (quoted with 🤔) and the **answer**
  are posted back into the conversation via the official Talk bot API
  (`POST /ocs/v2.php/apps/spreed/api/v1/bot/<roomToken>/message`,
  `OCS-APIRequest: true`, signed with the same secret — see the
  [Talk bots docs](https://nextcloud-talk.readthedocs.io/en/latest/bots/)).
* **File attachments** (per-room, isolated): Talk sends files as a
  `{file}` placeholder; pigeon downloads by **file id** instead via
  WebDAV SEARCH (basicsearch on `oc:fileid`, Basic auth with the room's
  app password) which resolves the dav path, then a plain authenticated
  GET saves the file into the room's `workspace/chat/files/` folder, and the
  message that reaches pi gets the local path in front of the user's
  text: `chat/files/starwars-bb-8.webp What is on the pics?` (the
  `{file}` placeholder, if present, is removed). Talk sends **each
  attachment as its own event** (the user's text usually travels with
  one of them), so multiple files arrive as several messages that queue
  up in the same room session. Each room stores its own Nextcloud
  **app password** (user + password in the room's `.room.json`, never
  shared with other rooms):
  * in a room, send `/pw <nextcloud-user> <app-password>` to configure
    it (intercepted by pigeon, never reaches pi); `/pw` shows the status
    (the password is never echoed back)
  * while a room has no app password, a file message does not download
    anything — pigeon instead instructs the bot to tell the user **in
    the user's language** that the password for file access is missing
    and how to set it
  * app passwords: create one per room-user in Nextcloud under
    *Personal Information → Security* (do not use real account passwords)
  * the `/pw` message passes through the room's Talk history — delete it
    afterwards, or edit `.room.json` on the host for a no-trace setup
* **Message batching**: a room worker waits up to `MESSAGE_BATCH_WINDOW`
  seconds after a chat message for more messages from the same room and
  sends the whole burst to pi in a single run (as one prompt with the
  senders, so pi can answer at once); 0 disables the wait. Scheduled
  prompts skip the window so they stay on time.
* A **heartbeat thread** runs every `HEARTBEAT_INTERVAL` seconds and checks
  all started tasks:
  * pi process still alive past `PI_TIMEOUT` → killed; the worker reruns
    the task in the same room session (up to `MAX_RETRIES`),
  * task stuck without a process past the timeout → marked failed.
  * every `STATE_SCAN_EVERY`th tick the heartbeat also **scans the state
    files** and re-queues jobs that are on disk but no longer tracked in
    memory (crash self-healing) — so missed tasks are picked up without
    a restart.
* **Restart-safe queueing**: every queued message and the running task are
  persisted in `<room dir>/.pigeon.json`. On container (re)start — and
  periodically by the state scan — pigeon re-queues them, so no message
  is lost in a restart or crash; and since every room always runs in its
  stable pi session, context survives too.
* **Skills & scheduled tasks**: pi skills bundled in the image are copied
  into `$PI_CODING_AGENT_DIR/skills` (the `pi-home` volume) at every start
  — bundled ones are updated, your own are kept. The `scheduler` skill
  lets you tell the bot things like *"send me news for austria every day
  at 09:00"* or *"every hour between 10:00 and 22:00"*; it maintains a
  `schedules.json` per room that pigeon's scheduler thread checks every
  `SCHEDULER_INTERVAL` seconds and fires as normal room messages
  (daily `HH:MM` + IANA timezone, or every N minutes — optionally
  anchored at a `start_time` and/or confined to a local time `window`).
* **Fast shutdown**: on SIGTERM (`docker stop`) pigeon kills active pi
  runs and exits immediately — the container does not linger until the
  grace-period SIGKILL.
* `GET /health` returns server status, **pi configuration status**,
  heartbeat info, counters and all tasks (status, age, process alive) —
  200 when healthy, 503 degraded.

## Quick start (Docker)

```bash
cp .env.example .env      # fill in the values
# optional: stamp the git commit hash into the image (build info shown
# on /, /health and in the start log)
echo "GIT_COMMIT=$(git rev-parse --short HEAD)" >> .env
docker compose up -d --build
docker compose logs -f pigeon
```

Then register the bot on your **Nextcloud server** (>= 34; admin, as the
`www-data` user). Bots can only be added via the command line — the URL
path (`assibot`) becomes the bot's folder name, the **secret** must match
`TALK_BOT_SECRET` in `.env` (40-128 chars):

```bash
./occ talk:bot:install assi <secret> https://ai.lan:8080/webhook/assibot "pi agent"
```

This gives the bot the `webhook` + `response` features. Then add it to a
conversation (bot id from `./occ talk:bot:list`, token from the
conversation URL):

```bash
./occ talk:bot:setup <bot-id> <conversation-token>
```

Before the first message works, pi must be configured (see below).

## Configure pi

pi is configured **only with its default config files** (no model/API-key
environment variables in pigeon). They live in the `pi-home` named volume
(`/pi-home/agent`, i.e. `PI_CODING_AGENT_DIR`):

```bash
docker compose exec pigeon pi          # interactive pi
# inside pi: /login (provider credentials -> auth.json)
#            /model, /settings ...     # defaults, models.json, ...
```

pigeon considers pi configured when `auth.json` or `models.json` exists in
the config dir. Until then:

* `POST /webhook/<bot>` → `503 {"error": "pi is not configured ..."}`
* `GET /health` → `503` with `"pi": {"configured": false, ...}`

Full guide: **[docs/docker.md](docs/docker.md)**.

## Endpoints

| Endpoint | Method | Description |
|---|---|---|
| `/webhook/<bot>` | POST | Talk bot webhook. 202 = queued, 200 = ignored, 400 = bad bot name, 403 = bad token, 503 = not configured |
| `/health` | GET | Health + pi config + heartbeat + tasks (200 ok / 503 degraded) |
| `/` | GET | Service info |

Example health output:

```json
{
  "status": "ok",
  "pi": {"configured": true, "config_file": "/pi-home/agent/auth.json"},
  "heartbeat": {"last_run": 1760180000.123},
  "stats": {"completed": 12, "failed": 0, "retries": 1, "uptime_seconds": 3600.4},
  "tasks": [
    {"id": "1760180000-42-3", "bot": "bot1", "room_cwd": "/bots/bot1/rooms/dev-team",
     "status": "running", "retries": 0, "age_seconds": 42.1,
     "process_alive": true, "error": null}
  ]
}
```

## Configuration

All configuration is via environment variables (see `.env.example`):

| Variable | Default | Description |
|---|---|---|
| `NEXTCLOUD_URL` | – | Base URL of Nextcloud (no trailing slash) |
| `TALK_BOT_SECRET` | – | Secret from `occ talk:bot:install <name> <secret> <url>` (40-128 chars); verifies webhooks and signs bot API calls |
| `LOG_LEVEL` | `INFO` | python logging level (`DEBUG`/`INFO`/`WARNING`/`ERROR`) |
| `PI_TIMEOUT` | `600` | Seconds before the heartbeat kills a pi run |
| `HEARTBEAT_INTERVAL` | `30` | Seconds between heartbeat checks |
| `STATE_SCAN_EVERY` | `10` | Every Nth heartbeat also scans state files and re-queues stale jobs (crash self-healing) |
| `SCHEDULER_INTERVAL` | `30` | Seconds between checks of room `schedules.json` files (scheduled tasks) |
| `MAX_RETRIES` | `1` | Reruns a failed/timed-out task in the same room session |
| `MESSAGE_BATCH_WINDOW` | `5` | Seconds to wait for more messages from the room before sending the burst to pi (0 = immediate) |
| `SANDBOX` | `1` | Run pi in a bubblewrap sandbox (ro root, rw workspace + pi home, full net) |
| `MAX_FILE_SIZE_MB` | `100` | Max size of file attachments downloaded into `workspace/chat/files/` |
| `BOTS_DIR` | `/bots` | Where per-bot room folders live |
| `PI_CODING_AGENT_DIR` | `/pi-home/agent` | pi config dir (auth.json, models.json, sessions) |
| `PORT` | `8080` | HTTP port |

pi itself (model, thinking level, provider keys, extensions) is
configured via pi's default config files — see [docs/docker.md](docs/docker.md).
Skills are plain folders — see below.

## Skills & scheduled tasks

pi supports [skills](https://github.com/badlogic/pi-skills) — folders with a `SKILL.md`
(name + description + instructions, optionally scripts). pigeon ships two:

* **`scheduler`** — teaches pi how to manage this room's `schedules.json`
  (create / list / change / delete scheduled tasks).
* **`austria-news`** — example content skill (fetches Austrian headlines).

They are shipped in the repo (`./skills/`) and baked into the image; at
startup pigeon copies them into `$PI_CODING_AGENT_DIR/skills` inside the
`pi-home` volume — bundled skills are **updated on every start** when you
deploy a new image, while your own skills (other folders in that dir) are
left untouched. Add your own skills either by dropping a folder into the
volume's `skills/` dir (user skill) or into `./skills/` in the repo and
rebuilding (bundled skill).

### Scheduling

Because of the `scheduler` skill, you can just ask the bot in a room:

> send me news for austria every day at 09:00
> list all scheduled tasks
> remove the news schedule

pi writes a `schedules.json` into the room folder, e.g.
`{"schedules": [{"id": "at-news", "prompt": "…", "time": "09:00",
"tz": "Europe/Vienna"}]}`. pigeon's scheduler thread checks those files
every `SCHEDULER_INTERVAL` seconds; when an entry is due it sends its
`prompt` to the room like a normal chat message (same pi session, same
queue, survives restarts — `next_run` is persisted). Supported entries:

* daily at `HH:MM` in an IANA timezone (`time` + `tz`)
* every N minutes (`every_minutes` ≥ 5)

A room only gets scheduled messages after it has received at least one
webhook message (pigeon stores the room's conversation token in
`.room.json` to post to).


## Files

```
pigeon/
  config.py       environment variables + logging
  security.py     webhook/bot-API HMAC signatures, 24h /nc room tokens
  room.py         room folders, .room.json (token + app password), /pw
  nextcloud.py    bot API posting, WebDAV file download, CalDAV helpers
  pi.py           pi config check, bubblewrap sandbox, JSONL run
  tasks.py        per-room queues + workers, retries, heartbeat
  scheduler.py    schedules.json time math + tick
  skills.py       bundled skill sync into the pi home
  nc_proxy.py     /nc routes: token-gated Nextcloud proxy for pi
  web.py          Flask app: /, /health, /webhook/<bot>, startup/shutdown
wsgi.py           gunicorn WSGI entrypoint + config module (post_fork -> pigeon.web.startup())
skills/           pi skills mounted into the container (scheduler, austria-news, …)
Dockerfile        node:24 + python3 + pi image, runs gunicorn
docker-compose.yml
.env.example
LICENSE           MIT
bots/             one folder per bot (bind mount), rooms/ inside
docs/docker.md    docker + pi configuration documentation
test/smoke.py     local end-to-end test with fake pi + fake Nextcloud
test/nc_stub.py   fake Nextcloud (bot API, verifies outgoing signatures)
```

Test without Docker:

```bash
python test/smoke.py   # starts fake Nextcloud + fake pi, exercises the full flow
```

## Notes & limitations (on purpose)

* Production server is **gunicorn** (single worker, threaded — pigeon keeps
  its state in process memory, so it runs with exactly one worker);
  `python -m pigeon.web` (Flask dev server) still works for local tinkering.
* The bot posts results as the bot identity via the signed bot API; it does
  not reply thread-style to the triggering message.
* Message text is sent to pi verbatim as the prompt; there is no templating.
* Reactions and management events from Talk are ignored (message events
  only).
