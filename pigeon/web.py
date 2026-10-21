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

from . import indicator, nc_proxy, scheduler, skills, stt, tasks
from .config import (BOTS_DIR, COMMIT, HEARTBEAT_INTERVAL, PI_CONFIG_DIR,
                     TALK_BOT_SECRET, VERSION, log)
from .nextcloud import (apply_file_param, dav_file_url, payload_file_param,
                        post_message, save_download)
from .pi import pi_config_status
from .room import (BOT_NAME_RE, HELP_TEXT, clean_session_and_workspace,
                   resolve_room_folder, handle_bot_added, handle_bot_removed,
                   handle_mention_command, handle_memory_command,
                   handle_mode_command, handle_pw_command,
                   handle_thinking_command, mode_start_prompt, room_creds,
                   room_dir, room_meta_read, room_mode, strip_mention,
                   write_room_token)
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
    """Normalise one ActivityPub webhook event into a dict:
      action:    "message" | "bot_joined" | "bot_left"
      room:      conversation token
      room_name: display name of the room
      bot_name:  Talk's bot name (joined/left only)
      message:   chat text (message only)
      sender:    sender display name (message only)
      message_id: Talk message id (message only; used for reactions)
    or None when the event is not one we handle.
    Regular chat:  {type: Create, object: Note, target: Collection}
    Bot join/leave: {type: Join|Leave, object: Collection} (no Note and
    no target - the object IS the room)."""
    obj = payload.get("object") or {}
    target = payload.get("target") or {}

    if payload.get("type") in ("Join", "Leave") \
            and obj.get("type") == "Collection":
        room_token = obj.get("id") or ""
        if not re.fullmatch(r"[a-z0-9]{4,30}", room_token):
            return None
        actor = payload.get("actor") or {}
        return {"action": "bot_joined" if payload["type"] == "Join"
                else "bot_left",
                "room": room_token,
                "room_name": (obj.get("name") or "").strip(),
                "bot_name": (actor.get("name") or "").strip(),
                "message": "", "sender": "system"}

    if obj.get("type") == "Note" and target.get("type") == "Collection":
        room_token = target.get("id") or ""
        if not re.fullmatch(r"[a-z0-9]{4,30}", room_token):
            return None
        if obj.get("name") not in ("message", "file_shared"):
            return None  # ignore reactions, management events, ...
        try:
            content = json.loads(obj.get("content") or "{}")
            message = (content.get("message") or "").strip()
        except (json.JSONDecodeError, AttributeError):
            message = ""
        actor = payload.get("actor") or {}
        return {"action": "message", "room": room_token,
                "room_name": (target.get("name") or "").strip(),
                "bot_name": "",
                "message": message,
                "message_id": str(obj.get("id") or ""),
                "sender": (actor.get("name") or "").strip() or "user"}
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
        return jsonify({"ignored": "not a handled event"}), 200

    room = parsed["room"]
    room_name = parsed["room_name"]
    folder = resolve_room_folder(bot, room, room_name)
    cwd = room_dir(bot, folder)

    # bot added to the room -> one-time greeting (no pi involved)
    if parsed["action"] == "bot_joined":
        os.makedirs(cwd, exist_ok=True)
        write_room_token(cwd, room)  # needed for scheduled tasks to reply
        reply = handle_bot_added(parsed.get("bot_name") or bot, room_name,
                                 cwd)
        if reply:
            post_message(room, reply)
            return jsonify({"handled": "greet"}), 200
        return jsonify({"ignored": "already greeted"}), 200

    # bot removed from the room -> total clean (no pi involved)
    if parsed["action"] == "bot_left":
        handle_bot_removed(cwd)
        return jsonify({"handled": "wiped"}), 200

    # regular chat message from here on
    message = parsed["message"]
    sender = parsed["sender"]

    # voice message (audio/wav, audio/mpeg) — may arrive with no message
    # text at all: transcribe it, post the text back and hand the text to pi
    f = payload_file_param(payload)
    if f is not None and stt.is_voice_file(f):
        return handle_voice_message(bot, folder, room, f, cwd, sender,
                                    parsed["message_id"])

    if not message:
        return jsonify({"ignored": "empty message"}), 200

    os.makedirs(cwd, exist_ok=True)
    write_room_token(cwd, room)  # needed for scheduled tasks to reply

    # room command: app password for file access (never reaches pi)
    reply = handle_pw_command(message, cwd, room)
    if reply is not None:
        post_message(room, reply)
        return jsonify({"handled": "!pw"}), 200

    # room command: !clean (+ !c) -> drop the pi sessions AND wipe the
    # workspace, so the next message starts a fresh pi session with no
    # files behind
    if message in ("!clean", "!c"):
        reply = clean_session_and_workspace(cwd)
        # a fresh session is a fresh start for start-prompt personas
        # (e.g. finance asks again which country applies)
        start = mode_start_prompt(room_mode(cwd))
        if start and "nothing to clear" not in reply:
            reply += "\n\n" + start
        post_message(room, reply)
        return jsonify({"handled": "!clean"}), 200

    # room command: !thinking (+ !t) -> post pi's last saved thinking
    reply = handle_thinking_command(message, cwd)
    if reply is not None:
        post_message(room, reply)
        return jsonify({"handled": "!thinking"}), 200

    # room commands: !stop (or !s), !schedule list|rm, !mention, !help
    # (or !h), !version (or !v) — none of these ever reach pi
    if message in ("!stop", "!s"):
        stopped = tasks.stop_running(cwd)
        post_message(room,
                     "pi run stopped \u2014 the room is free again."
                     if stopped else
                     "Nothing is running in this room right now.")
        return jsonify({"handled": "!stop"}), 200

    reply = scheduler.handle_schedule_command(message, cwd)
    if reply is not None:
        post_message(room, reply)
        return jsonify({"handled": "!schedule"}), 200

    reply = handle_mention_command(message, cwd)
    if reply is not None:
        post_message(room, reply)
        return jsonify({"handled": "!mention"}), 200

    reply = handle_mode_command(message, cwd)
    if reply is not None:
        post_message(room, reply)
        return jsonify({"handled": "!mode"}), 200

    reply = handle_memory_command(message, cwd)
    if reply is not None:
        post_message(room, reply)
        return jsonify({"handled": "!memory"}), 200

    meta = room_meta_read(cwd)
    if message in ("!help", "!h"):
        names = [n for n in (meta.get("bot_name"), "bot") if n]
        mention = " or ".join("@" + n for n in names)
        post_message(room, HELP_TEXT.format(mention=mention))
        return jsonify({"handled": "!help"}), 200

    if message in ("!version", "!v"):
        post_message(room, f"pigeon {VERSION} ({COMMIT})")
        return jsonify({"handled": "!version"}), 200

    # mention mode: when on, only messages that mention the bot
    # (@<bot name> or @bot) reach pi (the mention is stripped first) —
    # the commands above always work
    if meta.get("mention_only"):
        message = strip_mention(message, meta.get("bot_name"))
        if message is None:
            return jsonify({"ignored": "no mention"}), 200
        if not message:
            return jsonify({"ignored": "empty message"}), 200

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
    if f is not None or "{file}" in message:
        message = (apply_file_param(message, f, cwd) if f
                   else message.replace("{file}",
                                        "[file: no file data in payload]"))

    key = f"{bot}/{folder}"
    ind = (indicator.Indicator(room, parsed["message_id"])
           if parsed["message_id"] else None)
    tasks.enqueue(key, bot, {
        "room": room,
        "room_id": folder,
        "message": message,
        "indicator": ind,
        "sender": sender,
        "cwd": cwd,
        "workspace": os.path.join(cwd, "workspace"),
        "batch": True,
    })
    log.info("queued message from %s in %s", sender, key)
    return jsonify({"queued": True, "bot": bot, "room_folder": folder}), 202


def handle_voice_message(bot, folder, room, f, cwd, sender, message_id=""):
    """Voice message (audio/wav, audio/mpeg): download the file, transcribe
    it with the configured STT endpoint (.pigeon.json in the bots folder),
    post the extracted text back to the room and queue that text for pi.
    Any failure is reported in the chat — pi never sees the raw audio."""
    name = f.get("name") or "voice message"
    os.makedirs(cwd, exist_ok=True)
    write_room_token(cwd, room)  # needed for scheduled tasks to reply

    cfg = stt.transcription_config()
    if cfg is None:
        reply = ("⚠ Voice messages are not configured yet: set "
                 "transcription.endpoint, transcription.model and "
                 "transcription.api_key in .pigeon.json in the bots "
                 "folder, then send the message again.")
        post_message(room, reply)
        return jsonify({"handled": "stt-unconfigured"}), 200

    creds = room_creds(cwd)
    if creds is None:
        reply = ("⚠ I cannot download voice messages: this room has no "
                 "app password for file access yet. Send "
                 "!pw <nextcloud-user> <app-password> first.")
        post_message(room, reply)
        return jsonify({"handled": "stt-no-credentials"}), 200

    try:
        dl = dav_file_url(creds, int(f.get("id")))
        saved, _size = save_download(
            dl, name, os.path.join(cwd, "workspace", "chat", "files"),
            auth=creds)
        wav_path = os.path.join(cwd, "workspace", "chat", "files", saved)
    except Exception as e:
        log.warning("voice download failed (%s): %s", name, e)
        post_message(room, f"⚠ Downloading the voice message failed: {e}")
        return jsonify({"error": "download failed"}), 502

    try:
        text = stt.transcribe(cfg, wav_path)
    except Exception as e:
        log.warning("voice transcription failed (%s): %s", name, e)
        post_message(room, f"⚠ Transcription failed: {e}")
        return jsonify({"error": "transcription failed"}), 502

    post_message(room, "\U0001f442 " + text)
    key = f"{bot}/{folder}"
    ind = (indicator.Indicator(room, message_id)
           if message_id else None)
    tasks.enqueue(key, bot, {
        "room": room,
        "room_id": folder,
        "message": text,
        "indicator": ind,
        "sender": sender,
        "cwd": cwd,
        "workspace": os.path.join(cwd, "workspace"),
        "batch": True,
    })
    log.info("voice message transcribed in %s (%d chars)", key, len(text))
    return jsonify({"queued": True, "bot": bot, "transcribed": True}), 202


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
    stt.ensure_config_file()  # .pigeon.json boilerplate in the bots folder
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
