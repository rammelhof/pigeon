"""Section: github — !github command, token injection into the pi
process (bwrap --setenv), redaction of the token out of posted text.
"""
import json
import os

import nc_stub
from harness import (BOTS_TEST, FakePopen, check, new_payload,
                     signed_webhook, wait_room_done)

ROOM = "ghroom001"
TOKEN = "github_pat_TESTtok_0123456789abcd"
CWDSuffix = "bot1/rooms/github-room/workspace"


def posts():
    return [m for r, m in nc_stub.H.received if r == ROOM]


def cmds():
    return [cmd for c, cmd in FakePopen.cmds
            if c.replace("\\", "/").endswith(CWDSuffix)]


def run():
    stt_json = os.path.join(BOTS_TEST, ".pigeon.json")

    s, body = signed_webhook("bot1", new_payload("!github",
                                                 room_token=ROOM,
                                                 room_name="GitHub Room"))
    check("!github (status): not configured",
          s == 200 and body.get("handled") == "!github"
          and posts()[-1].startswith("GitHub: not configured"),
          str(body) + str(posts()[-1:]))

    s, body = signed_webhook("bot1", new_payload("!github tooshort",
                                                 room_token=ROOM,
                                                 room_name="GitHub Room"))
    check("!github rejects a too-short token",
          body.get("handled") == "!github"
          and "too short" in posts()[-1],
          str(posts()[-1:]))

    s, body = signed_webhook("bot1", new_payload("!github " + TOKEN,
                                                 room_token=ROOM,
                                                 room_name="GitHub Room"))
    check("!github <token> stores it in .pigeon.json + warns about history",
          body.get("handled") == "!github"
          and json.load(open(stt_json)).get("github", {}).get("token")
          == TOKEN
          and "stored" in posts()[-1] and "history" in posts()[-1],
          str(body) + str(posts()[-1:]))

    s, body = signed_webhook("bot1", new_payload("!github",
                                                 room_token=ROOM,
                                                 room_name="GitHub Room"))
    check("!github (status): shows masked token tail, never the token",
          posts()[-1] == "GitHub: configured (token \u2026abcd)."
          and TOKEN not in posts()[-1],
          str(posts()[-1:]))

    # injection: the pi command carries the token via bwrap --setenv
    s, body = signed_webhook("bot1", new_payload("hi", room_token=ROOM,
                                                 room_name="GitHub Room"))
    t = wait_room_done("bot1/rooms/github-room")
    check("github message handled by pi",
          bool(t) and t["status"] == "done", str(t))
    check("GITHUB_TOKEN + GH_TOKEN setenv'd for pi (sandbox)",
          bool(cmds()) and "GITHUB_TOKEN" in cmds()[-1]
          and "GH_TOKEN" in cmds()[-1] and TOKEN in cmds()[-1],
          str(cmds()[-1][-6:]) if cmds() else "no cmd")

    # redaction: a run whose reply contains the token is masked in chat
    s, body = signed_webhook("bot1",
                             new_payload("LEAK:" + TOKEN,
                                         room_token=ROOM,
                                         room_name="GitHub Room"))
    t = wait_room_done("bot1/rooms/github-room", min_count=2)
    check("answer containing the token is redacted before posting",
          bool(t) and t["status"] == "done"
          and any("\u2026" in m and TOKEN not in m for m in posts()[-2:])
          and all(TOKEN not in m for m in posts()),
          str(posts())[-200:])

    # clear: token gone, next pi run without it
    s, body = signed_webhook("bot1", new_payload("!github clear",
                                                 room_token=ROOM,
                                                 room_name="GitHub Room"))
    check("!github clear removes the token",
          body.get("handled") == "!github"
          and json.load(open(stt_json))["github"]["token"] == ""
          and posts()[-1] == "GitHub token removed.",
          str(posts()[-1:]))
    s, body = signed_webhook("bot1", new_payload("hi again",
                                                 room_token=ROOM,
                                                 room_name="GitHub Room"))
    wait_room_done("bot1/rooms/github-room", min_count=3)
    check("pi run without a configured token has no GITHUB_TOKEN",
          bool(cmds()) and "GITHUB_TOKEN" not in cmds()[-1],
          str(cmds()[-1][-6:]) if cmds() else "no cmd")
