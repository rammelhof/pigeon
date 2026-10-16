"""Sections 4 + 4a + 4a2: a signed message becomes a done task, and pi runs
in the room workspace under a bubblewrap sandbox (no fallback to
unsandboxed if the sandbox cannot start)."""
import os

from harness import (BOTS_TEST, FakePopen, PI_CFG, check, new_payload,
                     signed_webhook, wait_room_done)


def run():
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
