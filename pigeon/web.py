"""Flask app: core routes (/, /health, /webhook/<bot>), /nc proxy
blueprint, one-time startup and fast shutdown.

Run:
  dev:    python -m pigeon.web          (flask dev server)
  docker: gunicorn -c python:wsgi wsgi:app   (see wsgi.py)
"""

import json
import os
import re
import signal
import threading
import time

from flask import Flask, jsonify, request

from . import nc_proxy, scheduler, skills, tasks
from .config import (BOTS_DIR, COMMIT, HEARTBEAT_INTERVAL, PI_CONFIG_DIR,
                     TALK_BOT_SECRET, VERSION, log)
from .nextcloud import apply_file_param, payload_file_param, post_message
from .pi import delete_session, pi_config_status
from .room import BOT_NAME_RE, folder_for_room, handle_pw_command, \
    room_dir, write_room_token
from .security import verify_webhook_signature

app = Flask(__name__)
app.register_blueprint(nc_proxy.bp)


# ---------------------------------------------------------------- core ----

@app.get("/")
def index():
    return jsonify({"service": "pigeon", "version": VERSION,
                    "commit": COMMIT,
                    "endpoints": ["/webhook/<bot>", "/nc/...", "/health"]})


@app.get("/health")
def health():
    pi_ok, pi_config_file = pi_config_status()
    tasks_list, stats, heartbeat = tasks.snapshot()
    stats["uptime_seconds"] = round(time.time() - stats["started_at"], 1)
    heartbeat_ok = bool(heartbeat["last_run"]
                        and time.time() - heartbeat["last_run"]
                        < HEARTBEAT_INTERVAL * 3)
    stats_ok = stats["uptime_seconds"] < 60 or heartbeat_ok
    ok = pi_ok and stats_ok
    return jsonify({
        "status": "ok" if ok else "degraded",
        "version": VERSION,
        "commit": COMMIT,
        "pi": {"configured": pi_ok, "config_file": pi_config_file},
        "heartbeat": heartbeat,
        "scheduler": scheduler.state(),
        "stats": stats,
        "tasks": tasks_list,
    }), (200 if ok else 503)


# --------------------------------------------------------------- webhook --

def parse_payload(payload):
    """Return (message, room, room_name, sender) or None
    when the payload is not an actionable chat message.
    room is the conversation token (from target.id)."""
    obj = payload.get("object") or {}
    target = payload.get("target") or {}
    if obj.get("type") == "Note" and target.get("type") == "Collection":
        room_token = target.get("id") or ""
        if not re.fullmatch(r"[a-z0-9]{4,30}", room_token):
            return None
        if obj.get("name") != "message":
            return None  # ignore reactions, management events, ...
        try:
            content = json.loads(obj.get("content") or "{}")
            message = (content.get("message") or "").strip()
        except (json.JSONDecodeError, AttributeError):
            message = ""
        actor = payload.get("actor") or {}
        return (message, room_token, (target.get("name") or "").strip(),
                (actor.get("name") or "").strip() or "user")
    return None


@app.post("/webhook/<bot>")
def webhook(bot):
    if not BOT_NAME_RE.match(bot) or ".." in bot:
        return jsonify({"error": "invalid bot name"}), 400

    raw_body = request.get_data(as_text=True)
    signature = request.headers.get("X-Nextcloud-Talk-Signature", "")
    random_str = request.headers.get("X-Nextcloud-Talk-Random", "")
    payload = request.get_json(silent=True) or {}

    if not signature \
            or not TALK_BOT_SECRET \
            or not verify_webhook_signature(TALK_BOT_SECRET, random_str,
                                            signature, raw_body):
        log.warning("webhook %s: missing or bad signature", bot)
        return jsonify({"error": "bad signature"}), 403

    parsed = parse_payload(payload)
    if parsed is None:
        return jsonify({"ignored": "not a chat message"}), 200
    message, room, room_name, sender = parsed
    if not message:
        return jsonify({"ignored": "empty message"}), 200

    folder = folder_for_room(room, room_name)
    cwd = room_dir(bot, folder)
    os.makedirs(cwd, exist_ok=True)
    write_room_token(cwd, room)  # needed for scheduled tasks to reply

    # room command: app password for file access (never reaches pi)
    reply = handle_pw_command(message, cwd, room)
    if reply is not None:
        post_message(room, reply)
        return jsonify({"handled": "/pw"}), 200

    # room command: /clean -> drop the room's pi session files, so the
    # next message starts a fresh session (no old history in the context)
    if message == "/clean":
        n = delete_session(f"{bot}_{folder}", os.path.join(cwd, "workspace"))
        post_message(room,
                     "\u2705 pi session cleared \u2014 the next message starts "
                     "a fresh pi session" if n else
                     "no pi session in this room yet \u2014 nothing to clear")
        return jsonify({"handled": "/clean"}), 200

    configured, config_file = pi_config_status()
    if not configured:
        log.error("pi is not configured (no %s in %s)",
                  "auth.json or models.json", PI_CONFIG_DIR)
        return jsonify({"error": "pi is not configured — run `pi` in the "
                                 "container once and /login, or add a "
                                 f"models.json to {config_file}"}), 503

    # file attachment: Talk sends each attachment as its own event — the
    # {file} placeholder (if any) and/or a file parameter. Download via
    # WebDAV SEARCH with the room's app password and put the local path in
    # front of the message — or instruct pi that the password is missing.
    f = payload_file_param(payload)
    if f is not None or "{file}" in message:
        message = (apply_file_param(message, f, cwd) if f
                   else message.replace("{file}",
                                        "[file: no file data in payload]"))

    key = f"{bot}/{folder}"
    tasks.enqueue(key, bot, {
        "room": room,
        "room_id": folder,
        "message": message,
        "sender": sender,
        "cwd": cwd,
        "workspace": os.path.join(cwd, "workspace"),
        "batch": True,
    })
    log.info("queued message from %s in %s", sender, key)
    return jsonify({"queued": True, "bot": bot, "room_folder": folder}), 202


# -------------------------------------------------------- startup/shutdown --

def _shutdown(signum, frame):
    """Kill active pi process groups and exit so docker stop is fast."""
    log.info("received %s, stopping active pi runs", signum)
    tasks.kill_active()
    os._exit(128 + signum)


_started = False


def startup():
    """One-time init: signal handlers, dirs, resume from state files,
    heartbeat + scheduler. Runs once per process — called from gunicorn's
    post_fork (wsgi.py) or __main__ below."""
    global _started
    if _started:
        return
    _started = True
    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    os.makedirs(BOTS_DIR, exist_ok=True)
    synced = skills.sync_skills()
    if synced:
        log.info("synced %d bundled skill(s) into %s", synced,
                 os.path.join(PI_CONFIG_DIR, "skills"))
    resumed = tasks.requeue_stale()
    if resumed:
        log.info("resumed %d job(s) from before the restart", resumed)
    threading.Thread(target=tasks.heartbeat_loop, daemon=True,
                     name="heartbeat").start()
    threading.Thread(target=scheduler.scheduler_loop, daemon=True,
                     name="scheduler").start()
    log.info("pigeon %s (%s) ready, bots in %s, pi config in %s",
             VERSION, COMMIT, BOTS_DIR, PI_CONFIG_DIR)


if __name__ == "__main__":
    startup()
    port = int(os.environ.get("PORT", "8080"))
    log.info("listening on :%d (flask dev server — use gunicorn in docker)",
             port)
    app.run(host="0.0.0.0", port=port)
