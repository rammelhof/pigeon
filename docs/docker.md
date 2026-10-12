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

## 7. Heartbeat & task monitoring

* A background thread wakes every `HEARTBEAT_INTERVAL` seconds.
* For every started task it checks:
  * **running + process alive + age > `PI_TIMEOUT`** → kill the process; the
    worker reruns the task in the same room session (same `--session-id`)
    up to `MAX_RETRIES` times, then posts a failure notice to the room.
  * **running + no process + age > `PI_TIMEOUT`+30s** → stuck (e.g. while
    posting results) → marked `failed`.
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
  re-queued automatically at startup. Every room always runs in its
  stable pi session (`--session-id <room folder>`), and pi's session state
  lives in the room folders
  (`/bots`) and the `pi-home` volume, both of which survive a restart.
* **Stop** (`docker stop pigeon`): fast. On SIGTERM pigeon kills active pi
  runs and exits, so the container stops in well under a second. If the
  stop takes ~10 s, you are running an old image without the shutdown
  handler — rebuild with `docker compose up -d --build`.

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
