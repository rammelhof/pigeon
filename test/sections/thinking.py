"""Section 4d3: the thinking quote + the reaction progress indicator.
Every room's messaging goes through the signed Talk bot API only: while
pi runs, the bot reacts to the user's message and cycles the emoji every
REACTION_EVERY seconds (live progress, no extra messages); after the run,
pi's thinking is quoted back with the bot API's silent flag, then the
answer follows as a normal bot post.
"""
import nc_stub
from harness import check, new_payload, signed_webhook, wait_room_done


def run():
    rec_before = len(nc_stub.H.received)
    rea_before = len(nc_stub.H.bot_reactions)
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

    # live progress: reaction on the user's message (id 999 in the test
    # payload), cycling emoji while pi ran, removed after the answer
    reacts = [(mid, emoji, act)
              for room, mid, emoji, act
              in nc_stub.H.bot_reactions[rea_before:]
              if room == "bubrm0001"]
    adds = [emoji for mid, emoji, act in reacts if act == "add"]
    check("reaction added on the user's message (starts with thinking)",
          bool(reacts) and reacts[0][0] == "999" and adds[0] == "\U0001f914",
          str(reacts))
    check("reaction cycled emojis while pi ran",
          len(set(adds)) >= 2, str(adds))
    check("reaction removed after the answer",
          bool(reacts) and reacts[-1][2] == "remove"
          and reacts[-1][1] == adds[-1], str(reacts))

    # fail-soft: Talk without the reaction endpoints (404) must not break
    # the run — the answer is still posted, no crash
    nc_stub.H.reaction_error = 404
    rec2 = len(nc_stub.H.received)
    s, body = signed_webhook("bot1", new_payload("no reactions here",
                                                 room_token="bubrm0002",
                                                 room_name="NoReacts"))
    t = wait_room_done("bot1/rooms/noreacts")
    nc_stub.H.reaction_error = 0
    check("task done when the reaction API 404s",
          bool(t) and t["status"] == "done", str(t))
    check("answer still posted when the reaction API 404s",
          any(room == "bubrm0002" and "hello from fake pi" in m
              for room, m in nc_stub.H.received[rec2:]),
          str(nc_stub.H.received[rec2:]))
