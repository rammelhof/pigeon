"""Sections 4b + 4b2 + 4b3 + 4b4: /pw (per-room app password), /clean
(+ /c, session + workspace wipe), /thinking (+ /t, post pi's last
saved thinking), and the bot Join/Leave webhooks (greet / total wipe)."""
import json
import os

import nc_stub
from pigeon.pi import session_dir
from harness import (BOTS_TEST, check, join_leave_payload, new_payload,
                     signed_webhook, wait_room_done)


def run():
    # 4b. !pw room command (per-room app password for file downloads)
    nc_stub.H.share_auth = {"ncuser": "ncpw123"}

    s, body = signed_webhook("bot1", new_payload("!pw",
                                                 room_token="pwroom0001",
                                                 room_name="Pw Room"))
    check("!pw status: handled, not queued",
          s == 200 and body.get("handled") == "!pw", str(body))
    pw_cwd = os.path.join(BOTS_TEST, "bot1", "rooms", "pw-room")
    check("!pw does not create a task/state file",
          not os.path.exists(os.path.join(pw_cwd, ".pigeon.json")))
    posted = [m for room, m in nc_stub.H.received if room == "pwroom0001"]
    check("!pw status reply: not configured",
          any("not configured" in m for m in posted), str(posted)[:200])

    s, body = signed_webhook("bot1",
                             new_payload("!pw ncuser ncpw123",
                                         room_token="pwroom0001",
                                         room_name="Pw Room"))
    check("!pw set: handled", s == 200 and body.get("handled") == "!pw",
          str(body))
    meta = json.load(open(os.path.join(pw_cwd, ".room.json")))
    check("!pw credentials stored per room (token kept)",
          meta.get("nextcloud_user") == "ncuser"
          and meta.get("nextcloud_password") == "ncpw123"
          and meta.get("token") == "pwroom0001", str(meta))
    posted = [m for room, m in nc_stub.H.received if room == "pwroom0001"]
    check("!pw set reply posted without echoing the password",
          any("saved" in m and "ncpw123" not in m for m in posted),
          str(posted)[:200])
    signed_webhook("bot1", new_payload("!pw", room_token="pwroom0001",
                                       room_name="Pw Room"))
    posted = [m for room, m in nc_stub.H.received if room == "pwroom0001"]
    check("!pw status reply: configured",
          any("configured for user: ncuser" in m for m in posted),
          str(posted)[:200])

    # 4b2. !clean room command: drops the pi sessions AND wipes the
    # workspace, so the next message starts a fresh pi session
    clean_ws = os.path.join(BOTS_TEST, "bot1", "rooms", "clean-room",
                            "workspace")
    sdir = session_dir(clean_ws)
    os.makedirs(sdir, exist_ok=True)
    old_session = os.path.join(
        sdir, "2026-01-01T00-00-00Z_bot1_clean-room.jsonl")
    with open(old_session, "w") as f:
        f.write('{"type":"session","version":3}\n')
    os.makedirs(clean_ws, exist_ok=True)
    with open(os.path.join(clean_ws, "leftover.txt"), "w") as f:
        f.write("junk")
    s, body = signed_webhook("bot1", new_payload("!clean",
                                                 room_token="cleanrm0001",
                                                 room_name="Clean Room"))
    check("!clean: handled, not queued",
          s == 200 and body.get("handled") == "!clean", str(body))
    check("!clean deleted the old pi session file",
          not os.path.exists(old_session) and os.path.isdir(sdir))
    check("!clean wiped the workspace dir", not os.path.exists(clean_ws))
    posted = [m for r, m in nc_stub.H.received if r == "cleanrm0001"]
    check("!clean reply posted",
          any("cleared" in m and "wiped the workspace" in m for m in posted),
          str(posted)[:200])
    s, body = signed_webhook("bot1", new_payload("!clean",
                                                 room_token="cleanrm0001",
                                                 room_name="Clean Room"))
    posted = [m for r, m in nc_stub.H.received if r == "cleanrm0001"]
    check("!clean twice: nothing to clear",
          body.get("handled") == "!clean" and any("nothing to clear" in m
                                                  for m in posted),
          str(posted)[:200])
    s, body = signed_webhook("bot1", new_payload("!c",
                                                 room_token="cleanrm0001",
                                                 room_name="Clean Room"))
    check("!c alias: handled like !clean",
          body.get("handled") == "!clean", str(body))

    # 4b3. bot join/leave webhooks: one-time greeting + total clean on leave
    greet_room_dir = os.path.join(BOTS_TEST, "bot1", "rooms", "greet-room")
    s, body = signed_webhook("bot1",
                             join_leave_payload("greetrm0001", "Greet Room"))
    check("Join: greeted", s == 200 and body.get("handled") == "greet",
          str(body))
    posted = [m for r, m in nc_stub.H.received if r == "greetrm0001"]
    check("Join: greeting posted (with the bot's Talk name)",
          len(posted) == 1 and "I'm Pigeon" in posted[0]
          and "Greet Room" in posted[0]
          and "@Pigeon" in posted[0] and "@bot" in posted[0]
          and "!thinking" in posted[0] and "!clean" in posted[0],
          str(posted)[:200])
    with open(os.path.join(greet_room_dir, ".room.json")) as f:
        meta = json.load(f)
    check("Join: greeted flag + bot name stored in .room.json",
          meta.get("greeted") is True and meta.get("bot_name") == "Pigeon",
          str(meta))
    s, body = signed_webhook("bot1",
                             join_leave_payload("greetrm0001", "Greet Room"))
    posted = [m for r, m in nc_stub.H.received if r == "greetrm0001"]
    check("Join twice: ignored, no second greeting",
          s == 200 and body.get("ignored") == "already greeted"
          and len(posted) == 1,
          f"{body} {posted[:1]}")

    # Leave: room with stored password + workspace file + pi session -> wiped
    leave_room_dir = os.path.join(BOTS_TEST, "bot1", "rooms", "leave-room")
    leave_ws = os.path.join(leave_room_dir, "workspace")
    os.makedirs(leave_ws, exist_ok=True)
    with open(os.path.join(leave_ws, "leftover.txt"), "w") as f:
        f.write("junk")
    s, body = signed_webhook("bot1",
                             new_payload("!pw ncuser pw12345",
                                         room_token="leaverm0001",
                                         room_name="Leave Room"))
    check("Leave setup: /pw stored",
          s == 200 and body.get("handled") == "!pw", str(body))
    leave_sdir = session_dir(leave_ws)
    os.makedirs(leave_sdir, exist_ok=True)
    old_leave_session = os.path.join(
        leave_sdir, "2026-01-02T00-00-00Z_bot1_leave-room.jsonl")
    with open(old_leave_session, "w") as f:
        f.write('{"type":"session","version":3}\n')
    posts_before = sum(1 for r, m in nc_stub.H.received if r == "leaverm0001")
    s, body = signed_webhook("bot1",
                             join_leave_payload("leaverm0001", "Leave Room",
                                                join=False))
    check("Leave: wiped", s == 200 and body.get("handled") == "wiped",
          str(body))
    check("Leave: whole room folder gone",
          not os.path.exists(leave_room_dir))
    check("Leave: pi session file gone", not os.path.exists(old_leave_session))
    check("Leave: nothing posted (bot is out of the room)",
          sum(1 for r, m in nc_stub.H.received if r == "leaverm0001")
          == posts_before)
    # Leave on a room we never heard of: no-op, still 200
    s, body = signed_webhook("bot1",
                             join_leave_payload("ghostrm001", "Ghost Room",
                                                join=False))
    check("Leave on unknown room: ok, no crash",
          s == 200 and body.get("handled") == "wiped", str(body))

    # 4b3b. two different rooms with the same display name -> separate
    # folders (the second gets the token prefix), no shared state
    s, body = signed_webhook("bot1", join_leave_payload("aaa11111", "Dup Room"))
    dup_plain = os.path.join(BOTS_TEST, "bot1", "rooms", "dup-room")
    check("dup room: first join uses the plain name folder",
          s == 200 and body.get("handled") == "greet"
          and json.load(open(os.path.join(dup_plain, ".room.json")))
          .get("token") == "aaa11111", str(body))
    s, body = signed_webhook("bot1", join_leave_payload("bbb22222", "Dup Room"))
    dup_other = os.path.join(BOTS_TEST, "bot1", "rooms", "dup-room-bbb22222")
    check("dup room: second token gets its own folder (token prefix)",
          s == 200 and body.get("handled") == "greet"
          and json.load(open(os.path.join(dup_other, ".room.json")))
          .get("token") == "bbb22222", str(body))
    s, body = signed_webhook("bot1",
                             new_payload("hi from the duplicate",
                                         room_token="bbb22222",
                                         room_name="Dup Room"))
    t = wait_room_done("bot1/rooms/dup-room-bbb22222")
    check("dup room: duplicate's message runs in its own workspace",
          s == 202 and bool(t) and t["status"] == "done", str(t))
    s, body = signed_webhook("bot1",
                             new_payload("hi from the original",
                                         room_token="aaa11111",
                                         room_name="Dup Room"))
    t = wait_room_done("bot1/rooms/dup-room")
    check("dup room: original still uses the plain folder",
          s == 202 and bool(t) and t["status"] == "done", str(t))
    meta = json.load(open(os.path.join(dup_plain, ".room.json")))
    check("dup room: original's stored token untouched",
          meta.get("token") == "aaa11111", str(meta))

    # 4b4. !thinking (+ /t): pi's last thinking is saved to the room
    # folder and posted back to the chat
    think_cwd = os.path.join(BOTS_TEST, "bot1", "rooms", "think-room")
    s, body = signed_webhook("bot1", new_payload("think out loud",
                                                 room_token="thinkrm0001",
                                                 room_name="Think Room"))
    check("!thinking setup: queued (202)", s == 202, str(s))
    t = wait_room_done("bot1/rooms/think-room")
    check("!thinking setup: task done", bool(t) and t["status"] == "done",
          str(t))
    thinking_file = os.path.join(think_cwd, "last-thinking.md")
    check("thinking saved to the room folder",
          os.path.isfile(thinking_file)
          and "step one" in open(thinking_file).read(), "missing")
    rec_before = len(nc_stub.H.received)
    s, body = signed_webhook("bot1", new_payload("!thinking",
                                                 room_token="thinkrm0001",
                                                 room_name="Think Room"))
    check("!thinking: handled, not queued",
          s == 200 and body.get("handled") == "!thinking", str(body))
    posted = [m for r, m in nc_stub.H.received[rec_before:]
              if r == "thinkrm0001"]
    check("!thinking: last thinking posted as a quote",
          any("\U0001f914" in m and "> step one" in m for m in posted),
          str(posted)[:200])
    rec_before = len(nc_stub.H.received)
    s, body = signed_webhook("bot1", new_payload("!t",
                                                 room_token="thinkrm0001",
                                                 room_name="Think Room"))
    check("!t alias: handled like !thinking",
          s == 200 and body.get("handled") == "!thinking", str(body))
    posted = [m for r, m in nc_stub.H.received[rec_before:]
              if r == "thinkrm0001"]
    check("!t alias: same reply",
          any("> step one" in m for m in posted), str(posted)[:200])
    rec_before = len(nc_stub.H.received)
    s, body = signed_webhook("bot1", new_payload("!t",
                                                 room_token="emptyrm001",
                                                 room_name="Empty Room"))
    posted = [m for r, m in nc_stub.H.received[rec_before:]
              if r == "emptyrm001"]
    check("!thinking on a fresh room: nothing saved yet",
          s == 200 and body.get("handled") == "!thinking"
          and any("No thinking saved" in m for m in posted),
          str(posted)[:200])
    s, body = signed_webhook("bot1", new_payload("!clean",
                                                 room_token="thinkrm0001",
                                                 room_name="Think Room"))
    check("!clean drops the saved thinking too",
          body.get("handled") == "!clean"
          and not os.path.isfile(thinking_file), str(body))
