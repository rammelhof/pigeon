"""pigeon — Nextcloud Talk bot -> pi agent webhook server (KISS).

Endpoints:
  POST /webhook/<bot>  - Nextcloud Talk bot webhook (incoming chat messages)
  GET  /health         - health check + pi config status + task overview
  GET  /               - short info page

Behaviour:
  * Every incoming message is queued per bot+room (one worker thread per
    room, so pi sessions are never interleaved).
  * Each Talk room gets its own folder under BOTS_DIR/<bot>/rooms/, named
    after the room. Pi runs with that folder as working directory, so pi's
    per-directory sessions keep per-room conversation context.
  * Pi must be configured with its default config files (auth.json /
    models.json in PI_CODING_AGENT_DIR). When not configured, the webhook
    answers 503 and /health reports the error.
  * Pi is invoked with `--mode json`. The assistant's thinking and answer
    are extracted from the JSONL event stream and posted back to the chat.
  * A background heartbeat task checks all started tasks: it kills runs
    that exceed PI_TIMEOUT and flags stuck tasks. Failed runs are retried
    (pi --continue) up to MAX_RETRIES times.
"""

import json
import logging
import os
import queue
import re
import subprocess
import threading
import time

import requests
from flask import Flask, jsonify, request

# ---------------------------------------------------------------- config --

NEXTCLOUD_URL = os.environ.get("NEXTCLOUD_URL", "").rstrip("/")
# ONE token: verified in the webhook payload AND used as bearer token for
# all Talk API calls. Must match the token given to `occ talk:bot:install`.
NEXTCLOUD_BOT_TOKEN = os.environ.get("NEXTCLOUD_BOT_TOKEN", "")

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


# ------------------------------------------------------- nextcloud api ---

def _nc_headers():
    return {"Authorization": f"Bearer {NEXTCLOUD_BOT_TOKEN}"}


def _nc_get(path):
    r = requests.get(f"{NEXTCLOUD_URL}/ocs/v2.php{path}",
                     headers=_nc_headers(), timeout=30)
    r.raise_for_status()
    return r.json()["ocs"]["data"]


def get_room_name(room_id):
    try:
        data = _nc_get(f"/apps/sparkle/api/v1/room/{room_id}")
        return (data.get("name") or "").strip()
    except Exception as e:
        log.warning("room name lookup failed for %s: %s", room_id, e)
        return None


def post_message(room_id, message):
    r = requests.post(
        f"{NEXTCLOUD_URL}/ocs/v2.php/apps/sparkle/api/v1/chat/{room_id}",
        data={"message": message},
        headers=_nc_headers(), timeout=30)
    if r.status_code >= 400:
        raise RuntimeError(f"post to chat failed: {r.status_code} {r.text[:200]}")


def folder_for_room(room_id, room_name):
    """Sanitised room name, fallback to numeric room id."""
    clean = re.sub(r"[^a-zA-Z0-9_-]+", "-", room_name or "").strip("-").lower()
    return clean or f"room-{room_id}"


# ------------------------------------------------------------- pi run ----

def pi_config_status():
    """Pi is configured iff its default config files exist (auth.json from
    /login, models.json for custom models/providers)."""
    for name in ("auth.json", "models.json"):
        path = os.path.join(PI_CONFIG_DIR, name)
        if os.path.isfile(path) and os.path.getsize(path) > 0:
            return True, path
    return False, PI_CONFIG_DIR


def pi_command(prompt, continue_session):
    cmd = [PI_BIN, "--mode", "json"]
    if continue_session:
        cmd += ["--continue"]
    cmd.append(prompt)
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
    cmd = pi_command(task["message"], continue_session=task["retries"] > 0)
    log.info("task %s: running %s", task["id"], " ".join(cmd[:-1]))
    proc = subprocess.Popen(cmd, cwd=task["cwd"],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True)
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
        "created_at": time.time(),
        "started_at": time.time(),
        "proc": None,
        "error": None,
        "_thinking": [],
        "_last_message_end": {},
    }
    with _lock:
        _tasks[task["id"]] = task

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
                post_message(job["room_id"], f"\U0001f914 {quoted}")
            post_message(job["room_id"], answer)
            task["status"] = "done"
            break
    except Exception as e:
        log.error("task %s failed permanently: %s", task["id"], e)
        task["status"] = "failed"
        task["error"] = str(e)
        try:
            post_message(job["room_id"],
                         f"\u26a0\ufe0f pi run failed: {e}"[:2000])
        except Exception as post_err:
            log.error("could not post failure notice: %s", post_err)
    finally:
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
            run_job(key, bot, job)
        except Exception as e:  # safety net, run_job already posts
            log.error("worker for %s crashed: %s", key, e)
        finally:
            q.task_done()


def enqueue(key, bot, job):
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
      worker thread then unblocks and retries (pi --continue) or fails.
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
    return jsonify({"service": "pigeon",
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
        "pi": {"configured": pi_ok, "config_file": pi_config_file},
        "heartbeat": hb,
        "stats": stats,
        "tasks": tasks,
    }), (200 if ok else 503)


@app.post("/webhook/<bot>")
def webhook(bot):
    if not BOT_NAME_RE.match(bot) or ".." in bot:
        return jsonify({"error": "invalid bot name"}), 400
    if not NEXTCLOUD_BOT_TOKEN:
        return jsonify({"error": "NEXTCLOUD_BOT_TOKEN not set"}), 503

    payload = request.get_json(silent=True) or {}
    if payload.get("token") != NEXTCLOUD_BOT_TOKEN:
        return jsonify({"error": "bad token"}), 403

    configured, config_file = pi_config_status()
    if not configured:
        log.error("pi is not configured (no %s in %s)",
                  "auth.json or models.json", PI_CONFIG_DIR)
        return jsonify({"error": "pi is not configured — run `pi` in the "
                                 "container once and /login, or add a "
                                 f"models.json to {config_file}"}), 503

    message = (payload.get("message")
               or payload.get("command") or "").strip()
    room_id = payload.get("room_id")
    if not message or not room_id:
        return jsonify({"ignored": not message or not room_id}), 200

    room_name = get_room_name(room_id)
    folder = folder_for_room(room_id, room_name)
    cwd = os.path.join(BOTS_DIR, bot, "rooms", folder)
    os.makedirs(cwd, exist_ok=True)

    key = f"{bot}/{folder}"
    enqueue(key, bot, {
        "room_id": room_id,
        "message": message,
        "sender": payload.get("sender_display_name")
                  or payload.get("sender") or "user",
        "cwd": cwd,
    })
    log.info("queued message from %s in %s",
             payload.get("sender_display_name"), key)
    return jsonify({"queued": True, "bot": bot, "room_folder": folder}), 202


if __name__ == "__main__":
    os.makedirs(BOTS_DIR, exist_ok=True)
    t = threading.Thread(target=_heartbeat_loop, daemon=True, name="heartbeat")
    t.start()
    port = int(os.environ.get("PORT", "8080"))
    log.info("listening on :%d, bots in %s, pi config in %s",
             port, BOTS_DIR, PI_CONFIG_DIR)
    app.run(host="0.0.0.0", port=port)
