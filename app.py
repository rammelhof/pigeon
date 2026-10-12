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
    after the room. Pi runs with that folder as working directory, so pi's
    per-directory sessions keep per-room conversation context.
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
import signal
import subprocess
import threading
import time

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
MAX_RETRIES = int(os.environ.get("MAX_RETRIES", "1"))
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

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
_task_counter = 0

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


def restart_requeue():
    """At startup: re-queue interrupted (running) and pending jobs that
    survived in the state files. Rooms always run in their stable pi
    session (--session-id), so context survives automatically.
    Jobs without a stored conversation token (state files written by an
    old version) are dropped: the result could not be posted anyway.
    Returns number re-queued."""
    requeued = 0
    if not os.path.isdir(BOTS_DIR):
        return 0
    for bot in sorted(os.listdir(BOTS_DIR)):
        rooms_dir = os.path.join(BOTS_DIR, bot, "rooms")
        if not os.path.isdir(rooms_dir):
            continue
        for folder in sorted(os.listdir(rooms_dir)):
            cwd = os.path.join(rooms_dir, folder)
            if not os.path.isdir(cwd):
                continue
            state = _state_read(cwd)
            entries = []
            running = state.get("running")
            if running:
                entries.append(running)
            entries.extend(state["pending"])
            if not entries:
                continue
            _state_update(cwd, lambda s: s.update(pending=[], running=None))
            for item in entries:
                message = (item.get("message") or "").strip()
                room = item.get("room")
                if not message:
                    continue
                if not room:
                    log.error("%s/%s: dropping job %r — no conversation token "
                              "in state file (written by an old pigeon "
                              "version); result could not be posted",
                              bot, folder, message[:50])
                    continue
                key = f"{bot}/{folder}"
                enqueue(key, bot, {
                    "room": room, "room_id": folder, "message": message,
                    "sender": item.get("sender") or "user", "cwd": cwd,
                })
                requeued += 1
                log.info("re-queued %s/%s: %r (room=%s)", bot, folder,
                         message[:50], room)
    return requeued


# ------------------------------------------------------------- pi run ----

def pi_config_status():
    """Pi is configured iff its default config files exist (auth.json from
    /login, models.json for custom models/providers)."""
    for name in ("auth.json", "models.json"):
        path = os.path.join(PI_CONFIG_DIR, name)
        if os.path.isfile(path) and os.path.getsize(path) > 0:
            return True, path
    return False, PI_CONFIG_DIR


def pi_command(prompt, session_id):
    """Always run in the room's stable pi session: --session-id opens it
    or creates it (first message of a new room)."""
    return [PI_BIN, "--mode", "json", "--session-id", session_id, prompt]


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
    cmd = pi_command(task["message"], task["session_id"])
    log.info("task %s: running %s", task["id"], " ".join(cmd[:-1]))
    proc = subprocess.Popen(cmd, cwd=task["cwd"],
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


def id_counter():
    global _task_counter
    with _lock:
        _task_counter += 1
        return _task_counter


def run_job(key, bot, job):
    """Worker: run one queued message through pi, post results, retry on
    failure (up to MAX_RETRIES)."""
    task = {
        "id": f"{int(time.time())}-{job['room_id']}-{id_counter()}",
        "bot": bot,
        "room_id": job["room_id"],
        "cwd": job["cwd"],
        "message": job["message"],
        "sender": job["sender"],
        "status": "running",
        "retries": 0,
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
            if task["status"] == "done":
                _stats["completed"] += 1
            else:
                _stats["failed"] += 1
        log.info("task %s finished: %s", task["id"], task["status"])


def _room_worker(key, bot, q):
    while True:
        job = q.get()
        if job is None:
            return
        try:
            _state_update(job["cwd"],
                          lambda s: s["pending"].pop(0) if s["pending"] else None)
            run_job(key, bot, job)
        except Exception as e:  # safety net, run_job already posts
            log.error("worker for %s crashed: %s", key, e)
        finally:
            q.task_done()


def enqueue(key, bot, job):
    """Persist the job to the room state file, then hand it to the queue."""
    item = {"message": job["message"], "sender": job.get("sender", "user"),
            "room": job["room"], "at": time.time()}

    def upd(state):
        state["pending"].append(item)
    _state_update(job["cwd"], upd)
    with _lock:
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

    configured, config_file = pi_config_status()
    if not configured:
        log.error("pi is not configured (no %s in %s)",
                  "auth.json or models.json", PI_CONFIG_DIR)
        return jsonify({"error": "pi is not configured — run `pi` in the "
                                 "container once and /login, or add a "
                                 f"models.json to {config_file}"}), 503

    folder = folder_for_room(room, room_name)
    cwd = os.path.join(BOTS_DIR, bot, "rooms", folder)
    os.makedirs(cwd, exist_ok=True)

    key = f"{bot}/{folder}"
    enqueue(key, bot, {
        "room": room,
        "room_id": str(room),
        "message": message,
        "sender": sender,
        "cwd": cwd,
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
    resumed = restart_requeue()
    if resumed:
        log.info("resumed %d job(s) from before the restart", resumed)
    t = threading.Thread(target=_heartbeat_loop, daemon=True, name="heartbeat")
    t.start()
    log.info("pigeon %s (%s) ready, bots in %s, pi config in %s",
             VERSION, COMMIT, BOTS_DIR, PI_CONFIG_DIR)


if __name__ == "__main__":
    startup()
    port = int(os.environ.get("PORT", "8080"))
    log.info("listening on :%d (flask dev server — use gunicorn in docker)",
             port)
    app.run(host="0.0.0.0", port=port)
