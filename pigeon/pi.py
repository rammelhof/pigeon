"""Pi invocation: config check, (sandboxed) command builder, JSONL run."""

import glob
import json
import os
import shutil
import subprocess
import time

from .config import (BOTS_DIR, PI_BIN, PI_CONFIG_DIR, SANDBOX, log)
from .room import room_creds
from .security import nc_token


def pi_config_status():
    """Pi is configured iff its default config files exist (auth.json from
    /login, models.json for custom models/providers).
    Returns (ok, path-or-dir)."""
    for name in ("auth.json", "models.json"):
        path = os.path.join(PI_CONFIG_DIR, name)
        if os.path.isfile(path) and os.path.getsize(path) > 0:
            return True, path
    return False, PI_CONFIG_DIR


def pi_command(prompt, session_id, cwd, bot, room):
    """Always run in the room's stable pi session: --session-id opens it
    or creates it (first message of a new room). The working directory is
    the room's workspace/ subfolder; when SANDBOX is on and bwrap exists,
    the whole run is wrapped in a bubblewrap sandbox (full network)."""
    pi = [PI_BIN, "--mode", "json", "--session-id", session_id, prompt]
    ws = os.path.join(cwd, "workspace")
    if not SANDBOX:
        return pi

    bwrap = shutil.which("bwrap")
    if bwrap is None:
        raise RuntimeError("SANDBOX=1 but bwrap not found — install "
                           "bubblewrap or set SANDBOX=0")
    # The sandbox root is a fresh tmpfs that only contains what is
    # bound here — so in addition to the read-only tool belt, the
    # merged-usr root symlinks must be recreated inside, or the
    # dynamic linker (/lib64/...) is invisible and every exec fails.
    flags = [
        "--ro-bind", "/usr", "/usr",
        "--ro-bind", "/etc", "/etc",
        "--ro-bind", "/var", "/var",
        "--ro-bind", "/run", "/run",      # Docker DNS: /run/resolv.conf
        "--ro-bind", "/sys", "/sys",
        # merged-usr shims (in the container / these are symlinks)
        "--symlink", "usr/lib64", "/lib64",
        "--symlink", "usr/lib", "/lib",
        "--symlink", "usr/bin", "/bin",
        "--symlink", "usr/sbin", "/sbin",
        # mask sensitive paths FIRST (empty, private to this
        # sandbox): other rooms incl. their .room.json, pigeon's
        # code. Ops are applied in order, so the writable binds
        # below pierce the /bots mask at exactly the workspace
        "--tmpfs", BOTS_DIR,
        "--tmpfs", "/app",
        # writable: this room's workspace + the pi home (sessions,
        # skills, model credentials)
        "--bind", ws, ws,
        "--bind", PI_CONFIG_DIR, PI_CONFIG_DIR,
        # inherit NO environment (TALK_BOT_SECRET lives in
        # gunicorn's) — set only what pi needs; HOME in the
        # workspace keeps the room self-contained
        "--clearenv",
        "--setenv", "PATH", "/usr/local/bin:/usr/bin:/bin",
        "--setenv", "HOME", ws,
        "--setenv", "LANG", "C.UTF-8",
        # pi must still find auth.json/models.json/sessions
        "--setenv", "PI_CODING_AGENT_DIR", PI_CONFIG_DIR,
        # fresh tmp/dev/proc (pid namespace: /proc shows pi only)
        "--tmpfs", "/tmp",
        "--dev", "/dev",
        "--proc", "/proc",
        # new PID/IPC/UTS namespaces, FULL network
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
        "--die-with-parent",
        "--chdir", ws]
    # the room's 24h Nextcloud-proxy token (only when the room has an
    # app password) — the proxy is pi's only door to Nextcloud.
    # Last flag: bwrap stops parsing at the first non-option arg.
    if room_creds(cwd) is not None:
        flags += ["--setenv", "PIGEON_NC_TOKEN", nc_token(bot, room)]
    return [bwrap] + flags + pi


def session_dir(workspace):
    """Pi stores a session per working directory:
    $PI_CODING_AGENT_DIR/sessions/--<cwd, / \\ : -> -->/<ts>_<session-id>.jsonl
    (see pi docs, session-format.md). This maps the room workspace to that
    subfolder; pi is started with exactly this workspace as cwd."""
    rel = os.path.abspath(workspace)
    if rel.startswith("/"):
        rel = rel[1:]  # pi drops the leading path separator
    rel = rel.replace("/", "-").replace("\\", "-").replace(":", "-")
    return os.path.join(PI_CONFIG_DIR, "sessions", "--" + rel + "--")


def delete_session(session_id, workspace):
    """Delete all session files of one pi session, so the next message
    starts a clean new session (no old history in the context).
    Returns the number of files removed."""
    d = session_dir(workspace)
    if not os.path.isdir(d):
        return 0
    files = glob.glob(os.path.join(d, f"*_{session_id}.jsonl"))
    for f in files:
        os.remove(f)
    return len(files)


def wipe_sessions(workspace):
    """Delete every pi session of this workspace, so the room starts
    with no history at all. Returns the number of files removed."""
    d = session_dir(workspace)
    if not os.path.isdir(d):
        return 0
    files = glob.glob(os.path.join(d, "*.jsonl"))
    for f in files:
        os.remove(f)
    return len(files)


def extract_answer_and_thinking(task):
    """Answer = text blocks of the LAST assistant message_end event.
    Thinking was collected from the event stream into task['_thinking']."""
    message = task["_last_message_end"].get("message", {})
    thinking = "\n\n".join(task["_thinking"]).strip()
    text_parts = []
    for block in message.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text" \
                and block.get("text"):
            text_parts.append(block["text"])
    answer = "\n\n".join(text_parts).strip()
    return answer, thinking


def run_pi_once(task, on_thinking=None):
    """Start one pi run, consume the JSONL stream, return (answer, thinking).
    on_thinking(cumulative_text) is called whenever a thinking block
    completes (pi streams thinking via message_update thinking_delta/
    thinking_end events), so callers can update a live chat bubble."""
    cmd = pi_command(task["message"], task["session_id"], task["cwd"],
                     task["bot"], task["room_id"])
    log.debug("task %s: running %s", task["id"], " ".join(cmd[:-1]))
    # the full prompt (which may be a batched multi-message one) is only
    # interesting for debugging — keep it at debug level
    log.debug("task %s: prompt for session %s (room %s):\n%s",
              task["id"], task["session_id"], task["room_id"],
              task["message"])
    proc = subprocess.Popen(cmd, cwd=task["workspace"],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, start_new_session=True)
    task["proc"] = proc
    task["started_at"] = time.time()
    streamed = task.setdefault("_thinking_streamed", set())

    for line in proc.stdout:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        etype = event.get("type")
        if etype == "message_update":
            ev = event.get("assistantMessageEvent") or {}
            t = ev.get("type")
            if t == "thinking_start":
                task["_thinking_idx"] = ev.get("contentIndex")
                task["_thinking_buf"] = ""
            elif (t == "thinking_delta"
                  and ev.get("contentIndex") == task.get("_thinking_idx")):
                task["_thinking_buf"] = task.get("_thinking_buf", "") \
                    + ev.get("delta", "")
            elif (t == "thinking_end"
                  and ev.get("contentIndex") == task.get("_thinking_idx")):
                task["_thinking_idx"] = None
                content = ev.get("content") or task.get("_thinking_buf", "")
                if content and content not in streamed:
                    streamed.add(content)
                    task["_thinking"].append(content)
                    if on_thinking:
                        on_thinking("\n\n".join(task["_thinking"]).strip())
        elif etype == "message_end":
            msg = event.get("message", {})
            if msg.get("role") == "assistant":
                task["_last_message_end"] = event
                for block in msg.get("content") or []:
                    if (isinstance(block, dict)
                            and block.get("type") == "thinking"
                            and block.get("thinking")
                            and block["thinking"] not in streamed):
                        streamed.add(block["thinking"])
                        task["_thinking"].append(block["thinking"])
    proc.wait()
    stderr = (proc.stderr.read() or "").strip()
    if proc.returncode != 0:
        raise RuntimeError(f"pi exited with {proc.returncode}: {stderr[:500]}")
    task["proc"] = None
    return extract_answer_and_thinking(task)
