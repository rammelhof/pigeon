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
import shutil
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


# ------------------------------------------------------------ /clean cmd --
# Stored per room; the command never reaches pi. /clean drops the pi
# session AND wipes the room's workspace completely (fresh pi session
# with no files left behind).

def clean_session_and_workspace(cwd):
    """Drop this room's pi sessions and wipe the non-empty workspace dir.
    Returns the reply to post in the room."""
    from .pi import wipe_sessions  # local import: pi depends on room
    ws = os.path.join(cwd, "workspace")
    had_ws = os.path.isdir(ws) and bool(os.listdir(ws))
    removed = wipe_sessions(ws)
    if os.path.isdir(ws):
        shutil.rmtree(ws, ignore_errors=True)
    if os.path.isfile(os.path.join(cwd, THINKING_FILE)):
        os.remove(os.path.join(cwd, THINKING_FILE))  # "reset my memory"
    cleared = bool(removed) or had_ws
    if cleared:
        extra = ", wiped the workspace" if had_ws else ""
        return (f"pi session cleared: removed {removed} pi session "
                f"file(s){extra}. The next message starts a fresh pi "
                "session.")
    return ("no pi session in this room and the workspace is empty \u2014 "
            "nothing to clear.")


# ---------------------------------------------------------- /thinking cmd --
# pi's last thinking is saved to the room folder by the worker after
# every run (tasks.py); /thinking (+ /t) posts it back to the room.

THINKING_FILE = "last-thinking.md"
THINKING_CMD_RE = re.compile(r"^/(thinking|t)$")


def save_thinking(cwd, text):
    """Atomically save pi's thinking for this room (full text)."""
    path = os.path.join(cwd, THINKING_FILE)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def load_thinking(cwd):
    """The last saved thinking for this room, or '' when there is none."""
    try:
        with open(os.path.join(cwd, THINKING_FILE), encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return ""


def quoted(text):
    """Block-quote for posting pi's thinking into the room."""
    return "\n".join("> " + ln if ln else ">"
                     for ln in text[:4000].splitlines())


def handle_thinking_command(message, cwd):
    """Handle /thinking (or /t) for this room. Returns the reply to post,
    or None when the message is not a /thinking command."""
    if not THINKING_CMD_RE.match(message):
        return None
    thinking = load_thinking(cwd)
    if not thinking:
        return ("No thinking saved in this room yet \u2014 it appears "
                "after the first pi run.")
    return "\U0001f914 " + quoted(thinking)


# --------------------------------------------------------- /mention cmd --
# When on, a message is only handled if it @mentions the bot first (the
# mention is stripped before pi sees it). Persisted in .room.json.

MENTION_CMD_RE = re.compile(r"^/(mention|m)\s+(on|off|status)\s*$")


def handle_mention_command(message, cwd):
    """Handle /mention on|off|status (or /m …). Returns the reply to
    post, or None when the message is not a /mention command."""
    m = MENTION_CMD_RE.match(message or "")
    if not m:
        return None
    mode = m.group(2)
    meta = room_meta_read(cwd)
    if mode == "status":
        return ("@mentions are "
                + ("required" if meta.get("mention_only") else "not "
                   "required")
                + " in this room. Use /mention on or /mention off to "
                  "change it.")
    meta["mention_only"] = (mode == "on")
    room_meta_write(cwd, meta)
    log.info("room %s: mention-only mode %s",
             os.path.basename(cwd), mode)
    return ("Only messages that @mention me are handled in this room "
            "now."
            if mode == "on"
            else "Mentions are no longer required in this room.")


# ------------------------------------------------------- bot lifecycle ----
# Talk sends dedicated ActivityPub events when this bot is added to or
# removed from a room (BotController enable/disable -> Join / Leave
# webhooks). They are scoped to this bot's webhook URL, so no name
# matching is needed: a Join here means "we were added to that room".

GREETING_FMT = (
    "Hi {room}, I'm {bot} \u2014 I run the pi coding agent in this room. "
    "Send /pw <nextcloud-user> <app-password> to enable Nextcloud file "
    "access. Use /thinking (or /t) to see my last reasoning, /clean "
    "(or /c) to reset my memory, and /help (or /h) for all commands."
)

HELP_TEXT = (
    "commands for this room:\n"
    "  /thinking (or /t) \u2014 post my last reasoning\n"
    "  /clean (or /c) \u2014 reset my memory and workspace\n"
    "  /stop (or /s) \u2014 abort the running pi run\n"
    "  /schedule list \u2014 show the scheduled tasks\n"
    "  /schedule rm <id> \u2014 remove one, e.g. /schedule rm at-news\n"
    "  /mention on | off | status (or /m on) \u2014 only react when "
    "@mentioned\n"
    "  /pw <user> <app-password> \u2014 enable file access\n"
    "  /version \u2014 show the pigeon version\n"
    "  /help (or /h) \u2014 this list"
)


def _meta_greeted(cwd):
    return bool(room_meta_read(cwd).get("greeted"))


def mark_greeted(cwd):
    meta = room_meta_read(cwd)
    meta["greeted"] = True
    room_meta_write(cwd, meta)


def handle_bot_added(bot, room_name, cwd):
    """Join webhook: post the one-time greeting. Returns the message to
    post, or None when this room was already greeted."""
    if _meta_greeted(cwd):
        return None
    mark_greeted(cwd)
    log.info("bot added to room %s", room_name or os.path.basename(cwd))
    return GREETING_FMT.format(room=room_name or "there", bot=bot)


def handle_bot_removed(cwd):
    """Leave webhook: total clean of the room (pi sessions + workspace +
    .room.json credentials + pending queue). Returns True."""
    wipe_room(cwd)
    return True


def wipe_room(cwd):
    """Remove the room folder AND its pi session files (which live in
    PI_CONFIG_DIR, outside the room dir)."""
    from .pi import wipe_sessions  # local import: pi depends on room
    wipe_sessions(os.path.join(cwd, "workspace"))
    if os.path.isfile(_state_path(cwd)):
        # drop pending queue + running job first, so a concurrent worker
        # does not resurrect the folder from its state file
        state_update(cwd, lambda s: s.update(pending=[], running=None))
    shutil.rmtree(cwd, ignore_errors=True)
    log.info("room wiped: %s", cwd)
