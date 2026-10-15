"""pigeon — Nextcloud Talk bot -> pi agent webhook server (KISS).

Endpoints:
  POST /webhook/<bot>  - Nextcloud Talk bot webhook (incoming chat messages)
  GET  /nc/...         - token-gated Nextcloud proxy (files + calendar) for pi
  GET  /health         - health check + pi config status + task overview
  GET  /               - short info page

Nextcloud Talk bot protocol (https://nextcloud-talk.readthedocs.io/en/latest/bots/):
  * incoming webhook: W3C activity object, verified with
    X-Nextcloud-Talk-Random + X-Nextcloud-Talk-Signature
    (HMAC-SHA256(random + raw body, TALK_BOT_SECRET))
  * posting: official bot API
    POST /ocs/v2.php/apps/spreed/api/v1/bot/{roomToken}/message
    with the same signature headers (bot feature "response")

Behaviour:
  * Every incoming message is queued per bot+room (one worker thread per
    room, so pi sessions are never interleaved). The queue is persisted
    in <room dir>/.pigeon.json, so pending and interrupted (running)
    jobs survive container restarts and are re-queued at startup.
  * Each Talk room gets its own folder under BOTS_DIR/<bot>/rooms/, named
    after the room. Pi runs in the room's workspace/ subfolder (inside a
    bubblewrap sandbox: read-only root, only the workspace and the pi
    home are writable) with full network access, so pi's per-directory
    sessions keep per-room conversation context.
  * Pi must be configured with its default config files (auth.json /
    models.json in PI_CODING_AGENT_DIR). When not configured, the webhook
    answers 503 and /health reports the error.
  * Pi is invoked with `--mode json --session-id <room folder>`: every
    room always uses the same stable pi session (created on first use),
    so the bot keeps full conversation context per room across messages
    and restarts.
  * A background heartbeat task checks all started tasks: it kills runs
    that exceed PI_TIMEOUT and flags stuck tasks. Failed runs are rerun
    in the same room session up to MAX_RETRIES times.
  * SIGTERM/SIGINT kills active pi runs and exits immediately, so
    `docker stop` is fast (no orphaned node processes keeping the
    container alive until the grace-period SIGKILL).
"""
