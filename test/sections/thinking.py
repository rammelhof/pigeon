"""Section 4d3: the thinking quote + the reaction progress indicator.
Every room's messaging goes through the signed Talk bot API only: as the
job progresses, the bot reacts to the user's message — 👍 when the
message is received, 🤖 when pi starts, 🤔/💭 (one per thinking block
edge) while pi thinks — and clears the reaction before posting; after
the run, pi's thinking is quoted back with the bot API's silent flag,
then the answer follows as a normal bot post.
"""
import nc_stub
from harness import check, new_payload, signed_webhook, wait_room_done

E_RECV = "\U0001f44d"      # 👍 message received
E_START = "\U0001f916"     # 🤖 pi started
E_THINK_A = "\U0001f914"   # 🤔 thinking block started
E_THINK_B = "\U0001f4ad"   # 💭 thinking block finished


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

    # live progress: the reaction on the user's message (id 999 in the
    # test payload), updated by events, cleared before the answer goes
    # out:  👍 received -> 🤖 started -> 🤔 -> 💭 -> remove
    reacts = [(mid, emoji, act)
              for room, mid, emoji, act
              in nc_stub.H.bot_reactions[rea_before:]
              if room == "bubrm0001"]
    adds = [emoji for mid, emoji, act in reacts if act == "add"]
    check("thumbs-up reaction when the message is received",
          bool(reacts) and reacts[0] == ("999", E_RECV, "add"),
          str(reacts))
    check("robot reaction when pi started",
          len(adds) >= 2 and adds[1] == E_START, str(adds))
    check("reaction spins thinking face / thought balloon while pi thinks",
          E_THINK_A in adds and E_THINK_B in adds, str(adds))
    check("reaction cleared before the answer is posted",
          bool(reacts) and reacts[-1][2] == "remove"
          and reacts[-1][1] == adds[-1], str(reacts))

    # fail-soft: Talk without the reaction endpoints (404) must not break
    # the run — the indicator quietly dies, the answer is still posted
    nc_stub.H.reaction_error = 404
    rec2 = len(nc_stub.H.received)
    s, body = signed_webhook("bot1", new_payload("no reactions here",
                                                 room_token="bubrm0002",
                                                 room_name="NoReacts"))
    t = wait_room_done("bot1/rooms/noreacts")
    nc_stub.H.reaction_error = 0
    check("task done when the reaction API 404s",
          bool(t) and t["status"] == "done", str(t))
    posts2 = [m for room, m in nc_stub.H.received[rec2:]
              if room == "bubrm0002"]
    check("answer still posted when the reaction API 404s",
          bool(posts2) and "hello from fake pi" in posts2[-1],
          str(posts2))
