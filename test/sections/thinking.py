"""Section 4d3: the live thinking bubble. A room WITH an app password gets
its thinking as ONE message (posted as the app-password user, since the bot
API cannot edit) that is edited in place while pi thinks; the answer still
goes out via the bot API. Rooms without credentials keep the classic
one-shot thinking quote (already present from the first plain message).
"""
import nc_stub
from harness import check, new_payload, signed_webhook, wait_room_done


def run():
    s, body = signed_webhook("bot1", new_payload("/pw ncuser ncpw123",
                                                 room_token="bubrm0001",
                                                 room_name="Bubble Room"))
    check("bubble setup: /pw stored", s == 200 and body.get("handled") == "/pw",
          str(body))
    sent_before = len(nc_stub.H.chat_sent)
    s, body = signed_webhook("bot1", new_payload("think out loud",
                                                 room_token="bubrm0001",
                                                 room_name="Bubble Room"))
    check("bubble msg 202", s == 202, str(s))
    t = wait_room_done("bot1/rooms/bubble-room")
    check("bubble task done", bool(t) and t["status"] == "done", str(t))
    bub_lookups = [c for c in nc_stub.H.chat_lookups if c[0] == "bubrm0001"]
    check("bubble: room resolved by token with the room credentials",
          bub_lookups and bub_lookups[-1][1] == "ncuser",
          str(nc_stub.H.chat_lookups))
    sent = nc_stub.H.chat_sent[sent_before:]
    check("bubble: exactly one initial 'thinking…' message (no spam)",
          len(sent) == 1
          and sent[0] == (nc_stub.H.chat_room_id, "\U0001f914 *thinking\u2026*"),
          str(sent))
    edits = nc_stub.H.chat_edits
    check("bubble: edited in place with the streamed thinking",
          bool(edits) and all(e[2].startswith("\U0001f914 ") for e in edits)
          and any("> step one" in e[2] and "> step two" in e[2]
                  for e in edits),
          str(edits)[:300])
    check("bubble: final edit carries the full thinking",
          bool(edits) and "> step one" in edits[-1][2]
          and "> step two" in edits[-1][2],
          str(edits[-1][2])[:200] if edits else "")
    check("bubble: the answer still goes out via the bot API",
          any(room == "bubrm0001" and "hello from fake pi" in m
              for room, m in nc_stub.H.received))
    check("bubble: no thinking quote posted via bot API for the credentialed room",
          not any(room == "bubrm0001" and "\U0001f914" in m
                  for room, m in nc_stub.H.received))
    check("bubble fallback: room without app password keeps the classic thinking quote",
          any(room == "ho5w45n9" and "\U0001f914" in m and "step one" in m
              for room, m in nc_stub.H.received)
          and all(tok != "ho5w45n9" for tok, _ in nc_stub.H.chat_lookups),
          str(nc_stub.H.chat_lookups))
