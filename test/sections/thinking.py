"""Section 4d3: the silent progress ack + where pi's thinking goes.
Every room's messaging goes through the signed Talk bot API only: when
pi starts, the bot posts one short SILENT ack (robot + ear, no
notification — reactions would notify the room on every change); after
the run, only the answer is posted — pi's thinking is saved to the
room folder for the !thinking command, never posted on its own. A
failed ack post must not break the run.
"""
import os

import nc_stub
from harness import (BOTS_TEST, check, new_payload, signed_webhook,
                     wait_room_done)
from pigeon.indicator import ACK


def _silents(since):
    """(message, silent) for the bot API posts since index `since` —
    received and bot_silent are appended together, so they align."""
    return [(m, s)
            for (r1, m), (r2, s) in zip(nc_stub.H.received[since:],
                                        nc_stub.H.bot_silent[since:])
            if r1 == r2]


def run():
    rec_before = len(nc_stub.H.received)
    s, body = signed_webhook("bot1", new_payload("think out loud",
                                                 room_token="bubrm0001",
                                                 room_name="Bubble Room"))
    check("thinking msg 202", s == 202, str(s))
    t = wait_room_done("bot1/rooms/bubble-room")
    check("thinking task done", bool(t) and t["status"] == "done", str(t))
    posts = [m for room, m in nc_stub.H.received[rec_before:]
             if room == "bubrm0001"]
    check("silent ack when pi starts, then the answer — nothing else "
          "(no thinking quote)",
          len(posts) == 2 and posts[0] == ACK
          and "hello from fake pi" in posts[1], str(posts))
    check("ack posted silent (no notification), answer not silent",
          [x for x in _silents(rec_before) if x[0] == ACK]
          == [(ACK, True)] and (ACK, False) not in _silents(rec_before),
          str(_silents(rec_before)))
    think_file = os.path.join(
        BOTS_TEST, "bot1", "rooms", "bubble-room", "last-thinking.md")
    check("thinking saved to the room folder instead of being posted",
          os.path.isfile(think_file)
          and "step one" in open(think_file).read(), "missing")

    # fail-soft: the ack post failing (Talk down/older version) must not
    # break the run — the ack is dropped quietly, the answer still posts
    nc_stub.H.post_fail = ACK
    rec2 = len(nc_stub.H.received)
    s, body = signed_webhook("bot1", new_payload("no ack here",
                                                 room_token="bubrm0002",
                                                 room_name="NoAck"))
    t = wait_room_done("bot1/rooms/noack")
    nc_stub.H.post_fail = ""
    check("task done when the ack post fails",
          bool(t) and t["status"] == "done", str(t))
    posts2 = [m for room, m in nc_stub.H.received[rec2:]
              if room == "bubrm0002"]
    check("answer still posted when the ack post fails",
          posts2 == ["hello from fake pi"], str(posts2))
