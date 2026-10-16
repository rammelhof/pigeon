"""Sections 12 + 14 + 15: the scheduler. Due entries fire as room messages
and next_run advances; rooms without a token are skipped; time windows and
start_time anchors are pure-function checks with fixed timestamps
(deterministic).
"""
import json
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from pigeon.scheduler import advance_next_run, initial_next_run
import nc_stub
from harness import BOTS_TEST, check, tasks, wait_room_done


def run():
    # 12. scheduler: due entries in schedules.json fire as room messages,
    # next_run advances; entries without a room token or invalid ones are
    # skipped
    sched_dir = os.path.join(BOTS_TEST, "bot6", "rooms", "news-room")
    os.makedirs(sched_dir)
    with open(os.path.join(sched_dir, ".room.json"), "w") as f:
        json.dump({"token": "sched6ro"}, f)
    sched_file = os.path.join(sched_dir, "workspace", "schedules.json")
    os.makedirs(os.path.dirname(sched_file), exist_ok=True)
    with open(sched_file, "w") as f:
        json.dump({"schedules": [
            {"id": "at-news", "prompt": "Send the Austrian news brief.",
             "time": "09:00", "tz": "Europe/Vienna",
             "next_run": time.time() - 10},
            {"id": "interval-check", "prompt": "Interval check.",
             "every_minutes": 60, "next_run": time.time() - 10},
        ]}, f)
    # a room whose schedules are due but which never got a webhook message
    # (no .room.json -> no token) must NOT produce any post
    notok_dir = os.path.join(BOTS_TEST, "bot6", "rooms", "no-token")
    os.makedirs(os.path.join(notok_dir, "workspace"), exist_ok=True)
    with open(os.path.join(notok_dir, "workspace", "schedules.json"), "w") as f:
        json.dump({"schedules": [
            {"id": "no-token-room", "prompt": "Should never post.",
             "time": "08:00", "tz": "Europe/Vienna",
             "next_run": time.time() - 10},
        ]}, f)
    t = wait_room_done("bot6/rooms/news-room", timeout=20, min_count=2)
    check("scheduler fired due entries in the room",
          bool(t) and t["status"] == "done", str(t))
    posted_after = [m for room, m in nc_stub.H.received if room == "sched6ro"]
    t = wait_room_done("bot6/rooms/news-room", timeout=10, min_count=2)
    check("scheduler tasks replied in the room (signed bot API posts)",
          len(posted_after) >= 2, str(len(posted_after)))
    tasks_now = tasks()["tasks"]
    check("scheduler skips rooms without a room token (no task, no post)",
          not [x for x in tasks_now
               if "no-token" in x["room_cwd"].replace("\\", "/")],
          str(tasks_now))
    new_state = json.load(open(sched_file))
    by_id = {e["id"]: e for e in new_state["schedules"]}
    now = time.time()
    vienna = datetime.fromtimestamp(
        by_id["at-news"]["next_run"], ZoneInfo("Europe/Vienna"))
    check("daily next_run advanced to the next 09:00 in Europe/Vienna",
          by_id["at-news"]["next_run"] > now and vienna.hour == 9
          and vienna.minute == 0, str(by_id["at-news"]))
    check("interval next_run advanced by 60 minutes",
          by_id["interval-check"]["next_run"] > now
          and by_id["interval-check"]["next_run"] < now + 61 * 60,
          str(by_id["interval-check"]))

    t = wait_room_done("bot6/rooms/news-room", timeout=10, min_count=3)
    posted = [m for room, m in nc_stub.H.received if room == "sched6ro"]
    check("due entry not fired twice by later scans",
          len(posted) == len(posted_after), str(len(posted)))

    # 14. scheduler windows: interval entries are confined to a local time
    # window (fixed timestamps, deterministic)
    VIE = ZoneInfo("Europe/Vienna")
    win_entry = {"prompt": "x", "every_minutes": 60,
                 "window": {"start": "10:00", "end": "22:00",
                            "tz": "Europe/Vienna"}}

    def at(day, hh, mm):
        return int(datetime(2026, 10, day, hh, mm, tzinfo=VIE).timestamp())

    nxt = advance_next_run(win_entry, at(13, 21, 30))
    loc = datetime.fromtimestamp(nxt, VIE)
    check("window: run after the end snaps to the next day's window start",
          (loc.day, loc.hour, loc.minute) == (14, 10, 0), str(loc))
    nxt = advance_next_run(win_entry, at(13, 15, 0))
    loc = datetime.fromtimestamp(nxt, VIE)
    check("window: run inside the window advances by the interval",
          (loc.day, loc.hour, loc.minute) == (13, 16, 0), str(loc))
    nxt = initial_next_run(win_entry, at(13, 3, 0))
    loc = datetime.fromtimestamp(nxt, VIE)
    check("window: initial next run before the window is the window start",
          (loc.day, loc.hour, loc.minute) == (13, 10, 0), str(loc))
    nxt = initial_next_run(win_entry, at(13, 11, 0))
    loc = datetime.fromtimestamp(nxt, VIE)
    check("window: initial next run inside the window adds the interval",
          (loc.day, loc.hour, loc.minute) == (13, 12, 0), str(loc))
    no_win = {"prompt": "x", "every_minutes": 30}
    nxt = advance_next_run(no_win, at(13, 3, 0))
    check("no window: plain interval runs around the clock",
          nxt == at(13, 3, 0) + 30 * 60, str(nxt))

    # 15. scheduler start_time anchor: runs on the start_time+N*interval
    # phase
    anch = {"prompt": "x", "every_minutes": 15, "start_time": "10:30",
            "tz": "Europe/Vienna"}
    nxt = initial_next_run(anch, at(13, 10, 20))
    check("anchor: next run before the anchor is the anchor",
          (datetime.fromtimestamp(nxt, VIE).hour,
           datetime.fromtimestamp(nxt, VIE).minute) == (10, 30), str(nxt))
    nxt = advance_next_run(anch, at(13, 10, 31))
    check("anchor: run advances to the next phase step",
          (datetime.fromtimestamp(nxt, VIE).hour,
           datetime.fromtimestamp(nxt, VIE).minute) == (10, 45), str(nxt))
    nxt = advance_next_run(anch, at(13, 23, 59))
    loc = datetime.fromtimestamp(nxt, VIE)
    check("anchor: after end of day the phase continues next day",
          (loc.day, loc.hour, loc.minute) == (14, 10, 30), str(loc))
    anch_win = {"prompt": "x", "every_minutes": 70, "start_time": "10:00",
                "window": {"start": "10:00", "end": "12:00",
                           "tz": "Europe/Vienna"}}
    nxt = advance_next_run(anch_win, at(13, 10, 0))
    loc = datetime.fromtimestamp(nxt, VIE)
    check("anchor+window: in-window phase step is kept (10:00 -> 11:10)",
          (loc.day, loc.hour, loc.minute) == (13, 11, 10), str(loc))
    nxt = advance_next_run(anch_win, at(13, 11, 11))
    loc = datetime.fromtimestamp(nxt, VIE)
    check("anchor+window: out-of-window steps skipped "
          "(12:20 -> next day 10:00)",
          (loc.day, loc.hour, loc.minute) == (14, 10, 0), str(loc))
