"""Local smoke test: fake pi + fake Nextcloud (no docker needed).

Run:  python test/smoke.py
Covers: webhook auth, bot-name path, pi-config gate, room lookup,
queueing, pi JSONL parsing (thinking + answer), posting back,
retry with --continue, heartbeat timeout kill, /health output.
"""
import io
import json
import os
import shutil
import sys
import threading
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

BOTS_TEST = os.path.join(ROOT, "bots-test")
PI_CFG = os.path.join(ROOT, "pi-cfg-test")

os.environ.update({
    "NEXTCLOUD_URL": "http://127.0.0.1:8099",
    "NEXTCLOUD_BOT_TOKEN": "testtoken",
    "PI_BIN": "fake-pi",
    "BOTS_DIR": BOTS_TEST,
    "PI_CODING_AGENT_DIR": PI_CFG,
    "PI_TIMEOUT": "3",
    "HEARTBEAT_INTERVAL": "1",
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
    def __init__(self, cmd, **kw):
        self.cmd = cmd
        self.returncode = 0
        self.killed = False
        prompt = cmd[-1]
        if "make-fail" in prompt and "--continue" not in cmd:
            self.returncode = 1
            self.stdout = iter([])
            self.stderr = io.StringIO("boom: provider error")
        elif "hang" in prompt and "--continue" not in cmd:
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
_nc = nc_stub.make_server()
threading.Thread(target=_nc.serve_forever, daemon=True).start()

# app ------------------------------------------------------------------------
import app  # noqa: E402
import traceback  # noqa: E402
threading.Thread(target=app._heartbeat_loop, daemon=True).start()
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


def get(path):
    req = urllib.request.Request(BASE + path)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        raw = e.read() or b"{}"
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, {}


def post(path, body):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        raw = e.read() or b"{}"
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, {}


def tasks():
    _, h = get("/health")
    return h


# 0. pi not configured -> webhook 503 + health degraded
auth_file = os.path.join(PI_CFG, "auth.json")
os.path.exists(auth_file) and os.remove(auth_file)
s, h = get("/health")
check("health 503 while pi unconfigured", s == 503, str(s))
check("health reports pi unconfigured", h.get("pi", {}).get("configured") is False)
s, body = post("/webhook/bot1", {"token": "testtoken", "room_id": 1,
                                 "message": "hi"})
check("webhook 503 while pi unconfigured", s == 503 and "configured" in body.get("error", ""), str(s))

# now configure pi
json.dump({"fake": "credential"}, open(auth_file, "w"))

# 1. health ok
s, h = get("/health")
check("health 200 after config", s == 200, str(s))
check("health shape", h.get("status") == "ok" and h.get("pi", {}).get("configured") is True
      and "heartbeat" in h and "tasks" in h)

# 2. bad token
s, _ = post("/webhook/bot1", {"token": "wrong", "room_id": 1, "message": "hi"})
check("bad token 403", s == 403, str(s))

# 3. invalid bot name
s, _ = post("/webhook/bad..name", {"token": "testtoken", "room_id": 1, "message": "hi"})
check("invalid bot name 400/404", s in (400, 404), str(s))

# 4. good message -> thinking + answer posted, task done
s, body = post("/webhook/bot1", {"token": "testtoken", "room_id": 123,
                                 "room_type": "group",
                                 "sender_display_name": "Rapha",
                                 "message": "hi there"})
check("webhook 202", s == 202, str(s))
check("bot + room folder in reply", body.get("bot") == "bot1"
      and body.get("room_folder") == "dev-team", str(body))
deadline = time.time() + 15
done = False
while time.time() < deadline:
    h = tasks()
    if any(t["status"] == "done" for t in h["tasks"]):
        done = True
        break
    time.sleep(0.3)
check("task done", done, json.dumps(h["tasks"]))
check("bots dir structure",
      os.path.isdir(os.path.join(BOTS_TEST, "bot1", "rooms", "dev-team")))

# 5. failing prompt -> retried with --continue -> done
s, _ = post("/webhook/bot1", {"token": "testtoken", "room_id": 123, "message": "make-fail"})
deadline = time.time() + 15
ok = False
while time.time() < deadline:
    h = tasks()
    retried = [t for t in h["tasks"]
               if t["status"] == "done" and t["retries"] == 1]
    if retried:
        ok = True
        break
    if any(t["status"] == "failed" for t in h["tasks"]):
        break
    time.sleep(0.3)
check("failed run retried and done", ok, json.dumps(h.get("tasks", [])))

# 6. hanging prompt (different bot) -> heartbeat kills -> retry -> done
s, _ = post("/webhook/bot2", {"token": "testtoken", "room_id": 456, "message": "hang"})
deadline = time.time() + 25
ok = False
while time.time() < deadline:
    h = tasks()
    t456 = [t for t in h["tasks"] if "-456-" in t["id"]]
    if t456 and t456[0]["status"] == "done" and t456[0]["retries"] == 1:
        ok = True
        break
    if t456 and t456[0]["status"] == "failed":
        break
    time.sleep(0.5)
check("hang killed by heartbeat, continued", ok, json.dumps(h.get("tasks", [])))
check("bot2 has own rooms dir",
      os.path.isdir(os.path.join(BOTS_TEST, "bot2", "rooms", "dev-team")))

# 7. empty message ignored
s, body = post("/webhook/bot1", {"token": "testtoken", "room_id": 1, "message": ""})
check("empty message 200 ignored", s == 200 and body.get("ignored"))

print()
if failures:
    print("FAILED:", failures)
    sys.exit(1)
print("ALL TESTS PASSED")
shutil.rmtree(BOTS_TEST, ignore_errors=True)
shutil.rmtree(PI_CFG, ignore_errors=True)
