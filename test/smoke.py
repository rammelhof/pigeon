"""Local smoke test: fake pi + fake Nextcloud (no docker needed).

Run:  python test/smoke.py
Covers: pi-config gate, HMAC webhook verification, payload parsing,
queueing, pi JSONL parsing (thinking + answer), posting via the bot API
(with outgoing signature verification), retry with --continue, heartbeat
timeout kill, /health output.
"""
import hashlib
import hmac
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime

try:  # Windows consoles are cp1252 by default
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

BOTS_TEST = os.path.join(ROOT, "bots-test")
PI_CFG = os.path.join(ROOT, "pi-cfg-test")
SECRET = "x" * 64

os.environ.update({
    "NEXTCLOUD_URL": "http://127.0.0.1:8099",
    "TALK_BOT_SECRET": SECRET,
    "PI_BIN": "fake-pi",
    "BOTS_DIR": BOTS_TEST,
    "PI_CODING_AGENT_DIR": PI_CFG,
    "PI_TIMEOUT": "3",
    "HEARTBEAT_INTERVAL": "1",
    "SCHEDULER_INTERVAL": "1",
    "STATE_SCAN_EVERY": "1",
    "MAX_RETRIES": "1",
    "LOG_LEVEL": "INFO",
    "PORT": "8088",
})
os.makedirs(PI_CFG, exist_ok=True)  # auth.json created later


# fake pi ------------------------------------------------------------------
GOOD = [
    json.dumps({"type": "session_header", "id": "f1", "version": "1"}),
    json.dumps({"type": "message_end",
                "message": {"role": "user", "content": "x"}}),
    json.dumps({"type": "message_end",
                "message": {"role": "assistant",
                            "content": [
                                {"type": "thinking",
                                 "thinking": "step one\nstep two"},
                                {"type": "text", "text": "hello from fake pi"}],
                            "stopReason": "stop"}}),
    json.dumps({"type": "agent_settled", "aborted": False}),
]


class FakePopen:
    _seen = set()  # (cwd, prompt) already attempted

    def __init__(self, cmd, **kw):
        self.cmd = cmd
        self.returncode = 0
        self.killed = False
        prompt = cmd[-1]
        cwd = kw.get("cwd", "")
        key = (cwd, prompt)
        first_attempt = key not in self._seen
        self._seen.add(key)
        if "make-fail" in prompt and first_attempt:
            self.returncode = 1
            self.stdout = iter([])
            self.stderr = io.StringIO("boom: provider error")
        elif "hang" in prompt and first_attempt:
            self.stdout = self._hang()
            self.stderr = io.StringIO("")
        else:
            self.stdout = iter(GOOD)
            self.stderr = io.StringIO("")

    def _hang(self):
        yield GOOD[0]
        while not self.killed:
            time.sleep(0.1)

    def wait(self):
        self._finished = True
        return self.returncode

    def poll(self):
        return self.returncode if getattr(self, "_finished", False) else None

    def kill(self):
        self.killed = True
        self.returncode = 137


import subprocess
subprocess.Popen = FakePopen

# fake nextcloud -------------------------------------------------------------
import nc_stub  # noqa: E402
_nc = nc_stub.make_server(secret=SECRET)
threading.Thread(target=_nc.serve_forever, daemon=True).start()

# app ------------------------------------------------------------------------
import app  # noqa: E402
import traceback  # noqa: E402
app.startup()  # same init as the gunicorn post_fork hook
from werkzeug.serving import run_simple  # noqa: E402


def _serve():
    try:
        run_simple("127.0.0.1", 8088, app.app,
                   use_reloader=False, threaded=True)
    except BaseException:
        traceback.print_exc()


threading.Thread(target=_serve, daemon=True).start()
time.sleep(0.5)

BASE = "http://127.0.0.1:8088"
failures = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name
          + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        failures.append(name)


def http(path, body=None, headers=None, method=None):
    data = body.encode() if isinstance(body, str) else body
    req = urllib.request.Request(BASE + path, data=data,
                                 headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            raw = r.read()
            code = r.status
    except urllib.error.HTTPError as e:
        raw, code = e.read(), e.code
    try:
        return code, json.loads(raw or b"{}")
    except json.JSONDecodeError:
        return code, {}


def get(path):
    return http(path)


def new_payload(message, room_token="ho5w45n9", room_name="AssiBot"):
    return {
        "type": "Create",
        "actor": {"type": "Person", "id": "users/Raphael",
                  "name": "Raphael", "talkParticipantType": "1"},
        "object": {"type": "Note", "id": "999", "name": "message",
                   "content": json.dumps({"message": message, "parameters": []}),
                   "mediaType": "text/markdown"},
        "target": {"type": "Collection", "id": room_token,
                   "name": room_name},
        "published": "2026-10-12T11:21:23+00:00",
    }


def signed_webhook(bot, payload, secret=SECRET, raw_override=None):
    """POST a new-protocol webhook with (correct or forged) signature."""
    raw = raw_override if raw_override is not None else json.dumps(payload)
    random_str = os.urandom(32).hex()
    sig = hmac.new(secret.encode(), (random_str + raw).encode(),
                   hashlib.sha256).hexdigest()
    return http(f"/webhook/{bot}", body=raw, method="POST", headers={
        "Content-Type": "application/json",
        "X-Nextcloud-Talk-Random": random_str,
        "X-Nextcloud-Talk-Signature": sig,
        "OCS-APIRequest": "true",
    })


def tasks():
    _, h = get("/health")
    return h


def wait_room_done(cwd, timeout=25, min_count=1, retries=None):
    deadline = time.time() + timeout
    h = {}
    while time.time() < deadline:
        h = tasks()
        mine = [t for t in h["tasks"]
                if t["room_cwd"].replace("\\", "/").endswith(cwd)]
        if len(mine) >= min_count and mine[-1]["status"] in ("done", "failed") \
                and (retries is None or mine[-1]["retries"] == retries):
            return mine[-1]
        time.sleep(0.3)
    return None


# 0. pi not configured -> webhook 503 + health degraded
auth_file = os.path.join(PI_CFG, "auth.json")
os.path.exists(auth_file) and os.remove(auth_file)
s, h = get("/health")
check("health 503 while pi unconfigured", s == 503, str(s))
check("health reports pi unconfigured", h.get("pi", {}).get("configured") is False)
s, body = signed_webhook("bot1", new_payload("hi"))
check("webhook 503 while pi unconfigured",
      s == 503 and "configured" in body.get("error", ""), str(s))

# now configure pi
json.dump({"fake": "credential"}, open(auth_file, "w"))

# 1. health ok
s, h = get("/health")
check("health 200 after config", s == 200, str(s))
check("health shape", h.get("status") == "ok"
      and h.get("pi", {}).get("configured") is True
      and "heartbeat" in h and "tasks" in h)
check("health reports version + commit",
      h.get("version") == "1.0.0" and "commit" in h, str(h.get("version")))
s, idx = get("/")
check("index reports version + commit",
      idx.get("version") == "1.0.0" and "commit" in idx, str(idx))

# 2. bad signature
s, body = signed_webhook("bot1", new_payload("hi"), secret="y" * 64)
check("bad signature 403", s == 403, str(s))

# 3. invalid bot name
s, _ = signed_webhook("bad..name", new_payload("hi"))
check("invalid bot name 400", s == 400, str(s))

# 4. good new-protocol message -> thinking + answer posted, task done
s, body = signed_webhook("bot1", new_payload("hi there"))
check("webhook 202", s == 202, str(s))
check("bot + room folder in reply", body.get("bot") == "bot1"
      and body.get("room_folder") == "assibot", str(body))
t = wait_room_done("bot1/rooms/assibot")
check("task done", bool(t) and t["status"] == "done", str(t))
check("bots dir structure",
      os.path.isdir(os.path.join(BOTS_TEST, "bot1", "rooms", "assibot")))

# 4b. file attachment: {file} placeholder -> download into chat/files
NC = "http://127.0.0.1:8099"


def file_payload(message, link, room_token, room_name):
    p = new_payload(message, room_token=room_token, room_name=room_name)
    p["object"]["content"] = json.dumps({
        "message": message,
        "parameters": {"file": {"type": "file", "id": "65885",
                                "name": "starwars-darth-vader.webp",
                                "size": "8048", "link": link,
                                "mimetype": "image/webp"}}})
    return p


s, body = signed_webhook("bot1",
                         file_payload("look at {file}",
                                      f"{NC}/s/XL4BZ8Dd89Zk8Bf",
                                      "fileroom01", "File Room"))
check("webhook with file 202", s == 202, str(s))
file_cwd = os.path.join(BOTS_TEST, "bot1", "rooms", "file-room")
files_dir = os.path.join(file_cwd, "chat", "files")
saved = [f for f in (os.listdir(files_dir) if os.path.isdir(files_dir)
                     else []) if f.endswith(".webp")]
check("file downloaded into chat/files/",
      len(saved) == 1 and saved[0].endswith("starwars-darth-vader.webp"),
      str(saved))
if saved:
    data = open(os.path.join(files_dir, saved[0]), "rb").read()
    check("downloaded file content matches the share",
          data == nc_stub.H.file_bytes, data[:40])
    state = json.load(open(os.path.join(file_cwd, ".pigeon.json")))
    msgs = [e["message"] for e in state["pending"]]
    if state.get("running"):
        msgs.append(state["running"]["message"])
    joined = " | ".join(msgs)
    check("{file} replaced with hint to the local file",
          "{file}" not in joined and "chat/files/" in joined
          and saved[0] in joined, joined[:200])
t = wait_room_done("bot1/rooms/file-room")
check("file task done", bool(t) and t["status"] == "done", str(t))
prompted = [p for c, p in FakePopen._seen
            if c.replace("\\", "/").endswith("bot1/rooms/file-room")]
check("pi was prompted with the local file hint",
      any("chat/files/" in p and saved[0] in p for p in prompted),
      str(prompted)[:200])

# 4c. broken share link -> download failure hint, task still runs
s, body = signed_webhook("bot1",
                         file_payload("look at {file}",
                                      f"{NC}/s/missing-share",
                                      "brokenroom2", "Broken Room"))
check("webhook with broken file 202", s == 202, str(s))
b_cwd = os.path.join(BOTS_TEST, "bot1", "rooms", "broken-room")
b_state = json.load(open(os.path.join(b_cwd, ".pigeon.json")))
b_msgs = [e["message"] for e in b_state["pending"]]
if b_state.get("running"):
    b_msgs.append(b_state["running"]["message"])
check("broken file replaced with failure hint + share link",
      any("download failed" in m and "missing-share" in m
          for m in b_msgs), str(b_msgs)[:200])
b_files = os.path.join(b_cwd, "chat", "files")
leftover = [f for f in (os.listdir(b_files) if os.path.isdir(b_files)
                        else []) if f.endswith(".webp")]
check("no partial file left behind", not leftover, str(leftover))

# 5. non-message event (reaction) ignored
payload = new_payload("like")
payload["object"]["name"] = "reaction"
s, body = signed_webhook("bot1", payload)
check("non-message event ignored", s == 200 and body.get("ignored"), str(body))

# 6. empty message ignored
s, body = signed_webhook("bot1", new_payload("   "))
check("empty message ignored", s == 200 and body.get("ignored"), str(body))

# 7. failing prompt -> retried with --continue -> done
s, _ = signed_webhook("bot1", new_payload("make-fail"))
t = wait_room_done("bot1/rooms/assibot", min_count=2, retries=1)
check("failed run retried and done",
      bool(t) and t["status"] == "done" and t["retries"] == 1, str(t))

# 8. hanging prompt (different bot) -> heartbeat kills -> retry -> done;
#    a second message stays queued and is persisted in the state file
s, _ = signed_webhook("bot2", new_payload("hang", room_token="h4ngtok1",
                                          room_name="Hang Room"))
s, _ = signed_webhook("bot2", new_payload("follow-up please",
                                          room_token="h4ngtok1",
                                          room_name="Hang Room"))
time.sleep(0.5)
state_file = os.path.join(BOTS_TEST, "bot2", "rooms", "hang-room",
                          ".pigeon.json")
try:
    state = json.load(open(state_file))
except Exception:
    state = {}
check("pending message persisted in state file",
      any(p.get("message") == "follow-up please"
          for p in state.get("pending", [])), str(state))
check("running entry stores the room token (needed for resume)",
      (state.get("running") or {}).get("room") == "h4ngtok1", str(state))
t = wait_room_done("bot2/rooms/hang-room", min_count=2, timeout=30)
check("hang killed by heartbeat, continued, follow-up processed",
      bool(t) and t["status"] == "done", str(t))
state = json.load(open(state_file))
check("state file clean after tasks done",
      state.get("pending") == [] and state.get("running") is None, str(state))
check("bot2 has own rooms dir",
      os.path.isdir(os.path.join(BOTS_TEST, "bot2", "rooms", "hang-room")))

s, body = signed_webhook("bot3", new_payload("hi via bot3", room_token="tok3nabc",
                                          room_name="Team Bot"))
t = wait_room_done("bot3/rooms/team-bot")
check("bot3 gets own folder + session",
      s == 202 and bool(t) and t["status"] == "done", f"{s} {body} {t}")

# 10. restart: interrupted (running) + pending jobs are re-queued and run
resume_dir = os.path.join(BOTS_TEST, "bot4", "rooms", "resumed-room")
os.makedirs(resume_dir)
with open(os.path.join(resume_dir, ".pigeon.json"), "w") as f:
    json.dump({
        "pending": [{"message": "second one", "sender": "x",
                     "room": "resu4med", "at": time.time()},
                    # old-version format: no "room" token -> must be dropped
                    {"message": "old format no room", "sender": "x",
                     "at": time.time()}],
        "running": {"message": "interrupted one", "sender": "x",
                    "room": "resu4med", "started_at": time.time(),
                    "retries": 1, "task_id": "old-1"},
    }, f)
resumed = app.requeue_stale()
t = wait_room_done("bot4/rooms/resumed-room", min_count=2)
mine = [x for x in tasks()["tasks"]
        if "resumed-room" in x["room_cwd"].replace("\\", "/")]
check("interrupted + pending jobs resumed after restart",
      resumed == 2 and bool(t) and t["status"] == "done", f"{resumed} {t}")
check("old-format job without room token dropped (no bogus post)",
      len(mine) == 2, str(mine))

# 11. heartbeat self-healing: a stale on-disk job (unknown task, e.g. after
# a crash) is re-queued by the periodic state scan
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
      state.get("pending") == [] and state.get("running") is None, str(state))

# 12. scheduler: due entries in schedules.json fire as room messages,
# next_run advances; entries without a room token or invalid ones are skipped
from zoneinfo import ZoneInfo  # noqa: E402
sched_dir = os.path.join(BOTS_TEST, "bot6", "rooms", "news-room")
os.makedirs(sched_dir)
with open(os.path.join(sched_dir, ".room.json"), "w") as f:
    json.dump({"token": "sched6ro"}, f)
sched_file = os.path.join(sched_dir, "schedules.json")
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
os.makedirs(notok_dir)
with open(os.path.join(notok_dir, "schedules.json"), "w") as f:
    json.dump({"schedules": [
        {"id": "no-token-room", "prompt": "Should never post.",
         "time": "08:00", "tz": "Europe/Vienna",
         "next_run": time.time() - 10},
    ]}, f)
t = wait_room_done("bot6/rooms/news-room", timeout=20, min_count=2)
check("scheduler fired due entries in the room",
      bool(t) and t["status"] == "done", str(t))
posted_before = [m for room, m in nc_stub.H.received if room == "sched6ro"]
posted_after = [m for room, m in nc_stub.H.received if room == "sched6ro"]
t = wait_room_done("bot6/rooms/news-room", timeout=10, min_count=2)
check("scheduler tasks replied in the room (signed bot API posts)",
      len(posted_after) >= 2, str(len(posted_after)))
tasks_now = tasks()["tasks"]
check("scheduler skips rooms without a room token (no task, no post)",
      not [x for x in tasks_now
           if "no-token" in x["room_cwd"].replace("\\", "/")], str(tasks_now))
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
             "window": {"start": "10:00", "end": "22:00", "tz": "Europe/Vienna"}}

def at(day, hh, mm):
    return int(datetime(2026, 10, day, hh, mm, tzinfo=VIE).timestamp())


nxt = app._advance_next_run(win_entry, at(13, 21, 30))
loc = datetime.fromtimestamp(nxt, VIE)
check("window: run after the end snaps to the next day's window start",
      (loc.day, loc.hour, loc.minute) == (14, 10, 0), str(loc))
nxt = app._advance_next_run(win_entry, at(13, 15, 0))
loc = datetime.fromtimestamp(nxt, VIE)
check("window: run inside the window advances by the interval",
      (loc.day, loc.hour, loc.minute) == (13, 16, 0), str(loc))
nxt = app._initial_next_run(win_entry, at(13, 3, 0))
loc = datetime.fromtimestamp(nxt, VIE)
check("window: initial next run before the window is the window start",
      (loc.day, loc.hour, loc.minute) == (13, 10, 0), str(loc))
nxt = app._initial_next_run(win_entry, at(13, 11, 0))
loc = datetime.fromtimestamp(nxt, VIE)
check("window: initial next run inside the window adds the interval",
      (loc.day, loc.hour, loc.minute) == (13, 12, 0), str(loc))
no_win = {"prompt": "x", "every_minutes": 30}
nxt = app._advance_next_run(no_win, at(13, 3, 0))
check("no window: plain interval runs around the clock",
      nxt == at(13, 3, 0) + 30 * 60, str(nxt))

# 15. scheduler start_time anchor: runs on the start_time+N*interval phase
anch = {"prompt": "x", "every_minutes": 15, "start_time": "10:30",
        "tz": "Europe/Vienna"}
nxt = app._initial_next_run(anch, at(13, 10, 20))
check("anchor: next run before the anchor is the anchor",
      (datetime.fromtimestamp(nxt, VIE).hour,
       datetime.fromtimestamp(nxt, VIE).minute) == (10, 30), str(nxt))
nxt = app._advance_next_run(anch, at(13, 10, 31))
check("anchor: run advances to the next phase step",
      (datetime.fromtimestamp(nxt, VIE).hour,
       datetime.fromtimestamp(nxt, VIE).minute) == (10, 45), str(nxt))
nxt = app._advance_next_run(anch, at(13, 23, 59))
loc = datetime.fromtimestamp(nxt, VIE)
check("anchor: after end of day the phase continues next day",
      (loc.day, loc.hour, loc.minute) == (14, 10, 30), str(loc))
anch_win = {"prompt": "x", "every_minutes": 70, "start_time": "10:00",
            "window": {"start": "10:00", "end": "12:00", "tz": "Europe/Vienna"}}
nxt = app._advance_next_run(anch_win, at(13, 10, 0))
loc = datetime.fromtimestamp(nxt, VIE)
check("anchor+window: in-window phase step is kept (10:00 -> 11:10)",
      (loc.day, loc.hour, loc.minute) == (13, 11, 10), str(loc))
nxt = app._advance_next_run(anch_win, at(13, 11, 11))
loc = datetime.fromtimestamp(nxt, VIE)
check("anchor+window: out-of-window steps skipped (12:20 -> next day 10:00)",
      (loc.day, loc.hour, loc.minute) == (14, 10, 0), str(loc))

# 13. bundled skills are copied into the pi config at startup: new skills
# are added, changed ones are updated, user skills are left untouched
skills_src = tempfile.mkdtemp()
skills_dest = os.path.join(PI_CFG, "skills")
user_skill = os.path.join(skills_dest, "user-skill")
os.makedirs(user_skill)
with open(os.path.join(user_skill, "SKILL.md"), "w") as f:
    f.write("---\nname: user-skill\ndescription: mine\n---\nuser content")


def put_skill(name, body):
    d = os.path.join(skills_src, name)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "SKILL.md"), "w") as f:
        f.write(body)


put_skill("bundled-a", "v1")
n = app.sync_skills(src=skills_src, dest=skills_dest)
check("sync_skills copies bundled skills into the pi config",
      n == 1 and open(os.path.join(skills_dest, "bundled-a", "SKILL.md")).read() == "v1")
put_skill("bundled-a", "v2")
put_skill("bundled-b", "new")
app.sync_skills(src=skills_src, dest=skills_dest)
check("sync_skills updates changed skills on next start",
      open(os.path.join(skills_dest, "bundled-a", "SKILL.md")).read() == "v2"
      and os.path.exists(os.path.join(skills_dest, "bundled-b", "SKILL.md")))
check("sync_skills leaves user skills untouched",
      open(os.path.join(user_skill, "SKILL.md")).read().endswith("user content"))
shutil.rmtree(skills_src, ignore_errors=True)

print()
if failures:
    print("FAILED:", failures)
    sys.exit(1)
print("ALL TESTS PASSED")
shutil.rmtree(BOTS_TEST, ignore_errors=True)
shutil.rmtree(PI_CFG, ignore_errors=True)
