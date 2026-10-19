"""Sections 4c + 4d + 4d2 + 4e: file messages. Without an app password pi is
instructed in user language (no download); with credentials the file is
downloaded by file id into chat/files and the local path replaces {file};
Talk's plain text event (file param, no {file} token) gets the path in front
of the text; wrong credentials -> failure hint with the share link, but the
task still runs."""
import os

import nc_stub
from harness import (BOTS_TEST, FakePopen, check, file_payload, new_payload,
                     signed_webhook, wait_prompt, wait_room_done)


def run():
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
          any("app password" in p and "!pw <nextcloud-user> <app-password>" in p
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
    fd = os.path.join(BOTS_TEST, "bot1", "rooms", "pw-room", "workspace",
                      "chat", "files")
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
    prompt = wait_prompt("I send 2 test files.",
                         "bot1/rooms/pw-room/workspace")
    check("file path added in front of the text message",
          prompt is not None and saved2[0] in prompt
          and prompt.index("chat/files/") < prompt.index("I send 2 test files."),
          str(prompt)[:200])

    # 4e. wrong credentials -> failure hint + share link, task still runs
    signed_webhook("bot1", new_payload("!pw ncuser WRONGpw",
                                       room_token="pwroom0001",
                                       room_name="Pw Room"))
    s, body = signed_webhook("bot1", file_payload("look at {file}",
                                                  "pwroom0001", "Pw Room"))
    check("file msg with wrong password 202", s == 202, str(s))
    t = wait_room_done("bot1/rooms/pw-room", min_count=3)  # 4d + 4d2 + 4e
    check("failed-download task done", bool(t) and t["status"] == "done",
          str(t))
    prompt = wait_prompt("download failed (dav search",
                         "bot1/rooms/pw-room/workspace")
    check("wrong password -> failure hint with share link (pi was prompted)",
          prompt is not None and "virtual-link" in prompt, str(prompt))
