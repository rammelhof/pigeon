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

from .config import BOTS_DIR, DEFAULT_MODE, MODES_DIR, log

BOT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
BOT_DIR_RE = re.compile(r"[a-z0-9][a-z0-9_-]*")
ROOM_DIR_RE = re.compile(r"[a-z0-9][a-z0-9_.-]*")

_lock = threading.Lock()


def folder_for_room(room_id, room_name):
    """Sanitised room name, fallback to the room token."""
    clean = re.sub(r"[^a-zA-Z0-9_-]+", "-", room_name or "").strip("-").lower()
    return clean or f"room-{room_id}"


def resolve_room_folder(bot, room, room_name):
    """Folder for a room, disambiguated by its token: two different
    Nextcloud rooms may share a display name (e.g. two 1:1s named
    "Team") — the second one then gets <name-slug>-<token prefix> so
    workspaces/sessions never mix. Existing rooms keep their plain
    name folder (the stored token still matches)."""
    base = folder_for_room(room, room_name)
    if room_token(room_dir(bot, base)) not in (None, room):
        return f"{base}-{room[:8]}"
    return base


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


# ------------------------------------------------------------- !pw cmd ----
# Stored per room (isolation between rooms); the command never reaches pi.

PW_SET_RE = re.compile(r"^!pw\s+(\S+)\s+(\S+)\s*$")
PW_STATUS_RE = re.compile(r"^!pw\s*$")


def handle_pw_command(message, cwd, room):
    """Handle !pw for this room. Returns the reply to post, or None when
    the message is not a !pw command."""
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
                "!pw <nextcloud-user> <app-password> to enable file "
                "downloads.")
    return None


# ------------------------------------------------------------ !clean cmd --
# Stored per room; the command never reaches pi. !clean drops the pi
# session AND wipes the room's workspace completely (fresh pi session
# with no files left behind).

def clean_session_and_workspace(cwd):
    """Drop this room's pi sessions and wipe the non-empty workspace dir.
    Returns the reply to post in the room. memory.md (long-term room
    memory, see the memory skill) survives the wipe."""
    from .pi import wipe_sessions  # local import: pi depends on room
    ws = os.path.join(cwd, "workspace")
    had_ws = os.path.isdir(ws) and \
        any(n != MEMORY_FILE for n in os.listdir(ws))
    removed = wipe_sessions(ws)
    if os.path.isdir(ws):
        for name in os.listdir(ws):
            if name == MEMORY_FILE:      # long-term memory survives
                continue
            p = os.path.join(ws, name)
            shutil.rmtree(p, ignore_errors=True) if os.path.isdir(p) \
                else os.remove(p)
    if os.path.isdir(ws) and not os.listdir(ws):
        os.rmdir(ws)                # no memory.md -> fully gone again
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


# ---------------------------------------------------------- !thinking cmd --
# pi's last thinking is saved to the room folder by the worker after
# every run (tasks.py); !thinking (+ !t) posts it back to the room.

THINKING_FILE = "last-thinking.md"
THINKING_CMD_RE = re.compile(r"^!(thinking|t)$")


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
    """Handle !thinking (or !t) for this room. Returns the reply to post,
    or None when the message is not a !thinking command."""
    if not THINKING_CMD_RE.match(message):
        return None
    thinking = load_thinking(cwd)
    if not thinking:
        return ("No thinking saved in this room yet \u2014 it appears "
                "after the first pi run.")
    return "\U0001f914 " + quoted(thinking)


# --------------------------------------------------------- !mention cmd --
# When on, a message is only handled if it @mentions the bot first (the
# mention is stripped before pi sees it). Persisted in .room.json.

MENTION_CMD_RE = re.compile(r"^!(mention|m)\s+(on|off|status)\s*$")

MENTION_HELP = (
    "mention mode \u2014 usage:\n"
    "  !mention on \u2014 only react to messages that start with\n"
    "    @<bot name> or @bot\n"
    "  !mention off \u2014 react to every message again\n"
    "  !mention status \u2014 show the current setting\n"
    "  (alias: !m on|off|status)"
)


def handle_mention_command(message, cwd):
    """Handle !mention on|off|status (or !m …). Returns the reply to
    post, or None when the message is not a !mention command."""
    m = MENTION_CMD_RE.match(message or "")
    if not m:
        if message in ("!mention", "!m"):
            return MENTION_HELP
        return None
    mode = m.group(2)
    meta = room_meta_read(cwd)
    names = [n for n in (meta.get("bot_name"), "bot") if n]
    mention = " or ".join("@" + n for n in names)
    if mode == "status":
        return ("@mentions are "
                + ("required" if meta.get("mention_only") else "not "
                   "required")
                + " in this room. Use !mention on or !mention off to "
                  "change it.")
    meta["mention_only"] = (mode == "on")
    room_meta_write(cwd, meta)
    log.info("room %s: mention-only mode %s",
             os.path.basename(cwd), mode)
    return ("Only messages starting with " + mention + " are handled in "
            "this room now."
            if mode == "on"
            else "Mentions are no longer required in this room.")


def strip_mention(message, bot_name):
    """If the message starts with a mention of this bot ("@<bot name>"
    or "@bot"), return the rest of the message (stripped); otherwise
    None. Case-insensitive, matched at a word boundary."""
    names = [n for n in (bot_name, "bot") if n]
    names.sort(key=len, reverse=True)  # longest first, no prefix matches
    m = re.match(r"^@(" + "|".join(re.escape(n) for n in names) +
                 r")\b(.*)$", message or "", re.S | re.I)
    if not m:
        return None
    return m.group(2).strip()


# ------------------------------------------------------------ persona ----
# A persona is one .md file in pigeon/modes/; its text is appended to
# pi's system prompt for every run of the room (pi's --append-system-
# prompt). "programmer" (no file) means pi's native prompt. A mode can
# carry a start prompt (<name>_start_prompt.md) that pigeon posts when
# the mode is entered or the session is cleared (it asks the user for
# context the persona needs, e.g. the finance country). DEFAULT_MODE is
# what new rooms use.
# The active mode lives in .room.json ("mode"), like the other room
# settings — it survives !clean, is gone with the room on bot leave.

MODE_NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,30}")
MODE_CMD_RE = re.compile(r"^!mode(?:\s+(\S+))?\s*$")
PROGRAMMER_MODE = "programmer"      # native pi prompt, no persona file
START_PROMPT_SUFFIX = "_start_prompt"


def list_modes():
    """Available personas: (name, description) from the .md files in
    MODES_DIR (start-prompt files excluded); the description is the
    file's first non-empty line."""
    out = []
    if os.path.isdir(MODES_DIR):
        for fn in sorted(os.listdir(MODES_DIR)):
            if not fn.endswith(".md"):
                continue
            name = fn[:-3]
            if not MODE_NAME_RE.fullmatch(name) or \
                    name.endswith(START_PROMPT_SUFFIX):
                continue
            try:
                with open(os.path.join(MODES_DIR, fn),
                          encoding="utf-8") as f:
                    desc = next((ln.strip() for ln in f if ln.strip()), "")
            except OSError:
                desc = ""
            out.append((name, desc))
    return out


def mode_names():
    """All settable mode names: programmer first, then the file modes."""
    return [PROGRAMMER_MODE] + [n for n, _ in list_modes()]


def room_mode(cwd):
    """The room's active persona; unset rooms use DEFAULT_MODE."""
    return room_meta_read(cwd).get("mode") or DEFAULT_MODE


def set_room_mode(cwd, mode):
    meta = room_meta_read(cwd)
    if mode == DEFAULT_MODE:
        meta.pop("mode", None)      # the default needs no entry
    else:
        meta["mode"] = mode
    room_meta_write(cwd, meta)
    log.info("room %s: persona mode %s", os.path.basename(cwd), mode)


def mode_text(mode):
    """The text to append to pi's system prompt for a mode, or None for
    programmer/empty/unknown names."""
    if not mode or not MODE_NAME_RE.fullmatch(mode):
        return None
    path = os.path.join(MODES_DIR, mode + ".md")
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as f:
        return f.read()


def mode_start_prompt(mode):
    """The mode's start-prompt text — an instruction appended to pi's
    system prompt telling it to ask for its setup details (country,
    language, ...) in the first reply of a fresh session — or None."""
    if not mode or not MODE_NAME_RE.fullmatch(mode):
        return None
    path = os.path.join(MODES_DIR, mode + START_PROMPT_SUFFIX + ".md")
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as f:
        return f.read().strip()


def handle_mode_command(message, cwd):
    """Handle !mode [name]: show or switch the room's persona. Returns
    the reply to post, or None when the message is not a !mode command.
    'default' is an alias for DEFAULT_MODE. Switching personas starts a
    fresh pi session (wipes the room's sessions, keeps the workspace) so
    that a mode's start prompt takes effect on the next message."""
    m = MODE_CMD_RE.match(message or "")
    if not m:
        return None
    arg = (m.group(1) or "list").lower()
    if arg == "default":
        arg = DEFAULT_MODE
    names = mode_names()
    if arg in ("list", "status"):
        cur = room_mode(cwd)
        lines = ["- {} \u2014 pi's native programmer prompt (no persona)"
                 .format(PROGRAMMER_MODE)]
        lines += ["- {}{} \u2014 {}".format(
                      n, " (default)" if n == DEFAULT_MODE else "", d)
                  for n, d in list_modes()]
        return ("modes:\n" + "\n".join(lines) +
                "\nthis room uses: {}".format(cur))
    if arg in names:
        cur = room_mode(cwd)
        if arg == cur:
            return "Mode: {} \u2014 already active.".format(cur)
        from .pi import wipe_sessions  # local import: pi depends on room
        removed = wipe_sessions(os.path.join(cwd, "workspace"))
        set_room_mode(cwd, arg)
        fresh = (" (fresh pi session: removed {} session file(s))"
                 .format(removed) if removed else "")
        reply = ("Mode: {} \u2014 pi's native programmer prompt.{}"
                 .format(PROGRAMMER_MODE, fresh)
                 if arg == PROGRAMMER_MODE else
                 "Mode: {} \u2014 the persona applies from the next "
                 "message.{}".format(arg, fresh))
        return reply
    return ("Unknown mode \u2018{}\u2019. Available: {}".format(
        arg, ", ".join(names)))


# ------------------------------------------------------------ memory cmd --
# Long-term room memory: pi maintains workspace/memory.md (memory
# skill); it survives !clean. !memory shows it to the user.

MEMORY_FILE = "memory.md"


def handle_memory_command(message, cwd):
    """Handle !memory: show the room's long-term memory. Returns the
    reply to post, or None when the message is not !memory."""
    if (message or "").strip() != "!memory":
        return None
    path = os.path.join(cwd, "workspace", MEMORY_FILE)
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read().strip()
    except OSError:        # no file yet = no entries yet
        text = ""
    if not text:
        return ("I have no memory entries yet \u2014 over time I note "
                "durable facts about you and this room in my memory "
                "(it survives !clean).")
    return "my memory for this room (survives !clean):\n" + text


def handle_status_command(message, cwd):
    """Handle !status: report whether pi is currently running in this
    room (for how long, on what message) or not. Returns the reply to
    post, or None when the message is not !status."""
    if (message or "").strip() != "!status":
        return None
    from .tasks import snapshot  # local import: tasks depends on room
    tasks_list, _, _ = snapshot()
    running = [t for t in tasks_list
               if t["room_cwd"] == cwd and t["status"] == "running"]
    mode = room_mode(cwd)
    if not running:
        return "pi is not running in this room (mode: {}).".format(mode)
    t = running[-1]
    words = " ".join((t.get("message") or "").split())
    preview = words[:40] + ("\u2026" if len(words) > 40 else "")
    return ("pi is running ({}s, retry {}) \u2014 \u201c{}\u201d "
            "(mode: {})".format(t["age_seconds"], t["retries"], preview,
                                mode))


# ------------------------------------------------------- bot lifecycle ----
# Talk sends dedicated ActivityPub events when this bot is added to or
# removed from a room (BotController enable/disable -> Join / Leave
# webhooks). They are scoped to this bot's webhook URL, so no name
# matching is needed: a Join here means "we were added to that room".

GREETING_FMT = (
    "Hi {room}, I'm {bot} \u2014 I run the pi coding agent in this room. "
    "Send !pw <nextcloud-user> <app-password> to enable Nextcloud file "
    "access. Mention me as @{bot} or @bot to get my attention, use "
    "!thinking (or !t) to see my last reasoning, !clean (or !c) to "
    "reset my memory, and !help (or !h) for all commands."
)

HELP_TEXT = (
    "commands for this room:\n"
    "  !thinking (or !t) \u2014 post my last reasoning\n"
    "  !clean (or !c) \u2014 reset my memory and workspace\n"
    "  !stop (or !s) \u2014 abort the running pi run\n"
    "  !schedule (or !sch) list \u2014 show the scheduled tasks\n"
    "  !schedule rm <id> \u2014 remove one, e.g. !schedule rm at-news\n"
    "  !mention on|off|status (or !m \u2026) \u2014 only react when "
    "    mentioned ({mention}); bare !mention / !m shows usage\n"
    "  !mode [name] \u2014 switch persona (default = office assistant, "
    "office, finance, trader, programmer); bare !mode lists the modes\n"
    "  !status \u2014 show whether pi is running right now\n"
    "  !memory \u2014 show my long-term memory for this room "
    "(survives !clean)\n"
    "  !pw <user> <app-password> \u2014 enable file access\n"
    "  !version (or !v) \u2014 show the pigeon version\n"
    "  !help (or !h) \u2014 this list"
)


def _meta_greeted(cwd):
    return bool(room_meta_read(cwd).get("greeted"))


def mark_greeted(cwd):
    meta = room_meta_read(cwd)
    meta["greeted"] = True
    room_meta_write(cwd, meta)


def handle_bot_added(bot, room_name, cwd):
    """Join webhook: post the one-time greeting and remember the bot's
    Talk name (for the mention gate). Returns the message to post, or
    None when this room was already greeted."""
    if bot:
        meta = room_meta_read(cwd)
        if meta.get("bot_name") != bot:
            meta["bot_name"] = bot
            room_meta_write(cwd, meta)
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
