"""Sections 10-11: restart recovery. Jobs pending + running in a room's
.pigeon.json are re-queued by requeue_stale() at startup (old-format
entries without a room token are dropped); a stale running job on disk
from a crash is picked up again by the periodic state scan.
"""
import json
import os
import time

from pigeon.tasks import requeue_stale
from harness import BOTS_TEST, check, tasks, wait_room_done


def run():
    # 10. restart: interrupted (running) + pending jobs are re-queued and run
    resume_dir = os.path.join(BOTS_TEST, "bot4", "rooms", "resumed-room")
    os.makedirs(resume_dir)
    with open(os.path.join(resume_dir, ".pigeon.json"), "w") as f:
        json.dump({
            "pending": [{"message": "second one", "sender": "x",
                         "room": "resu4med", "at": time.time()},
                        # old-version format: no "room" token -> must be
                        # dropped
                        {"message": "old format no room", "sender": "x",
                         "at": time.time()}],
            "running": {"message": "interrupted one", "sender": "x",
                        "room": "resu4med", "started_at": time.time(),
                        "retries": 1, "task_id": "old-1"},
        }, f)
    resumed = requeue_stale()
    t = wait_room_done("bot4/rooms/resumed-room", min_count=2)
    mine = [x for x in tasks()["tasks"]
            if "resumed-room" in x["room_cwd"].replace("\\", "/")]
    check("interrupted + pending jobs resumed after restart",
          resumed == 2 and bool(t) and t["status"] == "done",
          f"{resumed} {t}")
    check("old-format job without room token dropped (no bogus post)",
          len(mine) == 2, str(mine))

    # 11. heartbeat self-healing: a stale on-disk job (unknown task, e.g.
    # after a crash) is re-queued by the periodic state scan
    stale_dir = os.path.join(BOTS_TEST, "bot5", "rooms", "stale-room")
    os.makedirs(stale_dir)
    with open(os.path.join(stale_dir, ".pigeon.json"), "w") as f:
        json.dump({"pending": [],
                   "running": {"message": "stale on disk", "sender": "x",
                               "room": "stale5ro", "started_at": time.time(),
                               "retries": 0, "task_id": "dead-999"}}, f)
    t = wait_room_done("bot5/rooms/stale-room", timeout=15)
    check("heartbeat re-queues stale disk job (crash recovery)",
          bool(t) and t["status"] == "done", str(t))
    state = json.load(open(os.path.join(resume_dir, ".pigeon.json")))
    check("state file clean after resume",
          state.get("pending") == [] and state.get("running") is None,
          str(state))
