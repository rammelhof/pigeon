"""Scheduled room prompts from <room dir>/workspace/schedules.json
(managed by pi via the "scheduler" skill):

  {"schedules": [
     {"id": "at-news", "prompt": "...", "time": "09:00", "tz": "Europe/Vienna"},
     {"id": "check",   "prompt": "...", "every_minutes": 15},
     {"id": "standup", "prompt": "...", "every_minutes": 60,
      "weekdays": [0, 1, 2, 3, 4],
      "window": {"start": "10:00", "end": "22:00", "tz": "Europe/Vienna"}}
  ]}

weekdays limits an entry to certain days of the week (0 = Monday …
6 = Sunday, e.g. [0, 1, 2, 3, 4] = Mon–Fri); candidates on other days
are skipped.

When an entry is due, pigeon sends its prompt to the room as a normal
message (same pipeline as a chat message, same pi session).
next_run is maintained by pigeon and persisted into the file.
"""

import json
import os
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .config import BOTS_DIR, SCHEDULER_INTERVAL, log
from .room import room_dir, room_token
from .tasks import enqueue

_scheduler = {"last_run": None}


def state():
    return dict(_scheduler)


# ------------------------------------------------------- time math --------

# optional weekday restriction: "weekdays": [0, 1, 2, 3, 4] (0 = Monday …
# 6 = Sunday). Entries without it run every day; entries on excluded
# days are skipped and the next run snaps to the next allowed day.

def _allowed_weekdays(entry):
    """Set of allowed weekday numbers (0 = Mon … 6 = Sun), or None = all."""
    wds = entry.get("weekdays")
    if not isinstance(wds, (list, tuple)):
        return None
    try:
        wds = {int(w) for w in wds}
    except (TypeError, ValueError):
        return None
    if not wds or any(w < 0 or w > 6 for w in wds):
        return None
    return wds


def _weekday_ok(entry, dt):
    """Is the (tz-aware) datetime on an allowed weekday for this entry?"""
    allowed = _allowed_weekdays(entry)
    return allowed is None or dt.weekday() in allowed


def _next_daily(entry, now):
    """Next occurrence of the entry's local time HH:MM in its IANA tz
    (skipping weekdays not in 'weekdays', if any)."""
    try:
        zone = ZoneInfo(entry.get("tz") or "UTC")
        hh, mm = (entry["time"] or "00:00").split(":")
        local = datetime.fromtimestamp(now, zone)
        cand = local.replace(hour=int(hh), minute=int(mm),
                             second=0, microsecond=0)
        if cand <= local:
            cand += timedelta(days=1)
        while not _weekday_ok(entry, cand):
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
    day), skipping candidates outside the window (if any) and on
    excluded weekdays (if any)."""
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

    for d in range(8):  # up to a week ahead (weekdays may skip several days)
        mid = mid0 if d == 0 else mid0 + timedelta(days=d)
        for pos in positions():
            cand = mid + timedelta(minutes=pos)
            if cand <= now_dt:  # 'now' may have seconds -> strictly after
                continue
            if _in_window(entry, cand.timestamp()) \
                    and _weekday_ok(entry, cand):
                return cand.timestamp()
    return now + every * 60


def _snap_to_weekday(entry, ts):
    """Advance ts (keeping the time) until it lands on an allowed weekday."""
    allowed = _allowed_weekdays(entry)
    if allowed is None:
        return ts
    try:
        zone = ZoneInfo(_entry_tz(entry))
    except Exception:
        return ts
    dt = datetime.fromtimestamp(ts, zone)
    while dt.weekday() not in allowed:
        dt += timedelta(days=1)
    return dt.timestamp()


def initial_next_run(entry, now):
    if (entry.get("time") or "").strip():
        return _next_daily(entry, now)
    if _sched_kind_ok(entry):
        if _anchor_minutes(entry) is not None:
            return _anchored_next(entry, now)
        if not _in_window(entry, now):
            return _snap_to_weekday(entry, _next_window_start(entry, now))
        return _snap_to_weekday(entry, now + entry["every_minutes"] * 60)
    return None


def advance_next_run(entry, now):
    if (entry.get("time") or "").strip():
        return _next_daily(entry, now)
    if _sched_kind_ok(entry):
        if _anchor_minutes(entry) is not None:
            return _anchored_next(entry, now)
        nxt = now + entry["every_minutes"] * 60
        if not _in_window(entry, nxt):
            nxt = _next_window_start(entry, nxt)
        return _snap_to_weekday(entry, nxt)
    return None


# ------------------------------------------------------------- tick -------

def tick():
    now = time.time()
    if not os.path.isdir(BOTS_DIR):
        return
    for bot in sorted(os.listdir(BOTS_DIR)):
        rooms_dir = os.path.join(BOTS_DIR, bot, "rooms")
        if not os.path.isdir(rooms_dir):
            continue
        for folder in sorted(os.listdir(rooms_dir)):
            cwd = room_dir(bot, folder)
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
                    entry["next_run"] = initial_next_run(entry, now)
                    changed = True
                if entry.get("next_run") is not None and \
                        entry["next_run"] <= now:
                    due.append(entry)
            if not due and not changed:
                continue
            # advance due entries and persist BEFORE enqueueing (so a crash
            # right after can never double-fire a schedule); an entry that
            # turned out to land on an excluded weekday (e.g. the config
            # changed since next_run was written) is just advanced again
            fire = []
            for entry in due:
                entry["next_run"] = advance_next_run(entry, now)
                if _weekday_ok(entry, datetime.fromtimestamp(
                        now, ZoneInfo(_entry_tz(entry)))):
                    fire.append(entry)
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, path)
            for entry in fire:
                token = room_token(cwd)
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


def scheduler_loop():
    while True:
        _scheduler["last_run"] = time.time()
        try:
            tick()
        except Exception as e:
            log.warning("scheduler tick failed: %s", e)
        time.sleep(SCHEDULER_INTERVAL)
