"""Shared smoke-test harness: fake pi, fake Nextcloud, fake bwrap, the
app under test (imported in-process, exactly like gunicorn does it), and
the helpers every test section uses.

Importing this module starts everything up (env, fakes, app, HTTP server).
`failures` collects every failed check; `cleanup()` removes the scratch
dirs. The sections live in test/sections/ and are run by test/smoke.py.
"""
import hashlib
import hmac
import io
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request

try:  # Windows consoles are cp1252 by default
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

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
# fake bwrap on PATH: pigeon/pi.py checks shutil.which("bwrap"); the command is
# never actually executed (FakePopen replaces subprocess.Popen)
FAKE_BIN = os.path.join(HERE, "fake-bin")
os.makedirs(FAKE_BIN, exist_ok=True)
for _name in ("bwrap", "bwrap.exe"):
    with open(os.path.join(FAKE_BIN, _name), "wb"):
        pass
os.environ["PATH"] = FAKE_BIN + os.pathsep + os.environ["PATH"]

# fresh state every run (the suite is not idempotent otherwise)
shutil.rmtree(BOTS_TEST, ignore_errors=True)
shutil.rmtree(PI_CFG, ignore_errors=True)
os.makedirs(PI_CFG, exist_ok=True)  # auth.json created later


# fake pi ------------------------------------------------------------------
# GOOD mimics a real pi stream: thinking arrives as message_update
# thinking_start/delta/end events and is repeated authoritatively in the
# assistant message_end (run_pi_once must dedupe it)
GOOD = [
    json.dumps({"type": "session_header", "id": "f1", "version": "1"}),
    json.dumps({"type": "message_end",
                "message": {"role": "user", "content": "x"}}),
    json.dumps({"type": "message_update",
                "assistantMessageEvent": {"type": "thinking_start",
                                          "contentIndex": 0}}),
    json.dumps({"type": "message_update",
                "assistantMessageEvent": {"type": "thinking_delta",
                                          "contentIndex": 0,
                                          "delta": "step one\n"}}),
    json.dumps({"type": "message_update",
                "assistantMessageEvent": {"type": "thinking_delta",
                                          "contentIndex": 0,
                                          "delta": "step two"}}),
    json.dumps({"type": "message_update",
                "assistantMessageEvent": {"type": "thinking_end",
                                          "contentIndex": 0,
                                          "content": "step one\nstep two"}}),
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
            # stand-in for pi using the nextcloud skill: call the real
            # /nc proxy with the token bwrap would have setenv'd
            if (os.path.basename(cmd[0]).startswith("bwrap")
                    and "PIGEON_NC_TOKEN" in cmd and first_attempt
                    and "bot1/rooms/ncx" in cwd.replace("\\", "/")):
                ti = cmd.index("PIGEON_NC_TOKEN")
                try:
                    urllib.request.urlopen(urllib.request.Request(
                        "http://127.0.0.1:%s/nc/bot1/ncx/files"
                        "?path=temp%%2Ffake-pi-upload.txt"
                        % os.environ.get("PIGEON_PORT", "8088"),
                        data=b"hello from sandboxed pi", method="PUT",
                        headers={"Authorization": "Bearer "
                                 + cmd[ti + 1]}),
                        timeout=5).read()
                except Exception:
                    pass
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


subprocess.Popen = FakePopen


# fake nextcloud -------------------------------------------------------------
import nc_stub  # noqa: E402
_nc = nc_stub.make_server(secret=SECRET)
threading.Thread(target=_nc.serve_forever, daemon=True).start()

# app under test (the same app object gunicorn serves) -----------------------
from pigeon.web import app, startup  # noqa: E402
startup()  # same init as the gunicorn post_fork hook
from werkzeug.serving import run_simple  # noqa: E402

_PIGEON_PORT = int(os.environ.get("PIGEON_PORT", "8088"))


def _serve():
    try:
        run_simple("127.0.0.1", _PIGEON_PORT, app,
                   use_reloader=False, threaded=True)
    except BaseException:
        traceback.print_exc()


threading.Thread(target=_serve, daemon=True).start()
time.sleep(0.5)

BASE = f"http://127.0.0.1:{_PIGEON_PORT}"
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
        return code, raw  # non-JSON (e.g. a raw file download)


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


def file_payload(message, room_token, room_name,
                 file_id=65890, name="starwars-r2-d2.webp",
                 mimetype="image/webp"):
    """A Talk file message: real Talk sends these with name 'file_shared'."""
    p = new_payload(message, room_token=room_token, room_name=room_name)
    p["object"]["name"] = "file_shared"
    p["object"]["content"] = json.dumps({"message": message,
        "parameters": {"file": {"type": "file", "id": str(file_id),
                                "name": name, "size": "9616",
                                "link": "https://nc.test/s/virtual-link",
                                "mimetype": mimetype}}})
    return p


def join_leave_payload(room_token, room_name, join=True, bot_name="Pigeon"):
    """Talk BotController enable/disable -> Join/Leave ActivityPub event."""
    return {
        "type": "Join" if join else "Leave",
        "actor": {"type": "Application", "id": "bots/bot-abc123",
                  "name": bot_name},
        "object": {"type": "Collection", "id": room_token,
                   "name": room_name},
        "published": "2026-10-12T11:21:23+00:00",
    }


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


def cleanup():
    shutil.rmtree(BOTS_TEST, ignore_errors=True)
    shutil.rmtree(PI_CFG, ignore_errors=True)
