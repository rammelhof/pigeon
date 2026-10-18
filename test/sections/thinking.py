"""Section 4d3: the thinking quote. Every room's messaging goes through
the signed Talk bot API only: after the run, pi's thinking is quoted back
into the room with the bot API's silent flag (no chat notification), and
the answer follows as a normal bot post.
"""
import nc_stub
from harness import check, new_payload, signed_webhook, wait_room_done


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
    quote = next((m for m in posts if "\U0001f914" in m), None)
    check("thinking quote posted via the bot API",
          quote is not None and "> step one" in quote
          and "> step two" in quote,
          str(posts))
    check("thinking quote is silent (bot API silent flag)",
          any(tok == "bubrm0001" and sil
              for tok, sil in nc_stub.H.bot_silent[rec_before:]),
          str(nc_stub.H.bot_silent[rec_before:]))
    check("answer posted after the thinking quote",
          len(posts) == 2 and "hello from fake pi" in posts[-1],
          str(posts))
