"""Local smoke test: fake pi + fake Nextcloud (no docker needed).

Run:  python test/smoke.py
Covers: pi-config gate, HMAC webhook verification, new (NC34) + legacy
payload parsing, queueing, pi JSONL parsing (thinking + answer),
posting via the bot API, retry with --continue, heartbeat timeout
kill, /health output.
"""
import hashlib
import hmac
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
SECRET = "x" * 64

os.environ.update({
    "NEXTCLOUD_URL": "http://127.0.0.1:8099",
    "TALK_BOT_SECRET": SECRET,
    "BOT_USER": "legacyuser",
    "BOT_APP_PASSWORD": "legacypass",
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


def legacy_webhook(bot, payload):
    return http(f"/webhook/{bot}", body=json.dumps(payload), method="POST",
                headers={"Content-Type": "application/json"})


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

# 8. hanging prompt (different bot) -> heartbeat kills -> retry -> done
s, _ = signed_webhook("bot2", new_payload("hang", room_token="h4ngtok1",
                                          room_name="Hang Room"))
t = wait_room_done("bot2/rooms/hang-room", timeout=30)
check("hang killed by heartbeat, continued",
      bool(t) and t["status"] == "done" and t["retries"] == 1, str(t))
check("bot2 has own rooms dir",
      os.path.isdir(os.path.join(BOTS_TEST, "bot2", "rooms", "hang-room")))

# 9. legacy payload (no signature headers, user credentials configured)
s, body = legacy_webhook("bot3", {"token": "whatever", "room_id": 789,
                                  "message": "hello legacy"})
check("legacy webhook 202", s == 202, str(s))
t = wait_room_done("bot3/rooms/dev-team")
check("legacy task done (room name via API)",
      bool(t) and t["status"] == "done", str(t))

print()
if failures:
    print("FAILED:", failures)
    sys.exit(1)
print("ALL TESTS PASSED")
shutil.rmtree(BOTS_TEST, ignore_errors=True)
shutil.rmtree(PI_CFG, ignore_errors=True)
