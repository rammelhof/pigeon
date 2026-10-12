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
  Each message is put in a per-room queue; **one worker thread per room**
  runs pi, so sessions are never interleaved.
* Every Talk room gets its own folder under `bots/<bot>/rooms/`, named
  after the room (sanitised). pi runs with that folder as working
  directory — pi's per-directory sessions give you conversation memory per
  room for free.
* pi is invoked as `pi --mode json "<message>"`. The JSONL event stream is
  parsed; the assistant's **thinking** (quoted with 🤔) and the **answer**
  are posted back into the conversation via the Talk API.
* A **heartbeat thread** runs every `HEARTBEAT_INTERVAL` seconds and checks
  all started tasks:
  * pi process still alive past `PI_TIMEOUT` → killed; the worker restarts
    the task with `pi --continue` (up to `MAX_RETRIES`),
  * task stuck without a process past the timeout → marked failed.
* `GET /health` returns server status, **pi configuration status**,
  heartbeat info, counters and all tasks (status, age, process alive) —
  200 when healthy, 503 degraded.

## Quick start (Docker)

```bash
cp .env.example .env      # fill in the values
docker compose up -d --build
docker compose logs -f pigeon
```

Then register the bot on your **Nextcloud server** (admin, as the `www-data`
user). Bots can only be added via the command line — the URL path
(`bot1`) becomes the bot's folder name:

```bash
./occ talk:bot:install pi1 https://YOUR-SERVER:8080/webhook/bot1 change-me "pi agent" \
  --respond-to-all-messages
```

* `change-me` must equal `NEXTCLOUD_BOT_TOKEN` in `.env` (pigeon uses the
  same token for its own Talk API calls).
* `--respond-to-all-messages` makes the bot answer every message; without
  it the bot only reacts to commands. Check `./occ talk:bot:install --help`.

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
| `NEXTCLOUD_BOT_TOKEN` | – | Webhook token **and** Talk API bearer token (must match `occ talk:bot:install`) |
| `LOG_LEVEL` | `INFO` | python logging level (`DEBUG`/`INFO`/`WARNING`/`ERROR`) |
| `PI_TIMEOUT` | `600` | Seconds before the heartbeat kills a pi run |
| `HEARTBEAT_INTERVAL` | `30` | Seconds between heartbeat checks |
| `MAX_RETRIES` | `1` | Task restarts (with `pi --continue`) after a failed run |
| `BOTS_DIR` | `/bots` | Where per-bot room folders live |
| `PI_CODING_AGENT_DIR` | `/pi-home/agent` | pi config dir (auth.json, models.json, sessions) |
| `PORT` | `8080` | HTTP port |

pi itself (model, thinking level, provider keys, extensions, skills) is
configured via pi's default config files — see [docs/docker.md](docs/docker.md).

## Files

```
app.py            the whole server (Flask, stdlib only otherwise)
Dockerfile        node:24 + python3 + pi image
docker-compose.yml
.env.example
LICENSE           MIT
bots/             one folder per bot (bind mount), rooms/ inside
docs/docker.md    docker + pi configuration documentation
test/smoke.py     local end-to-end test with fake pi + fake Nextcloud
```

Test without Docker:

```bash
python test/smoke.py   # starts fake Nextcloud + fake pi, exercises the full flow
```

## Notes & limitations (on purpose)

* Uses the Flask dev server — single host, that's it.
* The bot posts results as the bot user identity; it does not reply
  thread-style to the triggering message.
* Message text is sent to pi verbatim as the prompt; there is no templating.
* `NEXTCLOUD_BOT_TOKEN` is sent by Nextcloud in the webhook body on every
  call — keep the webhook URL private / behind TLS.
