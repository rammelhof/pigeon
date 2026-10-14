"""pigeon — Nextcloud Talk bot -> pi agent webhook server (KISS).

Endpoints:
  POST /webhook/<bot>  - Nextcloud Talk bot webhook (incoming chat messages)
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

import hashlib
import hmac
import json
import logging
import os
import queue
import re
import shutil
import signal
import subprocess
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from flask import Flask, jsonify, request

# ---------------------------------------------------------------- config --

NEXTCLOUD_URL = os.environ.get("NEXTCLOUD_URL", "").rstrip("/")

# build/version info (PIGEON_COMMIT is set by the Dockerfile build arg)
VERSION = "1.0.0"
COMMIT = os.environ.get("PIGEON_COMMIT", "unknown")
# Secret given to `occ talk:bot:install <name> <secret> <url> ...`
# (40-128 chars). Verifies incoming webhooks and signs outgoing API calls.
TALK_BOT_SECRET = os.environ.get("TALK_BOT_SECRET", "")

BOTS_DIR = os.environ.get("BOTS_DIR", "./bots")
PI_BIN = os.environ.get("PI_BIN", "pi")
PI_CONFIG_DIR = os.environ.get(
    "PI_CODING_AGENT_DIR",
    os.path.join(os.path.expanduser("~"), ".pi", "agent"))

PI_TIMEOUT = float(os.environ.get("PI_TIMEOUT", "600"))           # seconds
HEARTBEAT_INTERVAL = float(os.environ.get("HEARTBEAT_INTERVAL", "30"))
# scan state files for stale jobs every Nth heartbeat (crash self-healing)
STATE_SCAN_EVERY = int(os.environ.get("STATE_SCAN_EVERY", "10"))
# how often the scheduler checks schedules.json files (seconds)
SCHEDULER_INTERVAL = float(os.environ.get("SCHEDULER_INTERVAL", "30"))
MAX_RETRIES = int(os.environ.get("MAX_RETRIES", "1"))
# Incoming-message batching: after a chat message, the room worker waits up
# to this many seconds for more messages from the same room and sends the
# whole burst to pi in a single run (0 = off, immediate). Scheduled
# prompts are never delayed/batched.
MESSAGE_BATCH_WINDOW = float(os.environ.get("MESSAGE_BATCH_WINDOW", "5"))
# Run every pi invocation in a bubblewrap sandbox: read-only root, only
# the room's workspace/ and PI_CODING_AGENT_DIR writable, fresh PID/IPC/
# UTS namespaces, full network access (0 = off). There is NO fallback:
# if SANDBOX is on but bwrap is missing or cannot start, pi runs fail.
SANDBOX = os.environ.get("SANDBOX", "1") == "1"
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
# max size in MB of file attachments downloaded into the room's
# workspace/chat/files/
MAX_FILE_SIZE_MB = float(os.environ.get("MAX_FILE_SIZE_MB", "100"))

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
log = logging.getLogger("pigeon")

app = Flask(__name__)

# ------------------------------------------------------------- state -----

_lock = threading.Lock()
_tasks = {}          # task id -> task dict
_room_queues = {}    # "<bot>/<room>" -> queue.Queue
_room_workers = {}   # "<bot>/<room>" -> threading.Thread
_stats = {"started_at": time.time(), "completed": 0, "failed": 0, "retries": 0}
_heartbeat = {"last_run": None}
_heartbeat_ticks = 0
_task_counter = 0
_active_jobs = set()   # job ids currently tracked in memory (queued/running)

BOT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


# ------------------------------------------------------------- security --

def verify_webhook_signature(secret, random_str, signature, raw_body):
    expected = hmac.new(secret.encode(),
                        ((random_str or "") + raw_body).encode(),
                        hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature or "")


def sign_request(secret, body):
    """Headers for bot API calls (random + HMAC-SHA256(random+body, secret))."""
    random_str = os.urandom(32).hex()
    sig = hmac.new(secret.encode(),
                   (random_str + body).encode(), hashlib.sha256).hexdigest()
    return {
        "X-Nextcloud-Talk-Bot-Random": random_str,
        "X-Nextcloud-Talk-Bot-Signature": sig,
        "OCS-APIRequest": "true",
        "Content-Type": "application/json",
    }


# ------------------------------------------------------- nextcloud api ---

def post_message(room, message):
    """Post a chat message via the signed Talk bot API
    (https://nextcloud-talk.readthedocs.io/en/latest/bots/).
    room must be the conversation token."""
    url = (f"{NEXTCLOUD_URL}/ocs/v2.php/apps/spreed/api/v1"
           f"/bot/{room}/message")
    headers = sign_request(TALK_BOT_SECRET, message)  # sign raw message
    r = requests.post(url, data=json.dumps({"message": message}).encode(),
                      headers=headers, timeout=30)
    if r.status_code >= 400:
        raise RuntimeError(f"post to chat failed: {r.status_code} {r.text[:200]}")


# --------------------------------------------- file attachments ----------
# Talk sends files as a {file} rich-object placeholder. The "link" in the
# parameter is a Talk virtual link (not downloadable), but the file id is a
# real file id: pigeon resolves it to a WebDAV path via SEARCH
# (basicsearch on oc:fileid) and downloads it with plain GET + Basic auth
# (the room's app password) into the room's workspace/chat/files/ folder — no
# direct API, no temp share, no folder iteration.
# Without a room app password, pi is instructed to tell the user (in the
# user's language) that the password for file access is missing.

def _fmt_size(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def _payload_file_param(payload):
    """The file rich-object parameter of a webhook payload, or None."""
    try:
        content = json.loads(
            (payload.get("object") or {}).get("content") or "{}")
        f = (content.get("parameters") or {}).get("file")
        return f if isinstance(f, dict) else None
    except Exception:
        return None


def _room_file_creds(cwd):
    """(user, password) for this room from .room.json, or None."""
    meta = _room_meta_read(cwd)
    user, pw = meta.get("nextcloud_user"), meta.get("nextcloud_password")
    if user and pw:
        return user, pw
    return None


def _dav_file_url(user, pw, file_id):
    """WebDAV SEARCH (basicsearch on oc:fileid) -> absolute download URL.
    Single request, no temp share, no folder iteration."""
    uid = str(user).split("@", 1)[0]
    file_id = int(file_id)
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<d:searchrequest xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">'
        "<d:basicsearch><d:select><d:prop><d:displayname/><oc:fileid/>"
        "</d:prop></d:select><d:from><d:scope><d:href>/files/"
        + uid + "</d:href><d:depth>infinity</d:depth></d:scope></d:from>"
        "<d:where><d:eq><d:prop><oc:fileid/></d:prop>"
        f"<d:literal>{file_id}</d:literal></d:eq></d:where><d:orderby/>"
        "</d:basicsearch></d:searchrequest>")
    r = requests.request("SEARCH", f"{NEXTCLOUD_URL}/remote.php/dav",
                         auth=(user, pw), data=xml.encode(),
                         headers={"Content-Type": "application/xml",
                                  "Depth": "0"}, timeout=30)
    if r.status_code not in (200, 207):
        raise RuntimeError(f"dav search: HTTP {r.status_code}")
    m = re.search(r"<d:href>([^<]+)</d:href>", r.text)
    if not m:
        raise RuntimeError(f"file id {file_id} not found")
    href = m.group(1)
    if not href.startswith("/"):
        href = "/" + href
    return NEXTCLOUD_URL + href


def _save_download(dl_url, name, dest_dir, auth=None):
    """Stream dl_url into dest_dir/<ts>-<name>. Returns (basename, bytes)."""
    name = re.sub(r"[^A-Za-z0-9._-]+", "_",
                  os.path.basename(name or "file")).strip("._") or "file"
    name = name[:100]
    os.makedirs(dest_dir, exist_ok=True)
    stamp = int(time.time())
    dest = os.path.join(dest_dir, f"{stamp}-{name}")
    i, dot = 1, name.rfind(".")
    while os.path.exists(dest):
        base, ext = (name[:dot], name[dot:]) if dot > 0 else (name, "")
        dest = os.path.join(dest_dir, f"{stamp}-{base}_{i}{ext}")
        i += 1
    cap = int(MAX_FILE_SIZE_MB * 1024 * 1024)
    size, tmp = 0, dest + ".part"
    try:
        with requests.get(dl_url, auth=auth, stream=True,
                          timeout=(10, 600)) as r:
            if r.status_code >= 400:
                raise RuntimeError(f"download: HTTP {r.status_code}")
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(1 << 16):
                    size += len(chunk)
                    if size > cap:
                        raise RuntimeError(
                            f"file exceeds {MAX_FILE_SIZE_MB:.0f} MB cap")
                    f.write(chunk)
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
    os.replace(tmp, dest)
    return os.path.basename(dest), size


def _apply_file_param(message, f, cwd):
    """Download the file from the webhook's file parameter into the room's
    chat/files/ and put its path in front of the message
    ("chat/files/<name> <message>"). Without a room app password, pi is
    instructed to tell the user (in the user's language) that the password
    for file access is missing. Download failures yield a hint with the
    share link so the bot can still react."""
    name = f.get("name") or "file"
    mime = f.get("mimetype") or "unknown type"
    link = f.get("link") or ""
    creds = _room_file_creds(cwd)
    if creds is None:
        log.info("file %s: no app password configured for this room — "
                 "instructing pi", name)
        return ("The user just sent a file ("
                f"{name}, {mime}"
                + (f"; share: {link}" if link else "")
                + "), but this room has no app password configured, so the "
                "server could not download it. Reply to the user — in the "
                "same language they are using — that the app password for "
                "file access is missing, and tell them to enable it by "
                "sending: /pw <nextcloud-user> <app-password>")
    try:
        file_id = int(f.get("id"))
    except (TypeError, ValueError):
        file_id = None
    if file_id is None:
        return (f"[file: {name} ({mime})] — download failed (no file id)"
                + (f"; share link: {link}" if link else ""))
    try:
        dl = _dav_file_url(*creds, file_id)
        saved, size = _save_download(dl, name,
                                     os.path.join(cwd, "workspace", "chat",
                                                  "files"),
                                     auth=creds)
        log.info("downloaded file %s -> chat/files/%s (%s)",
                 name, saved, _fmt_size(size))
        hint = f"chat/files/{saved}"
        if "{file}" in message:
            rest = re.sub(r"\s+", " ",
                          message.replace("{file}", " ")).strip()
            return (f"{hint} {rest}" if rest else hint)
        return f"{hint} {message.strip()}".strip()
    except Exception as e:
        log.warning("file download failed: %s", e)
        return (f"[file: {name} ({mime})] — download failed ({e})"
                + (f"; share link: {link}" if link else ""))


# ---------------------------------------------------- /pw room command ----
# Stored per room (isolation between rooms); the command never reaches pi.

PW_SET_RE = re.compile(r"^/pw\s+(\S+)\s+(\S+)\s*$")
PW_STATUS_RE = re.compile(r"^/pw\s*$")


def _handle_pw_command(message, cwd, room):
    """Handle /pw for this room. Returns the reply to post, or None when
    the message is not a /pw command."""
    m = PW_SET_RE.match(message)
    if m:
        meta = _room_meta_read(cwd)
        meta["nextcloud_user"], meta["nextcloud_password"] = m.group(1), \
            m.group(2)
        _room_meta_write(cwd, meta)
        log.info("room %s: app password stored for user %s",
                 os.path.basename(cwd), m.group(1))
        return ("App password saved for file access in this room "
                f"(user: {m.group(1)}).")
    if PW_STATUS_RE.match(message):
        user = _room_file_creds(cwd)
        if user:
            return f"File access is configured for user: {user[0]}."
        return ("File access is not configured for this room. Send "
                "/pw <nextcloud-user> <app-password> to enable file "
                "downloads.")
    return None


def folder_for_room(room_id, room_name):
    """Sanitised room name, fallback to the room token."""
    clean = re.sub(r"[^a-zA-Z0-9_-]+", "-", room_name or "").strip("-").lower()
    return clean or f"room-{room_id}"


# ------------------------------------------------- queue persistence ------

def _state_path(cwd):
    return os.path.join(cwd, ".pigeon.json")


def _state_read(cwd):
    try:
        with open(_state_path(cwd)) as f:
            state = json.load(f)
    except Exception:
        return {"pending": [], "running": None}
    state.setdefault("pending", [])
    state.setdefault("running", None)
    return state


def _state_update(cwd, fn):
    """Load state, apply fn(state), write it back atomically."""
    with _lock:
        state = _state_read(cwd)
        fn(state)
        tmp = _state_path(cwd) + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f)
        os.replace(tmp, _state_path(cwd))
        return state


def _mark_running(cwd, task):
    def upd(state):
        state["running"] = {
            "message": task["message"],
            "sender": task["sender"],
            "room": task["room"],
            "started_at": task["started_at"],
            "retries": task["retries"],
            "task_id": task["id"],
        }
    _state_update(cwd, upd)


def _clear_running(cwd, task_id):
    def upd(state):
        if state.get("running") and state["running"].get("task_id") == task_id:
            state["running"] = None
    _state_update(cwd, upd)


def id_counter():
    global _task_counter
    with _lock:
        _task_counter += 1
        return _task_counter


# --------------------------------------------------------- scheduler ------
# Rooms can define schedules in <room dir>/workspace/schedules.json (managed
# by pi
# via the "scheduler" skill):
#   {"schedules": [
#      {"id": "at-news", "prompt": "...", "time": "09:00", "tz": "Europe/Vienna"},
#      {"id": "check",   "prompt": "...", "every_minutes": 15},
#      {"id": "standup", "prompt": "...", "every_minutes": 60,
#       "window": {"start": "10:00", "end": "22:00", "tz": "Europe/Vienna"}}
#   ]}
# When an entry is due, pigeon sends its prompt to the room as a normal
# message (same pipeline as a chat message, same pi session).
# next_run is maintained by pigeon and persisted into the file.

_scheduler = {"last_run": None}


def _room_token(cwd):
    """Conversation token of the room (written by the webhook)."""
    try:
        with open(os.path.join(cwd, ".room.json")) as f:
            return (json.load(f) or {}).get("token")
    except Exception:
        return None


# ------------------------------------------------- room metadata ----------
# .room.json holds the room token (written on every webhook, needed for
# scheduled tasks) and the optional per-room Nextcloud app password
# (used to download file attachments; never leaves this room).

def _room_meta_path(cwd):
    return os.path.join(cwd, ".room.json")


def _room_meta_read(cwd):
    try:
        with open(_room_meta_path(cwd)) as f:
            meta = json.load(f)
        return meta if isinstance(meta, dict) else {}
    except Exception:
        return {}


def _room_meta_write(cwd, meta):
    path = _room_meta_path(cwd)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(meta, f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _write_room_token(cwd, token):
    meta = _room_meta_read(cwd)
    if meta.get("token") == str(token):
        return
    meta["token"] = str(token)
    _room_meta_write(cwd, meta)


def _next_daily(entry, now):
    """Next occurrence of the entry's local time HH:MM in its IANA tz."""
    try:
        zone = ZoneInfo(entry.get("tz") or "UTC")
        hh, mm = (entry["time"] or "00:00").split(":")
        local = datetime.fromtimestamp(now, zone)
        cand = local.replace(hour=int(hh), minute=int(mm),
                             second=0, microsecond=0)
        if cand <= local:
            cand += timedelta(days=1)
        return cand.timestamp()
    except Exception as e:
        log.warning("scheduler: bad daily entry %r: %s", entry, e)
        return None


def _sched_kind_ok(entry):
    if (entry.get("time") or "").strip():
        return True
    every = entry.get("every_minutes")
    return isinstance(every, (int, float)) and not isinstance(every, bool) \
        and every > 0


# optional time window for interval schedules:
#   {"start": "10:00", "end": "22:00", "tz": "Europe/Vienna"}
# (runs are skipped outside the window and snap to the next window start)

def _window_parts(entry):
    """(ZoneInfo, start_min, end_min) or None if no valid window."""
    w = entry.get("window")
    if not isinstance(w, dict):
        return None
    try:
        sh, sm = (w.get("start") or "0:00").split(":")
        eh, em = (w.get("end") or "0:00").split(":")
        s_min = int(sh) * 60 + int(sm)
        e_min = int(eh) * 60 + int(em)
    except Exception:
        return None
    if s_min >= e_min:  # wrap-around windows are not supported
        return None
    try:
        zone = ZoneInfo(w.get("tz") or "UTC")
    except Exception:
        return None
    return zone, s_min, e_min


def _window_minutes(entry, ts):
    """Minutes since local midnight of ts in the window's zone, or None."""
    parts = _window_parts(entry)
    if parts is None:
        return None
    loc = datetime.fromtimestamp(ts, parts[0])
    return loc.hour * 60 + loc.minute


def _in_window(entry, ts):
    parts = _window_parts(entry)
    if parts is None:
        return True
    cur = _window_minutes(entry, ts)
    return parts[1] <= cur < parts[2]


def _next_window_start(entry, ts):
    parts = _window_parts(entry)
    if parts is None:
        return ts
    zone, s_min, _ = parts
    loc = datetime.fromtimestamp(ts, zone)
    cand = loc.replace(hour=s_min // 60, minute=s_min % 60,
                       second=0, microsecond=0)
    if loc.hour * 60 + loc.minute >= s_min:
        cand += timedelta(days=1)
    return cand.timestamp()


# optional phase anchor for interval schedules: "start_time": "10:30"
# runs then happen at 10:30, 10:30+I, 10:30+2I, ... (wall clock) in the
# entry's timezone — the next run is fully predictable and stays stable
# across restarts, instead of drifting as now+interval.

def _entry_tz(entry):
    """Timezone of an interval entry: window.tz, else tz, else UTC."""
    w = entry.get("window")
    if isinstance(w, dict) and (w.get("tz") or "").strip():
        return w["tz"]
    return (entry.get("tz") or "").strip() or "UTC"


def _anchor_minutes(entry):
    """start_time as minutes since local midnight, or None."""
    st = (entry.get("start_time") or "").strip()
    if not st:
        return None
    try:
        hh, mm = st.split(":")
        a = int(hh) * 60 + int(mm)
        return a if 0 <= a < 1440 else None
    except Exception:
        return None


def _anchored_next(entry, now):
    """Next run strictly after now on the phase start_time + N*interval
    (wall clock in the entry's tz; the phase restarts at start_time each
    day), skipping candidates outside the window (if any)."""
    every = int(entry["every_minutes"])  # _sched_kind_ok checked by caller
    anchor = _anchor_minutes(entry)
    try:
        zone = ZoneInfo(_entry_tz(entry))
    except Exception:
        return now + every * 60
    now_dt = datetime.fromtimestamp(now, zone)
    mid0 = now_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    day_len = 1440
    try:  # DST days have 23/25 h; keep the arithmetic wall-clock safe
        day_len = int((mid0 + timedelta(days=1)).timestamp()
                      - mid0.timestamp()) // 60
    except Exception:
        pass

    def positions():
        """All phase positions of one day (minutes since midnight):
        anchor + k*every, arithmetically (no cumulative drift)."""
        p, k = [], 0
        while True:
            pos = anchor + k * every
            if pos >= day_len:
                return p  # next day's phase is handled by the d=1 pass
            p.append(pos)
            k += 1

    for d in (0, 1):
        mid = mid0 if d == 0 else mid0 + timedelta(days=1)
        for pos in positions():
            cand = mid + timedelta(minutes=pos)
            if cand <= now_dt:  # 'now' may have seconds -> strictly after
                continue
            if _in_window(entry, cand.timestamp()):
                return cand.timestamp()
    return now + every * 60


def _initial_next_run(entry, now):
    if (entry.get("time") or "").strip():
        return _next_daily(entry, now)
    every = entry.get("every_minutes")
    if _sched_kind_ok(entry):
        if _anchor_minutes(entry) is not None:
            return _anchored_next(entry, now)
        if not _in_window(entry, now):
            return _next_window_start(entry, now)
        return now + every * 60
    return None


def _advance_next_run(entry, now):
    if (entry.get("time") or "").strip():
        return _next_daily(entry, now)
    if _sched_kind_ok(entry):
        if _anchor_minutes(entry) is not None:
            return _anchored_next(entry, now)
        nxt = now + entry["every_minutes"] * 60
        if not _in_window(entry, nxt):
            nxt = _next_window_start(entry, nxt)
        return nxt
    return None


def _scheduler_tick():
    now = time.time()
    if not os.path.isdir(BOTS_DIR):
        return
    for bot in sorted(os.listdir(BOTS_DIR)):
        rooms_dir = os.path.join(BOTS_DIR, bot, "rooms")
        if not os.path.isdir(rooms_dir):
            continue
        for folder in sorted(os.listdir(rooms_dir)):
            cwd = os.path.join(rooms_dir, folder)
            path = os.path.join(cwd, "workspace", "schedules.json")
            if not os.path.isdir(cwd) or not os.path.exists(path):
                continue
            try:
                with open(path) as f:
                    data = json.load(f)
            except Exception as e:
                log.warning("scheduler: %s/%s: unreadable schedules.json: %s",
                            bot, folder, e)
                continue
            schedules = (data or {}).get("schedules")
            if not isinstance(schedules, list):
                continue
            due = []
            changed = False
            for entry in schedules:
                if not isinstance(entry, dict) or \
                        not (entry.get("prompt") or "").strip():
                    continue
                if entry.get("next_run") is None:
                    entry["next_run"] = _initial_next_run(entry, now)
                    changed = True
                if entry.get("next_run") is not None and \
                        entry["next_run"] <= now:
                    due.append(entry)
            if not due and not changed:
                continue
            # advance due entries and persist BEFORE enqueueing (so a crash
            # right after can never double-fire a schedule)
            for entry in due:
                entry["next_run"] = _advance_next_run(entry, now)
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, path)
            for entry in due:
                token = _room_token(cwd)
                if not token:
                    log.warning("scheduler: %s/%s: entry %r due but no room "
                                "token yet (.room.json) — skipped",
                                bot, folder, entry.get("id"))
                    continue
                enqueue(f"{bot}/{folder}", bot, {
                    "room": token, "room_id": folder,
                    "message": entry["prompt"], "sender": "scheduler",
                    "cwd": cwd,
                    "workspace": os.path.join(cwd, "workspace"),
                })
                log.info("scheduler: fired %s/%s entry %r: %r",
                         bot, folder, entry.get("id"),
                         (entry.get("prompt") or "")[:60])


def _scheduler_loop():
    while True:
        _scheduler["last_run"] = time.time()
        try:
            _scheduler_tick()
        except Exception as e:
            log.warning("scheduler tick failed: %s", e)
        time.sleep(SCHEDULER_INTERVAL)


# ------------------------------------------------------------ skills ------
# Bundled skills live in the image (SKILLS_SRC). At startup they are copied
# into pi's user skills dir ($PI_CODING_AGENT_DIR/skills) so they are part
# of the persistent agent configuration; bundled skills are updated on
# every start, user skills (other folders) are left untouched.

SKILLS_SRC = os.environ.get("SKILLS_SRC", "/app/skills")


def sync_skills(src=SKILLS_SRC, dest=None):
    """Copy bundled skill folders into dest. Returns number synced."""
    if dest is None:
        dest = os.path.join(PI_CONFIG_DIR, "skills")
    if not os.path.isdir(src):
        return 0
    os.makedirs(dest, exist_ok=True)
    n = 0
    for name in sorted(os.listdir(src)):
        s = os.path.join(src, name)
        d = os.path.join(dest, name)
        if not os.path.isdir(s) or \
                not os.path.exists(os.path.join(s, "SKILL.md")):
            continue
        if os.path.isdir(d):
            shutil.rmtree(d)
        shutil.copytree(s, d)
        n += 1
    return n


# ------------------------------------------------------------- pi run ----

def pi_config_status():
    """Pi is configured iff its default config files exist (auth.json from
    /login, models.json for custom models/providers)."""
    for name in ("auth.json", "models.json"):
        path = os.path.join(PI_CONFIG_DIR, name)
        if os.path.isfile(path) and os.path.getsize(path) > 0:
            return True, path
    return False, PI_CONFIG_DIR


def pi_command(prompt, session_id, cwd):
    """Always run in the room's stable pi session: --session-id opens it
    or creates it (first message of a new room). The working directory is
    the room's workspace/ subfolder; when SANDBOX is on and bwrap exists,
    the whole run is wrapped in a bubblewrap sandbox (full network)."""
    cmd = [PI_BIN, "--mode", "json", "--session-id", session_id, prompt]
    ws = os.path.join(cwd, "workspace")
    if SANDBOX:
        bwrap = shutil.which("bwrap")
        if bwrap is None:
            raise RuntimeError("SANDBOX=1 but bwrap not found — install "
                               "bubblewrap or set SANDBOX=0")
        # The sandbox root is a fresh tmpfs that only contains what is
        # bound here — so in addition to the read-only tool belt, the
        # merged-usr root symlinks must be recreated inside, or the
        # dynamic linker (/lib64/...) is invisible and every exec fails.
        cmd = [bwrap,
               "--ro-bind", "/usr", "/usr",
               "--ro-bind", "/etc", "/etc",
               "--ro-bind", "/var", "/var",
               "--ro-bind", "/run", "/run",      # Docker DNS: /run/resolv.conf
               "--ro-bind", "/sys", "/sys",
               # merged-usr shims (in the container / these are symlinks)
               "--symlink", "usr/lib64", "/lib64",
               "--symlink", "usr/lib", "/lib",
               "--symlink", "usr/bin", "/bin",
               "--symlink", "usr/sbin", "/sbin",
               # mask sensitive paths FIRST (empty, private to this
               # sandbox): other rooms incl. their .room.json, pigeon's
               # code. Ops are applied in order, so the writable binds
               # below pierce the /bots mask at exactly the workspace
               "--tmpfs", BOTS_DIR,
               "--tmpfs", "/app",
               # writable: this room's workspace + the pi home (sessions,
               # skills, model credentials)
               "--bind", ws, ws,
               "--bind", PI_CONFIG_DIR, PI_CONFIG_DIR,
               # inherit NO environment (TALK_BOT_SECRET lives in
               # gunicorn's) — set only what pi needs; HOME in the
               # workspace keeps the room self-contained
               "--clearenv",
               "--setenv", "PATH", "/usr/local/bin:/usr/bin:/bin",
               "--setenv", "HOME", ws,
               "--setenv", "LANG", "C.UTF-8",
               # fresh tmp/dev/proc (pid namespace: /proc shows pi only)
               "--tmpfs", "/tmp",
               "--dev", "/dev",
               "--proc", "/proc",
               # new PID/IPC/UTS namespaces, FULL network
               "--unshare-pid",
               "--unshare-ipc",
               "--unshare-uts",
               "--die-with-parent",
               "--chdir", ws] + cmd
    return cmd


def extract_answer_and_thinking(task):
    """Answer = text blocks of the LAST assistant message_end event.
    Thinking was collected from the event stream into task['_thinking']."""
    message = task["_last_message_end"].get("message", {})
    thinking = "\n\n".join(task["_thinking"]).strip()
    text_parts = []
    for block in message.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text" \
                and block.get("text"):
            text_parts.append(block["text"])
    answer = "\n\n".join(text_parts).strip()
    return answer, thinking


def run_pi_once(task):
    """Start one pi run, consume the JSONL stream, return (answer, thinking)."""
    cmd = pi_command(task["message"], task["session_id"], task["cwd"])
    log.info("task %s: running %s", task["id"], " ".join(cmd[:-1]))
    proc = subprocess.Popen(cmd, cwd=task["workspace"],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, start_new_session=True)
    task["proc"] = proc
    task["started_at"] = time.time()

    for line in proc.stdout:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        etype = event.get("type")
        if etype == "message_end":
            msg = event.get("message", {})
            if msg.get("role") == "assistant":
                task["_last_message_end"] = event
                for block in msg.get("content") or []:
                    if (isinstance(block, dict)
                            and block.get("type") == "thinking"
                            and block.get("thinking")):
                        task["_thinking"].append(block["thinking"])
    proc.wait()
    stderr = (proc.stderr.read() or "").strip()
    if proc.returncode != 0:
        raise RuntimeError(f"pi exited with {proc.returncode}: {stderr[:500]}")
    task["proc"] = None
    return extract_answer_and_thinking(task)


def requeue_stale():
    """Scan the state files and re-queue entries that are NOT tracked in
    memory anymore (at startup: memory is empty, so every valid entry is
    stale; in a heartbeat: only leftovers of a crash). Live entries are
    recognised by job_id (pending) and task_id (running) and left alone.
    Entries without a conversation token (old versions) are dropped.
    Returns number re-queued."""
    requeued = 0
    if not os.path.isdir(BOTS_DIR):
        return 0
    with _lock:
        active = set(_active_jobs)
        known_tasks = set(_tasks)
    for bot in sorted(os.listdir(BOTS_DIR)):
        rooms_dir = os.path.join(BOTS_DIR, bot, "rooms")
        if not os.path.isdir(rooms_dir):
            continue
        for folder in sorted(os.listdir(rooms_dir)):
            cwd = os.path.join(rooms_dir, folder)
            if not os.path.isdir(cwd):
                continue
            state = _state_read(cwd)
            stale_pending = [it for it in state["pending"]
                             if it.get("job_id") not in active]
            running = state.get("running")
            stale_running = bool(running) and \
                running.get("task_id") not in known_tasks
            if not stale_pending and not stale_running:
                continue

            def upd(s):
                s["pending"] = [it for it in s["pending"]
                                if it.get("job_id") in active]
                if stale_running:
                    s["running"] = None
            _state_update(cwd, upd)
            entries = ([running] if stale_running else []) + stale_pending
            for item in entries:
                message = (item.get("message") or "").strip()
                room = item.get("room")
                if not message:
                    continue
                if not room:
                    log.error("%s/%s: dropping job %r — no conversation "
                              "token in state file (written by an old "
                              "pigeon version); result could not be posted",
                              bot, folder, message[:50])
                    continue
                key = f"{bot}/{folder}"
                enqueue(key, bot, {
                    "room": room, "room_id": folder, "message": message,
                    "sender": item.get("sender") or "user", "cwd": cwd,
                    "workspace": os.path.join(cwd, "workspace"),
                })
                requeued += 1
                log.warning("re-queued stale job %s/%s: %r (room=%s)",
                            bot, folder, message[:50], room)
    return requeued


def run_job(key, bot, job):
    """Worker: run one queued message through pi, post results, retry on
    failure (up to MAX_RETRIES)."""
    task = {
        "id": f"{int(time.time())}-{job['room_id']}-{id_counter()}",
        "bot": bot,
        "room_id": job["room_id"],
        "room": job["room"],
        "cwd": job["cwd"],
        "message": job["message"],
        "sender": job["sender"],
        "status": "running",
        "retries": 0,
        "job_id": job["job_id"],
        "job_ids": job.get("job_ids") or [job["job_id"]],
        "workspace": job["workspace"],
        "session_id": f"{bot}_{os.path.basename(job['cwd'])}",
        "created_at": time.time(),
        "started_at": time.time(),
        "proc": None,
        "error": None,
        "_thinking": [],
        "_last_message_end": {},
    }
    with _lock:
        _tasks[task["id"]] = task
    os.makedirs(job["workspace"], exist_ok=True)
    _mark_running(job["cwd"], task)

    try:
        while True:
            try:
                answer, thinking = run_pi_once(task)
            except Exception as e:
                task["error"] = str(e)
                if task["retries"] >= MAX_RETRIES:
                    raise
                task["retries"] += 1
                _stats["retries"] += 1
                task["status"] = "retrying"
                _mark_running(job["cwd"], task)
                log.warning("task %s failed (%s), retry %d",
                            task["id"], e, task["retries"])
                time.sleep(2)
                continue

            if not answer.strip():
                raise RuntimeError("pi produced no answer")

            # post thinking (as a quote) and then the answer
            if thinking:
                quoted = "\n".join(
                    "> " + ln if ln else ">"
                    for ln in thinking[:4000].splitlines())
                post_message(job["room"], f"\U0001f914 {quoted}")
            post_message(job["room"], answer)
            task["status"] = "done"
            break
    except Exception as e:
        log.error("task %s failed permanently: %s", task["id"], e)
        task["status"] = "failed"
        task["error"] = str(e)
        try:
            post_message(job["room"],
                         f"\u26a0\ufe0f pi run failed: {e}"[:2000])
        except Exception as post_err:
            log.error("could not post failure notice: %s", post_err)
    finally:
        _clear_running(job["cwd"], task["id"])
        with _lock:
            for jid in task.get("job_ids", [task["job_id"]]):
                _active_jobs.discard(jid)
        with _lock:
            if task["status"] == "done":
                _stats["completed"] += 1
            else:
                _stats["failed"] += 1
        log.info("task %s finished: %s", task["id"], task["status"])


def _room_worker(key, bot, q):
    while True:
        first = q.get()
        if first is None:
            return
        batch = [first]
        stop = False
        # batch window: collect more chat messages for this room, then run
        # pi once for the whole burst
        if first.get("batch") and MESSAGE_BATCH_WINDOW > 0:
            while True:
                try:
                    nxt = q.get(timeout=MESSAGE_BATCH_WINDOW)
                except queue.Empty:
                    break
                if nxt is None:
                    stop = True
                    break
                batch.append(nxt)
        if len(batch) == 1:
            job = batch[0]
        else:
            job = dict(batch[0])
            job["job_ids"] = [e["job_id"] for e in batch]
            job["message"] = "\n".join(
                f"[{e.get('sender', 'user')}] {e.get('message', '')}"
                for e in batch)
            log.info("%s: %d messages within %.0fs window — one pi run",
                     key, len(batch), MESSAGE_BATCH_WINDOW)
        try:
            for e in batch:
                jid = e["job_id"]

                def pop_job(s, jid=jid):
                    s["pending"] = [it for it in s["pending"]
                                    if it.get("job_id") != jid]

                _state_update(job["cwd"], pop_job)
            run_job(key, bot, job)
        except Exception as e:  # safety net, run_job already posts
            log.error("worker for %s crashed: %s", key, e)
        finally:
            for _ in batch:
                q.task_done()
        if stop:
            return


def enqueue(key, bot, job):
    """Persist the job to the room state file, then hand it to the queue."""
    job["job_id"] = id_counter()
    item = {"job_id": job["job_id"], "message": job["message"],
            "sender": job.get("sender", "user"),
            "room": job["room"], "at": time.time()}

    def upd(state):
        state["pending"].append(item)
    _state_update(job["cwd"], upd)
    with _lock:
        _active_jobs.add(job["job_id"])
        q = _room_queues.get(key)
        if q is None:
            q = queue.Queue()
            _room_queues[key] = q
            t = threading.Thread(target=_room_worker, args=(key, bot, q),
                                 daemon=True, name=f"room-{key}")
            _room_workers[key] = t
            t.start()
    q.put(job)


# ------------------------------------------------------------- heartbeat --

def _heartbeat_loop():
    """Check all started tasks:
    * running task + live process past PI_TIMEOUT  -> kill process; the
      worker thread then unblocks and reruns in the same room session or fails.
    * running task + no process stuck past timeout -> mark failed
      (e.g. hang while posting results).
    Every STATE_SCAN_EVERYth tick also scans the state files and re-queues
    jobs that are on disk but no longer tracked in memory (crash recovery).
    Retries are counted by the worker only, so MAX_RETRIES semantics stay
    consistent."""
    while True:
        now = time.time()
        with _lock:
            for task in _tasks.values():
                if task["status"] != "running":
                    continue
                elapsed = now - task["started_at"]
                proc = task["proc"]
                if proc is not None:
                    if elapsed > PI_TIMEOUT:
                        log.warning("task %s exceeded PI_TIMEOUT (%.0fs), killing",
                                    task["id"], PI_TIMEOUT)
                        try:
                            proc.kill()
                        except Exception:
                            pass
                elif elapsed > PI_TIMEOUT + 30:
                    log.error("task %s stuck without pi process, marking failed",
                              task["id"])
                    task["status"] = "failed"
                    task["error"] = task["error"] or "stuck after pi run"
        _heartbeat["last_run"] = now
        global _heartbeat_ticks
        _heartbeat_ticks += 1
        if _heartbeat_ticks % STATE_SCAN_EVERY == 0:
            try:
                requeued = requeue_stale()
                if requeued:
                    log.warning("state scan re-queued %d stale job(s)", requeued)
            except Exception as e:
                log.warning("state scan failed: %s", e)
        time.sleep(HEARTBEAT_INTERVAL)


# ------------------------------------------------------------- endpoints --

@app.get("/")
def index():
    return jsonify({"service": "pigeon", "version": VERSION,
                    "commit": COMMIT,
                    "endpoints": ["/webhook/<bot>", "/health"]})


@app.get("/health")
def health():
    pi_ok, pi_config_file = pi_config_status()
    with _lock:
        tasks = [
            {
                "id": t["id"],
                "bot": t["bot"],
                "room_cwd": t["cwd"],
                "status": t["status"],
                "retries": t["retries"],
                "age_seconds": round(time.time() - t["created_at"], 1),
                "process_alive": bool(t["proc"] and t["proc"].poll() is None),
                "error": t["error"],
            }
            for t in _tasks.values()
        ]
        stats = dict(_stats)
        hb = dict(_heartbeat)
    stats["uptime_seconds"] = round(time.time() - stats["started_at"], 1)
    heartbeat_ok = bool(hb["last_run"] and time.time() - hb["last_run"]
                        < HEARTBEAT_INTERVAL * 3)
    stats_ok = stats["uptime_seconds"] < 60 or heartbeat_ok
    ok = pi_ok and stats_ok
    return jsonify({
        "status": "ok" if ok else "degraded",
        "version": VERSION,
        "commit": COMMIT,
        "pi": {"configured": pi_ok, "config_file": pi_config_file},
        "heartbeat": hb,
        "scheduler": dict(_scheduler),
        "stats": stats,
        "tasks": tasks,
    }), (200 if ok else 503)


def _parse_payload(payload):
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

    parsed = _parse_payload(payload)
    if parsed is None:
        return jsonify({"ignored": "not a chat message"}), 200
    message, room, room_name, sender = parsed
    if not message:
        return jsonify({"ignored": "empty message"}), 200

    folder = folder_for_room(room, room_name)
    cwd = os.path.join(BOTS_DIR, bot, "rooms", folder)
    os.makedirs(cwd, exist_ok=True)
    _write_room_token(cwd, room)  # needed for scheduled tasks to reply

    # room command: app password for file access (never reaches pi)
    reply = _handle_pw_command(message, cwd, room)
    if reply is not None:
        post_message(room, reply)
        return jsonify({"handled": "/pw"}), 200

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
    f = _payload_file_param(payload)
    if f is not None or "{file}" in message:
        message = (_apply_file_param(message, f, cwd) if f
                   else message.replace("{file}",
                                        "[file: no file data in payload]"))

    key = f"{bot}/{folder}"
    enqueue(key, bot, {
        "room": room,
        "room_id": str(room),
        "message": message,
        "sender": sender,
        "cwd": cwd,
        "workspace": os.path.join(cwd, "workspace"),
        "batch": True,
    })
    log.info("queued message from %s in %s", sender, key)
    return jsonify({"queued": True, "bot": bot, "room_folder": folder}), 202


def _shutdown(signum, frame):
    """Kill active pi process groups and exit so docker stop is fast."""
    log.info("received %s, stopping active pi runs", signum)
    with _lock:
        procs = [t["proc"] for t in _tasks.values()
                 if t["proc"] is not None and t["proc"].poll() is None]
    for p in procs:
        try:
            if hasattr(os, "killpg"):  # Linux/Docker: kill whole group
                os.killpg(os.getpgid(p.pid), signal.SIGKILL)
            else:
                p.kill()
        except Exception:
            try:
                p.kill()
            except Exception:
                pass
    os._exit(128 + signum)


_started = False


def startup():
    """One-time init: signal handlers, dirs, resume from state files,
    heartbeat. Runs once per process — called from gunicorn's post_fork
    (wsgi.py) or __main__ below."""
    global _started
    if _started:
        return
    _started = True
    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    os.makedirs(BOTS_DIR, exist_ok=True)
    synced = sync_skills()
    if synced:
        log.info("synced %d bundled skill(s) into %s", synced,
                 os.path.join(PI_CONFIG_DIR, "skills"))
    resumed = requeue_stale()
    if resumed:
        log.info("resumed %d job(s) from before the restart", resumed)
    t = threading.Thread(target=_heartbeat_loop, daemon=True, name="heartbeat")
    t.start()
    t = threading.Thread(target=_scheduler_loop, daemon=True, name="scheduler")
    t.start()
    log.info("pigeon %s (%s) ready, bots in %s, pi config in %s",
             VERSION, COMMIT, BOTS_DIR, PI_CONFIG_DIR)


if __name__ == "__main__":
    startup()
    port = int(os.environ.get("PORT", "8080"))
    log.info("listening on :%d (flask dev server — use gunicorn in docker)",
             port)
    app.run(host="0.0.0.0", port=port)
