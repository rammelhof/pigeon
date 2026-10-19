"""Job pipeline: per-room queues, worker threads, retries, heartbeat.

One worker thread per room, so pi sessions are never interleaved. The
queue is persisted in .pigeon.json (see room.py); requeue_stale()
rebuilds it from disk after a restart/crash. The heartbeat kills pi
runs that exceed PI_TIMEOUT, flags stuck tasks and periodically scans
the state files for leftovers of a crash.
"""

import os
import queue
import signal
import threading
import time

from .config import (BOTS_DIR, HEARTBEAT_INTERVAL, MAX_RETRIES,
                     MESSAGE_BATCH_WINDOW, PI_TIMEOUT,
                     STATE_SCAN_EVERY, log)
from .nextcloud import post_message
from .pi import run_pi_once
from .room import (clear_running, mark_running, save_thinking,
                   state_read, state_update)

_lock = threading.Lock()
_tasks = {}          # task id -> task dict
_room_queues = {}    # "<bot>/<room>" -> queue.Queue
_room_workers = {}   # "<bot>/<room>" -> threading.Thread
_stats = {"started_at": time.time(), "completed": 0, "failed": 0,
          "retries": 0}
_heartbeat = {"last_run": None}
_heartbeat_ticks = 0
_task_counter = 0
_active_jobs = set()   # job ids currently tracked in memory (queued/running)


def _indicators(job):
    """Progress indicators attached to this job — normally at most one:
    only the LAST message of a burst is reacted (one notification, not
    one per message)."""
    return [i for i in (job.get("indicators") or []) if i is not None]


def stop_running(cwd):
    """Kill the running pi process of this room and make the worker end
    quietly (no retry, no error message, the robot reaction is removed).
    True when a run was stopped, False when nothing was running."""
    running = state_read(cwd).get("running") or {}
    task = _tasks.get(running.get("task_id"))
    if task is None or task["status"] not in ("running", "retrying"):
        return False
    task["stopped"] = True
    proc = task["proc"]
    if proc is not None:
        try:
            if hasattr(os, "killpg"):  # Linux/Docker: kill whole group
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            else:
                proc.kill()
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    log.info("task %s: stop requested for %s", task["id"], cwd)
    return True


def id_counter():
    global _task_counter
    with _lock:
        _task_counter += 1
        return _task_counter


def enqueue(key, bot, job):
    """Persist the job to the room state file, then hand it to the queue."""
    job["job_id"] = id_counter()
    item = {"job_id": job["job_id"], "message": job["message"],
            "sender": job.get("sender", "user"),
            "room": job["room"], "at": time.time()}

    def upd(state):
        state["pending"].append(item)
    state_update(job["cwd"], upd)
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
    mark_running(job["cwd"], task)

    # pi's thinking is saved to the room folder for the /thinking
    # command — it is never posted to the room on its own; only the
    # robot reaction (start) and the answer (result) reach the chat.
    # live progress indicator: one bot reaction on the (last) user
    # message, set when pi starts. The reaction is kept after the run
    # finishes — reactions are only ever added, never removed (any
    # change would notify the room) — except on /stop.
    inds = _indicators(job)

    def ind(name, *args):
        for i in inds:
            getattr(i, name)(*args)

    try:
        while True:
            if task.get("stopped"):
                task["status"] = "stopped"
                break  # /stop: end quietly (no post, no retry)
            try:
                ind("started")
                answer, thinking = run_pi_once(task)
            except Exception as e:
                task["error"] = str(e)
                if task.get("stopped"):
                    task["status"] = "stopped"
                    break  # /stop: end quietly (no post, no retry)
                if task["retries"] >= MAX_RETRIES:
                    raise
                task["retries"] += 1
                _stats["retries"] += 1
                task["status"] = "retrying"
                mark_running(job["cwd"], task)
                log.warning("task %s failed (%s), retry %d",
                            task["id"], e, task["retries"])
                time.sleep(2)
                continue

            if not answer.strip():
                raise RuntimeError("pi produced no answer")

            # shut the indicator thread down (the reaction stays), save
            # the thinking for /thinking, then post the answer
            for i in inds:
                i.stop()
            if thinking:
                save_thinking(job["cwd"], thinking)
            post_message(job["room"], answer)
            task["status"] = "done"
            break
    except Exception as e:
        if task.get("stopped"):
            # /stop: the run was aborted by the user — not a failure,
            # nothing is posted
            task["status"] = "stopped"
            log.info("task %s stopped by user", task["id"])
        else:
            log.error("task %s failed permanently: %s", task["id"], e)
            task["status"] = "failed"
            task["error"] = str(e)
            try:
                post_message(job["room"],
                             f"\u26a0\ufe0f pi run failed: {e}"[:2000])
            except Exception as post_err:
                log.error("could not post failure notice: %s", post_err)
    finally:
        for i in inds:
            if task.get("stopped"):
                i.clear()  # the user ended the run — drop the robot
            i.stop()
        clear_running(job["cwd"], task["id"])
        with _lock:
            for jid in task.get("job_ids", [task["job_id"]]):
                _active_jobs.discard(jid)
        with _lock:
            if task["status"] == "done":
                _stats["completed"] += 1
            elif task["status"] == "failed":
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
        # only the LAST message of the burst carries the reaction — one
        # notification instead of one per message (the earlier ones never
        # react, their threads just shut down)
        inds = [e["indicator"] for e in batch
                if e.get("indicator") is not None]
        job["indicators"] = inds[-1:]
        for i in inds[:-1]:
            i.stop()
        try:
            for e in batch:
                jid = e["job_id"]

                def pop_job(s, jid=jid):
                    s["pending"] = [it for it in s["pending"]
                                    if it.get("job_id") != jid]

                state_update(job["cwd"], pop_job)
            run_job(key, bot, job)
        except Exception as e:  # safety net, run_job already posts
            log.error("worker for %s crashed: %s", key, e)
        finally:
            for _ in batch:
                q.task_done()
        if stop:
            return


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
            state = state_read(cwd)
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
            state_update(cwd, upd)
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
                enqueue(f"{bot}/{folder}", bot, {
                    "room": room, "room_id": folder, "message": message,
                    "sender": item.get("sender") or "user", "cwd": cwd,
                    "workspace": os.path.join(cwd, "workspace"),
                })
                requeued += 1
                log.warning("re-queued stale job %s/%s: %r (room=%s)",
                            bot, folder, message[:50], room)
    return requeued


def heartbeat_loop():
    """Check all started tasks:
    * running task + live process past PI_TIMEOUT  -> kill process; the
      worker thread then unblocks and reruns in the same room session or fails.
    * running task + no process stuck past timeout -> mark failed
      (e.g. hang while posting results).
    Every STATE_SCAN_EVERYth tick also scans the state files and re-queues
    jobs that are on disk but no longer tracked in memory (crash recovery).
    Retries are counted by the worker only, so MAX_RETRIES semantics stay
    consistent."""
    global _heartbeat_ticks
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
                        log.warning(
                            "task %s exceeded PI_TIMEOUT (%.0fs), killing",
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
        _heartbeat_ticks += 1
        if _heartbeat_ticks % STATE_SCAN_EVERY == 0:
            try:
                requeued = requeue_stale()
                if requeued:
                    log.warning("state scan re-queued %d stale job(s)",
                                requeued)
            except Exception as e:
                log.warning("state scan failed: %s", e)
        time.sleep(HEARTBEAT_INTERVAL)


def kill_active():
    """Kill the process groups of all live pi runs (fast shutdown)."""
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


def snapshot():
    """(tasks, stats, heartbeat) for /health — taken under the lock."""
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
        heartbeat = dict(_heartbeat)
    return tasks, stats, heartbeat
