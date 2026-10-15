"""Per-room layout and persistence.

  BOTS_DIR/<bot>/rooms/<folder>/          one folder per Talk room
    .room.json                            room token (replies/schedules) +
                                          per-room Nextcloud app password
    .pigeon.json                          pending queue + running job, so
                                          jobs survive restarts
    workspace/                            pi's working directory
"""

import json
import os
import re
import threading

from .config import BOTS_DIR, log

BOT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
BOT_DIR_RE = re.compile(r"[a-z0-9][a-z0-9_-]*")
ROOM_DIR_RE = re.compile(r"[a-z0-9][a-z0-9_.-]*")

_lock = threading.Lock()


def folder_for_room(room_id, room_name):
    """Sanitised room name, fallback to the room token."""
    clean = re.sub(r"[^a-zA-Z0-9_-]+", "-", room_name or "").strip("-").lower()
    return clean or f"room-{room_id}"


def room_dir(bot, folder):
    """Room folder under BOTS_DIR for a bot + room folder name."""
    return os.path.join(BOTS_DIR, bot, "rooms", folder)


# ------------------------------------------------------------ .room.json --

def _room_meta_path(cwd):
    return os.path.join(cwd, ".room.json")


def room_meta_read(cwd):
    try:
        with open(_room_meta_path(cwd)) as f:
            meta = json.load(f)
        return meta if isinstance(meta, dict) else {}
    except Exception:
        return {}


def room_meta_write(cwd, meta):
    path = _room_meta_path(cwd)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(meta, f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def room_token(cwd):
    """Conversation token of the room (written by the webhook)."""
    return room_meta_read(cwd).get("token")


def write_room_token(cwd, token):
    meta = room_meta_read(cwd)
    if meta.get("token") == str(token):
        return
    meta["token"] = str(token)
    room_meta_write(cwd, meta)


def room_creds(cwd):
    """(user, password) for this room from .room.json, or None."""
    meta = room_meta_read(cwd)
    user, pw = meta.get("nextcloud_user"), meta.get("nextcloud_password")
    if user and pw:
        return user, pw
    return None


# ---------------------------------------------------------- .pigeon.json --

def _state_path(cwd):
    return os.path.join(cwd, ".pigeon.json")


def state_read(cwd):
    try:
        with open(_state_path(cwd)) as f:
            state = json.load(f)
    except Exception:
        return {"pending": [], "running": None}
    state.setdefault("pending", [])
    state.setdefault("running", None)
    return state


def state_update(cwd, fn):
    """Load state, apply fn(state), write it back atomically."""
    with _lock:
        state = state_read(cwd)
        fn(state)
        tmp = _state_path(cwd) + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f)
        os.replace(tmp, _state_path(cwd))
        return state


def mark_running(cwd, task):
    def upd(state):
        state["running"] = {
            "message": task["message"],
            "sender": task["sender"],
            "room": task["room"],
            "started_at": task["started_at"],
            "retries": task["retries"],
            "task_id": task["id"],
        }
    state_update(cwd, upd)


def clear_running(cwd, task_id):
    def upd(state):
        if state.get("running") and state["running"].get("task_id") == task_id:
            state["running"] = None
    state_update(cwd, upd)


# ------------------------------------------------------------- /pw cmd ----
# Stored per room (isolation between rooms); the command never reaches pi.

PW_SET_RE = re.compile(r"^/pw\s+(\S+)\s+(\S+)\s*$")
PW_STATUS_RE = re.compile(r"^/pw\s*$")


def handle_pw_command(message, cwd, room):
    """Handle /pw for this room. Returns the reply to post, or None when
    the message is not a /pw command."""
    m = PW_SET_RE.match(message)
    if m:
        meta = room_meta_read(cwd)
        meta["nextcloud_user"], meta["nextcloud_password"] = m.group(1), \
            m.group(2)
        room_meta_write(cwd, meta)
        log.info("room %s: app password stored for user %s",
                 os.path.basename(cwd), m.group(1))
        return ("App password saved for file access in this room "
                f"(user: {m.group(1)}).")
    if PW_STATUS_RE.match(message):
        creds = room_creds(cwd)
        if creds:
            return f"File access is configured for user: {creds[0]}."
        return ("File access is not configured for this room. Send "
                "/pw <nextcloud-user> <app-password> to enable file "
                "downloads.")
    return None
