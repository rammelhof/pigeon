# Docker environment — configuration guide

This document explains how the Docker environment is put together and how to
configure it: volumes, pi configuration, credentials, and networking.

## 1. The image

`Dockerfile` builds a single image that contains everything the webhook
server needs:

| Layer | What it installs | Why |
|---|---|---|
| `node:24-bookworm-slim` | Node.js 24 | pi is an npm package |
| apt | `python3`, `python3-pip`, `git`, `ripgrep` | server runtime + tools pi uses |
| `npm install -g @earendil-works/pi-coding-agent` | `pi` CLI | the agent |
| pip | `flask`, `requests` | the webhook server |
| copy | `app.py` | the server itself |

Build:

```bash
docker compose build          # or: docker build -t pigeon .
```

## 2. Run

```bash
cp .env.example .env          # edit this file
docker compose up -d
docker compose logs -f        # watch startup (LOG_LEVEL controls verbosity)
curl -s localhost:8080/health
```

Stop / update:

```bash
docker compose down           # volumes are kept
docker compose up -d --build  # after code changes
```

## 3. Volumes

Two volumes are mounted (see `docker-compose.yml`):

### `./bots` → `/bots` (bind mount)

One folder **per bot** (from the `/webhook/<bot>` URL), and inside it one
folder **per Talk room**, created automatically on first message:

```text
bots/
├── bot1/
│   └── rooms/
│       ├── dev-team/        # working directory of pi for room "Dev Team"
│       │   └── .pigeon.json # persisted task queue (pending/running)
│       ├── home-office/
│       └── room-47/         # fallback name when the room has no name (1:1)
└── bot2/
    └── rooms/
        └── ...
```

Because pi is started with `cwd=bots/<bot>/rooms/<room>`:

* files pi creates belong to that room,
* pi's per-directory sessions live in the pi config dir
  (`PI_CODING_AGENT_DIR/sessions/...`), so every room keeps its own
  conversation context,
* you can drop an `AGENTS.md` into a room folder to give that room's agent
  extra instructions (pi loads `AGENTS.md` from the working directory).

Bind-mounting the tree on the host means you can inspect/backup/edit room
state directly. Delete a bot or room folder (while the container is
stopped) to reset it.

### `pi-home` → `/pi-home` (named volume)

Holds pi's global config directory
(`PI_CODING_AGENT_DIR=/pi-home/agent`) — the **default pi config files**:

* `auth.json` — provider credentials from `/login`,
* `models.json` — custom model/provider definitions,
* `settings.json`, extensions, skills,
* `sessions/` — session storage (keyed by working directory → per room).

Named volume = survives `docker compose down` and image rebuilds.
Back it up with e.g.

```bash
docker run --rm -v pi-home:/data -v "$PWD":/backup alpine \
  tar czf /backup/pi-home.tar.gz -C /data .
```

> **Do not** bind-mount your host `~/.pi/agent` here unless you
> deliberately want the container to share your host pi configuration and
> credentials.

### `./skills` → copied into `/pi-home/agent/skills` at startup

pi's user skills directory. pigeon ships the `scheduler` and
`austria-news` skills **inside the image** (`/app/skills`); at every
startup `sync_skills()` copies them into `/pi-home/agent/skills` in the
`pi-home` volume: bundled skills are updated (delete + copy) when you
deploy a new image, any other folders (your own skills) are left
untouched. See section 8.2.

## 4. pi configuration

pigeon passes **no** model or API-key settings to pi. Pi is configured
exclusively with its default config files in the `pi-home` volume:

```bash
docker compose exec pigeon pi
# inside pi:
#   /login     -> writes auth.json
#   /model     -> choose default model
#   /settings  -> defaults such as thinking level
# then /quit
```

For custom/local providers, create `/pi-home/agent/models.json`
(e.g. for a Llama.cpp server):

```bash
docker compose exec pigeon vi /pi-home/agent/models.json
```

**pigeon's "configured" check:** the webhook answers `503` and `/health`
reports `"pi": {"configured": false, ...}` until `auth.json` or
`models.json` exists (non-empty) in `PI_CODING_AGENT_DIR`. Verify any time
with:

```bash
curl -s localhost:8080/health | python3 -m json.tool
```

Extensions / skills go into the config dir as usual:

```bash
docker compose exec pigeon mkdir -p /pi-home/agent/extensions
# mount or copy your extension folder there
```

## 5. Environment variables

All in `.env` (compose reads it automatically):

| Group | Variable | Notes |
|---|---|---|
| Nextcloud | `NEXTCLOUD_URL` | e.g. `https://cloud.example.com` |
| Nextcloud | `TALK_BOT_SECRET` | Secret from `occ talk:bot:install <name> <secret> <url>` (40-128 chars). Pigeon verifies the HMAC signature of every incoming webhook with it and signs its own calls to the Talk bot API with it |
| logging | `LOG_LEVEL` | `DEBUG` / `INFO` (default) / `WARNING` / `ERROR` — output goes to stdout, read with `docker compose logs -f` |
| behaviour | `PI_TIMEOUT` | heartbeat kills a pi run after this many seconds |
| behaviour | `HEARTBEAT_INTERVAL` | seconds between task checks (default 30) |
| behaviour | `STATE_SCAN_EVERY` | every Nth heartbeat also scans the `.pigeon.json` files and re-queues jobs that are on disk but no longer tracked in memory (crash self-healing; default 10 → every ~5 min) |
| behaviour | `SCHEDULER_INTERVAL` | seconds between checks of the rooms' `schedules.json` files (default 30) |
| behaviour | `SKILLS_SRC` | dir in the image with bundled skills, copied to `$PI_CODING_AGENT_DIR/skills` at startup (default `/app/skills`) |
| behaviour | `MAX_RETRIES` | how often a failed/timed-out task is rerun in the same room session |
| behaviour | `PORT` | HTTP port (published as `8080:8080`) |

## 6. Networking

* The container needs **outbound** HTTPS to your Nextcloud and to your LLM
  provider. Inbound it only serves `8080`.
* The Nextcloud server must be able to reach the webhook:
  `https://YOUR-SERVER:8080/webhook/<bot>` (that URL — including the bot
  path — is what you pass to `occ talk:bot:install`). If Nextcloud and this
  container share a Docker network, you can use the container name/DNS
  instead of the public URL.
* Every webhook is HMAC-signed by Nextcloud (headers
  `X-Nextcloud-Talk-Random` / `X-Nextcloud-Talk-Signature`), so the body
  itself does not contain the secret — still, keep the endpoint behind TLS
  on untrusted networks.
* **File attachments & per-room app passwords**: Talk file messages carry
  a `{file}` rich-object placeholder. Its `link` is a Talk *virtual* link
  (it 404s), but its `id` is a real file id, so pigeon resolves it with a
  single WebDAV SEARCH (`basicsearch` on `oc:fileid`, Basic auth with the
  room's **app password**) which returns the file's dav `href`, then a
  plain authenticated GET streams it into the room's `chat/files/`
  folder — no direct API, no temp share, no folder iteration. Each room
  keeps its own credentials in
  its `.room.json` (`{"token": …, "nextcloud_user": …,
  "nextcloud_password": …}`, mode 600) — rooms do not share information
  or accounts. Set it by sending `/pw <user> <app-password>` in the room
  (`/pw` alone shows the status); pigeon intercepts the command, stores
  it per room and never lets it reach pi. While a room has no app
  password, a file message triggers a task where the bot is instructed to
  tell the user — in the user's own language — that the password for
  file access is missing and how to set it. Downloads are capped at
  `MAX_FILE_SIZE_MB`; failures become a hint with the share link so the
  bot can still react. Use *app passwords* (Personal Information →
  Security), one per room if you want different permissions; the `/pw`
  chat message stays in the room history — delete it afterwards or edit
  `.room.json` on the host instead.

## 7. Heartbeat & task monitoring

* A background thread wakes every `HEARTBEAT_INTERVAL` seconds.
* For every started task it checks:
  * **running + process alive + age > `PI_TIMEOUT`** → kill the process; the
    worker reruns the task in the same room session (same `--session-id`)
    up to `MAX_RETRIES` times, then posts a failure notice to the room.
  * **running + no process + age > `PI_TIMEOUT`+30s** → stuck (e.g. while
    posting results) → marked `failed`.
* **State scan**: every `STATE_SCAN_EVERY`th tick the heartbeat also scans
  all `.pigeon.json` files and re-queues entries that are on disk but no
  longer tracked in memory — jobs left over from a crash are picked up
  without a restart. Live entries (pending `job_id` / running `task_id`
  still in memory) are left alone. Entries without a room token (old
  versions) are dropped, since their result could not be posted.
* `GET /health` shows the last heartbeat timestamp, pi configuration status,
  counters (`completed`/`failed`/`retries`) and every task with its status,
  age and whether its pi process is alive. Returns `503` when pi is not
  configured or the heartbeat stopped — wire this into your monitoring.

## 8. Multiple bots

Register several Nextcloud bots pointing at different paths of the same
container. Pigeon verifies with a single `TALK_BOT_SECRET`, so use the
same secret for all bots registered on it:

```bash
./occ talk:bot:install pi1 <secret> https://ai.lan:8080/webhook/bot1 "team bot"
./occ talk:bot:setup <bot1-id> <conversation-token-1>
./occ talk:bot:install pi2 <secret> https://ai.lan:8080/webhook/bot2 "support bot"
./occ talk:bot:setup <bot2-id> <conversation-token-2>
```

They share the one pi config (model, credentials) but get fully separate
room folders and sessions under `bots/bot1/rooms/` and `bots/bot2/rooms/`.
If you need different models or secrets per bot, run one pigeon container
per bot with its own `pi-home` volume and `TALK_BOT_SECRET`.

## 8.1 Restarting & stopping

* **Restart** (`docker compose restart pigeon`): safe. Queued (pending) and
  interrupted (running) tasks are stored in `<room>/.pigeon.json` and are
  re-queued automatically at startup — and the periodic state scan picks
  up jobs left on disk after a crash even without a restart. Every room
  always runs in its
  stable pi session (`--session-id <room folder>`), and pi's session state
  lives in the room folders
  (`/bots`) and the `pi-home` volume, both of which survive a restart.
* **Stop** (`docker stop pigeon`): fast. On SIGTERM pigeon kills active pi
  runs and exits, so the container stops in well under a second. If the
  stop takes ~10 s, you are running an old image without the shutdown
  handler — rebuild with `docker compose up -d --build`.

## 8.2 Skills & scheduled tasks

**Skills.** pi loads skills from `$PI_CODING_AGENT_DIR/skills`
(`/pi-home/agent/skills` in the `pi-home` volume). pigeon ships the
`scheduler` and `austria-news` skills inside the image (`/app/skills`)
and copies them into that dir at **every startup** (`sync_skills`):
bundled skills are updated when a new image is deployed, other folders
(user skills added to the volume) are left untouched. Each skill is a
folder with a `SKILL.md` (frontmatter `name`/`description` + instructions,
optionally scripts). pi advertises skills in its system prompt and loads
the full instructions when a task matches — you can also force one with
`/skill:<name>`. Two are shipped:

* `scheduler` — how to manage this room's `schedules.json`,
* `austria-news` — example: fetch and brief Austrian headlines.

Add your own: a folder in the volume's `skills/` dir (per-deployment)
or in `./skills/` in this repo (bundled, updated with rebuilds).

**Scheduling.** With the `scheduler` skill you simply ask the bot in a
room, e.g. *"send me news for austria every day at 09:00"* or
*"list all scheduled tasks"*. pi maintains `<room dir>/schedules.json`:

```json
{"schedules": [
  {"id": "at-news", "prompt": "Send the Austrian news brief.",
   "time": "09:00", "tz": "Europe/Vienna"},
  {"id": "standup", "prompt": "…", "every_minutes": 60,
   "window": {"start": "10:00", "end": "22:00", "tz": "Europe/Vienna"}}
]}
```

`window` (only with `every_minutes`) confines runs to a local time
window; a run that would fall outside snaps to the next window start
("every hour between 10:00 and 22:00" → runs at 10, 11, …, 21:00).
`start_time` ("HH:MM", also only with `every_minutes`) anchors the
phase: runs happen at `start_time + N·interval` (wall clock, in the
entry's timezone — `window.tz`, else `tz`, else UTC), so the next run
is fully predictable and stable across restarts ("every 15 minutes
starting at 10:30" → 10:30, 10:45, 11:00, …).

pigeon's scheduler thread checks these files every `SCHEDULER_INTERVAL`
seconds. Due entries send their `prompt` to the room like a normal chat
message — same pi session, same queue, same retry/timeout semantics, and
`next_run` is persisted before firing, so restarts and crashes neither
lose nor duplicate a run. Notes:

* a room only receives scheduled messages after it got at least one real
  webhook message (pigeon stores the conversation token in
  `<room dir>/.room.json` to post to); otherwise due entries are skipped
  and logged,
* timezones are IANA names (`Europe/Vienna`); the container image
  includes the tz database,
* the fired run happens in the room's pi session, so it has full
  conversation context.

## 9. Troubleshooting

| Symptom | Check |
|---|---|
| `403 bad signature` in logs | `TALK_BOT_SECRET` in `.env` ≠ secret in `occ talk:bot:install` |
| `503 pi is not configured` | run `docker compose exec pigeon pi` → `/login` (creates `auth.json`), then `curl localhost:8080/health` |
| Bot never answers | bot not added to the conversation (`occ talk:bot:setup <bot-id> <token>`), or Nextcloud cannot reach `:8080/webhook/<bot>`; `docker compose logs -f` (raise `LOG_LEVEL=DEBUG`) |
| `room name lookup failed` warnings | `NEXTCLOUD_BOT_TOKEN` invalid for API calls. Folders fall back to `room-<id>` |
| `pi run failed: …` in chat | pi run crashed (see `docker compose logs -f`); check model/provider in the pi config files |
| Answers always time out | raise `PI_TIMEOUT`, or check provider reachability from the container |
| Reset a room | stop container, delete `bots/<bot>/rooms/<room>`, remove that room's session dir in the `pi-home` volume |
