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
    "MESSAGE_BATCH_WINDOW": "1",
    "SANDBOX": "1",
    "LOG_LEVEL": "INFO",
    "PORT": "8088",
})
# fake bwrap on PATH: app.py checks shutil.which("bwrap"); the command is
# never actually executed (FakePopen replaces subprocess.Popen)
FAKE_BIN = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "fake-bin")
os.makedirs(FAKE_BIN, exist_ok=True)
for _name in ("bwrap", "bwrap.exe"):
    with open(os.path.join(FAKE_BIN, _name), "wb"):
        pass
os.environ["PATH"] = FAKE_BIN + os.pathsep + os.environ["PATH"]
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
    cmds = []      # every (cwd, cmd) pair — for sandbox assertions

    def __init__(self, cmd, **kw):
        self.cmd = cmd
        self.returncode = 0
        self.killed = False
        prompt = cmd[-1]
        cwd = kw.get("cwd", "")
        FakePopen.cmds.append((cwd, list(cmd)))
        key = (cwd, prompt)
        first_attempt = key not in self._seen
        self._seen.add(key)
        if (os.path.basename(cmd[0]).startswith("bwrap")
                and os.environ.get("FAKE_BWRAP_FAIL") == "1"):
            # simulate: sandbox cannot start in this container
            self.returncode = 1
            self.stdout = iter([])
            self.stderr = io.StringIO(
                "bwrap: Creating new namespace failed: Operation not "
                "permitted\n")
        elif "make-fail" in prompt and first_attempt:
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


def wait_prompt(needle, cwd_suffix, timeout=8):
    """Poll until a pi prompt containing needle was seen for cwd_suffix."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        for c, p in FakePopen._seen:
            if c.replace("\\", "/").endswith(cwd_suffix) and needle in p:
                return p
        time.sleep(0.1)
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

# 4a. pi runs in the room's workspace, wrapped in a bubblewrap sandbox
_assi_ws = os.path.join(BOTS_TEST, "bot1", "rooms", "assibot",
                        "workspace")
_cwd_used, last_cmd = FakePopen.cmds[-1]
check("pi runs with the room workspace as working directory",
      _cwd_used.replace("\\", "/").endswith(
          "bot1/rooms/assibot/workspace"), str(_cwd_used))
check("pi runs under bubblewrap",
      os.path.basename(last_cmd[0]).startswith("bwrap"),
      str(last_cmd[:1]))
check("sandbox: read-only tool belt (incl. /run for DNS) + writable "
      "workspace and pi home",
      "--ro-bind" in last_cmd and "/usr" in last_cmd and "/run" in last_cmd
      and last_cmd.count("--bind") == 2
      and _assi_ws in last_cmd and PI_CFG in last_cmd, str(last_cmd))
check("sandbox: merged-usr shims recreated (dynamic linker visible)",
      "--symlink" in last_cmd and "usr/lib64" in last_cmd
      and "/lib64" in last_cmd, str(last_cmd))
check("sandbox: inherit NO env except PATH/HOME/LANG/PI_CODING_AGENT_DIR",
      "--clearenv" in last_cmd and "--setenv" in last_cmd
      and "PATH" in last_cmd and "PI_CODING_AGENT_DIR" in last_cmd
      and PI_CFG in last_cmd and "/app" in last_cmd, str(last_cmd))
check("sandbox: masks applied BEFORE the workspace bind (ws is under "
      "BOTS_DIR, a later tmpfs would hide it)",
      last_cmd.index(BOTS_TEST) < last_cmd.index("--bind"),
      str(last_cmd))
check("sandbox: fresh pid/ipc/uts, full network, chdir into workspace",
      "--unshare-pid" in last_cmd and "--unshare-ipc" in last_cmd
      and "--unshare-uts" in last_cmd and "--unshare-net" not in last_cmd
      and last_cmd[last_cmd.index("--chdir") + 1] == _assi_ws
      and "fake-pi" in last_cmd, str(last_cmd))

# 4a2. bwrap exists but cannot start -> the task FAILS (no fallback)
os.environ["FAKE_BWRAP_FAIL"] = "1"
s, body = signed_webhook("bot1", new_payload("sandbox down"))
check("webhook 202 (sandbox-down room msg)", s == 202, str(s))
t = wait_room_done("bot1/rooms/assibot", timeout=25, min_count=2)
check("unstartable sandbox fails the task (no unsandboxed fallback)",
      bool(t) and t["status"] == "failed" and "bwrap" in (t["error"] or ""),
      str(t))
del os.environ["FAKE_BWRAP_FAIL"]
_attempts = [os.path.basename(c[1][0]).casefold() for c in FakePopen.cmds]
_bwrap_tail = [c for c in _attempts if c.startswith("bwrap")][-2:]
check("every attempt used bwrap (pi never ran unsandboxed)",
      len(_bwrap_tail) == 2, str(_attempts[-4:]))

# 4b. /pw room command (per-room app password for file downloads)
nc_stub.H.share_auth = {"ncuser": "ncpw123"}


def file_payload(message, room_token, room_name,
                 file_id=65890, name="starwars-r2-d2.webp"):
    p = new_payload(message, room_token=room_token, room_name=room_name)
    p["object"]["content"] = json.dumps({"message": message,
        "parameters": {"file": {"type": "file", "id": str(file_id),
                                "name": name, "size": "9616",
                                "link": "https://nc.test/s/virtual-link",
                                "mimetype": "image/webp"}}})
    return p


s, body = signed_webhook("bot1", new_payload("/pw",
                                             room_token="pwroom0001",
                                             room_name="Pw Room"))
check("/pw status: handled, not queued",
      s == 200 and body.get("handled") == "/pw", str(body))
pw_cwd = os.path.join(BOTS_TEST, "bot1", "rooms", "pw-room")
check("/pw does not create a task/state file",
      not os.path.exists(os.path.join(pw_cwd, ".pigeon.json")))
posted = [m for room, m in nc_stub.H.received if room == "pwroom0001"]
check("/pw status reply: not configured",
      any("not configured" in m for m in posted), str(posted)[:200])

s, body = signed_webhook("bot1",
                         new_payload("/pw ncuser ncpw123",
                                     room_token="pwroom0001",
                                     room_name="Pw Room"))
check("/pw set: handled", s == 200 and body.get("handled") == "/pw",
      str(body))
meta = json.load(open(os.path.join(pw_cwd, ".room.json")))
check("/pw credentials stored per room (token kept)",
      meta.get("nextcloud_user") == "ncuser"
      and meta.get("nextcloud_password") == "ncpw123"
      and meta.get("token") == "pwroom0001", str(meta))
posted = [m for room, m in nc_stub.H.received if room == "pwroom0001"]
check("/pw set reply posted without echoing the password",
      any("saved" in m and "ncpw123" not in m for m in posted),
      str(posted)[:200])
signed_webhook("bot1", new_payload("/pw", room_token="pwroom0001",
                                   room_name="Pw Room"))
posted = [m for room, m in nc_stub.H.received if room == "pwroom0001"]
check("/pw status reply: configured",
      any("configured for user: ncuser" in m for m in posted),
      str(posted)[:200])

# 4c. file message without app password -> pi is instructed (user language)
directs_before = len(nc_stub.H.search_calls)
s, body = signed_webhook("bot1", file_payload("look at {file}",
                                              "norpwrm001", "No Pw Room"))
check("file msg without password 202", s == 202, str(s))
np_cwd = os.path.join(BOTS_TEST, "bot1", "rooms", "no-pw-room")
t = wait_room_done("bot1/rooms/no-pw-room")
check("no-password file task done", bool(t) and t["status"] == "done",
      str(t))
prompted = [p for c, p in FakePopen._seen
            if c.replace("\\", "/").endswith("bot1/rooms/no-pw-room/workspace")]
check("pi instructed about the missing app password (in user's language)",
      any("app password" in p and "/pw <nextcloud-user> <app-password>" in p
          for p in prompted), str(prompted)[:250])
check("no dav SEARCH without credentials",
      len(nc_stub.H.search_calls) == directs_before
      and not os.path.isdir(os.path.join(np_cwd, "workspace",
                                         "chat", "files")),
      str(nc_stub.H.search_calls))

# 4d. file message with room credentials -> download by file id
s, body = signed_webhook("bot1", file_payload("look at {file}",
                                              "pwroom0001", "Pw Room"))
check("file msg with password 202", s == 202, str(s))
fd = os.path.join(pw_cwd, "workspace", "chat", "files")
saved = [f for f in (os.listdir(fd) if os.path.isdir(fd) else [])
         if f.endswith(".webp")]
check("file downloaded by id into chat/files/",
      len(saved) == 1 and saved[0].endswith("starwars-r2-d2.webp"),
      str(saved))
check("dav SEARCH called with the room's credentials + file id",
      nc_stub.H.search_calls[-1] == ("ncuser", 65890),
      str(nc_stub.H.search_calls[-1:]))
if saved:
    check("downloaded content matches the share",
          open(os.path.join(fd, saved[0]), "rb").read()
          == nc_stub.H.file_bytes)
t = wait_room_done("bot1/rooms/pw-room")
check("file task done (with password)", bool(t) and t["status"] == "done",
      str(t))
check("{file} replaced with the local path in front of the message",
      wait_prompt(saved[0], "bot1/rooms/pw-room/workspace") is not None,
      str([p[:60] for c, p in FakePopen._seen
           if c.replace("\\", "/").endswith(
               "bot1/rooms/pw-room/workspace")])[:250])

# 4d2. Talk sends the user's text as its own event: file parameter but
# no {file} token -> the file is still downloaded, path in front
s, body = signed_webhook("bot1",
                         file_payload("I send 2 test files.",
                                      "pwroom0001", "Pw Room",
                                      name="starwars-c-3po.png"))
check("text event with file param 202", s == 202, str(s))
saved2 = [f for f in (os.listdir(fd) if os.path.isdir(fd) else [])
          if f.endswith(".png")]
check("file without {file} token downloaded into chat/files/",
      len(saved2) == 1 and saved2[0].endswith("starwars-c-3po.png"),
      str(saved2))
t = wait_room_done("bot1/rooms/pw-room", min_count=2)
check("text-event file task done",
      bool(t) and t["status"] == "done", str(t))
prompt = wait_prompt("I send 2 test files.", "bot1/rooms/pw-room/workspace")
check("file path added in front of the text message",
      prompt is not None and saved2[0] in prompt
      and prompt.index("chat/files/") < prompt.index("I send 2 test files."),
      str(prompt)[:200])

# 4e. wrong credentials -> failure hint + share link, task still runs
signed_webhook("bot1", new_payload("/pw ncuser WRONGpw",
                                   room_token="pwroom0001",
                                   room_name="Pw Room"))
s, body = signed_webhook("bot1", file_payload("look at {file}",
                                              "pwroom0001", "Pw Room"))
check("file msg with wrong password 202", s == 202, str(s))
t = wait_room_done("bot1/rooms/pw-room", min_count=3)  # 4d + 4d2 + 4e
check("failed-download task done", bool(t) and t["status"] == "done", str(t))
prompt = wait_prompt("download failed (dav search",
                     "bot1/rooms/pw-room/workspace")
check("wrong password -> failure hint with share link (pi was prompted)",
      prompt is not None and "virtual-link" in prompt, str(prompt))

# 5. non-message event (reaction) ignored
payload = new_payload("like")
payload["object"]["name"] = "reaction"
s, body = signed_webhook("bot1", payload)
check("non-message event ignored", s == 200 and body.get("ignored"), str(body))

# 6. empty message ignored
s, body = signed_webhook("bot1", new_payload("   "))
check("empty message ignored", s == 200 and body.get("ignored"), str(body))

# 7. failing prompt -> retried -> done (own room: no cross-talk with the
# batch tests below)
s, _ = signed_webhook("bot1", new_payload("make-fail",
                                          room_token="fail0001",
                                          room_name="Fail Room"))
t = wait_room_done("bot1/rooms/fail-room", retries=1)
check("failed run retried and done",
      bool(t) and t["status"] == "done" and t["retries"] == 1, str(t))

# 7b. batch window: messages within the window go to pi in one run
s, _ = signed_webhook("bot1", new_payload("batch one"))
s, _ = signed_webhook("bot1", new_payload("batch two"))
t = wait_room_done("bot1/rooms/assibot", min_count=3)
check("batched messages processed", bool(t) and t["status"] == "done",
      str(t))
batched = [p for c, p in FakePopen._seen
           if c.replace("\\", "/").endswith("bot1/rooms/assibot/workspace")
           and "batch one" in p and "batch two" in p]
check("burst sent to pi as one prompt with both messages",
      len(batched) == 1, str([p[:60] for p in batched]))
mine = [x for x in tasks()["tasks"]
        if x["room_cwd"].replace("\\", "/").endswith("bot1/rooms/assibot")]
check("burst produced exactly one task (not one per message)",
      len(mine) == 3, str([(x["status"]) for x in mine]))

# 8. hanging prompt (different bot) -> heartbeat kills -> retry -> done;
#    a second message stays queued and is persisted in the state file
s, _ = signed_webhook("bot2", new_payload("hang", room_token="h4ngtok1",
                                          room_name="Hang Room"))
time.sleep(1.2)  # let the batch window pass so the hang runs alone
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
