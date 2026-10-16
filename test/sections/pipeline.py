"""Sections 5-8: pipeline mechanics. Ignored events (non-message, empty),
failing prompt -> retry -> done, the batch window (2 fast messages = one
pi run, merged in user order), a hanging prompt killed by the heartbeat
(pending message persisted, running entry carries the room token), and a
second bot with its own room folder.
"""
import json
import os
import time

from harness import (BOTS_TEST, FakePopen, check, new_payload,
                     signed_webhook, tasks, wait_room_done)


def run():
    # 5. non-message event (reaction) ignored
    payload = new_payload("like")
    payload["object"]["name"] = "reaction"
    s, body = signed_webhook("bot1", payload)
    check("non-message event ignored", s == 200 and body.get("ignored"),
          str(body))

    # 6. empty message ignored
    s, body = signed_webhook("bot1", new_payload("   "))
    check("empty message ignored", s == 200 and body.get("ignored"), str(body))

    # 7. failing prompt -> retried -> done (own room: no cross-talk with the
    # batch tests below)
    s, _ = signed_webhook("bot1", new_payload("make-fail",
                                              room_token="fail0001",
                                              room_name="Fail Room"))
    t = wait_room_done("bot1/rooms/fail-room", retries=1)
    check("failed run retried and done",
          bool(t) and t["status"] == "done" and t["retries"] == 1, str(t))

    # 7b. batch window: messages within the window go to pi in one run
    s, _ = signed_webhook("bot1", new_payload("batch one"))
    s, _ = signed_webhook("bot1", new_payload("batch two"))
    t = wait_room_done("bot1/rooms/assibot", min_count=3)
    check("batched messages processed", bool(t) and t["status"] == "done",
          str(t))
    batched = [p for c, p in FakePopen._seen
               if c.replace("\\", "/").endswith("bot1/rooms/assibot/workspace")
               and "batch one" in p and "batch two" in p]
    check("burst sent to pi as one prompt with both messages",
          len(batched) == 1, str([p[:60] for p in batched]))
    mine = [x for x in tasks()["tasks"]
            if x["room_cwd"].replace("\\", "/").endswith(
                "bot1/rooms/assibot")]
    check("burst produced exactly one task (not one per message)",
          len(mine) == 3, str([(x["status"]) for x in mine]))

    # 8. hanging prompt (different bot) -> heartbeat kills -> retry -> done;
    #    a second message stays queued and is persisted in the state file
    s, _ = signed_webhook("bot2", new_payload("hang", room_token="h4ngtok1",
                                              room_name="Hang Room"))
    time.sleep(1.2)  # let the batch window pass so the hang runs alone
    s, _ = signed_webhook("bot2", new_payload("follow-up please",
                                              room_token="h4ngtok1",
                                              room_name="Hang Room"))
    time.sleep(0.5)
    state_file = os.path.join(BOTS_TEST, "bot2", "rooms", "hang-room",
                              ".pigeon.json")
    try:
        state = json.load(open(state_file))
    except Exception:
        state = {}
    check("pending message persisted in state file",
          any(p.get("message") == "follow-up please"
              for p in state.get("pending", [])), str(state))
    check("running entry stores the room token (needed for resume)",
          (state.get("running") or {}).get("room") == "h4ngtok1", str(state))
    t = wait_room_done("bot2/rooms/hang-room", min_count=2, timeout=30)
    check("hang killed by heartbeat, continued, follow-up processed",
          bool(t) and t["status"] == "done", str(t))
    state = json.load(open(state_file))
    check("state file clean after tasks done",
          state.get("pending") == [] and state.get("running") is None,
          str(state))
    check("bot2 has own rooms dir",
          os.path.isdir(os.path.join(BOTS_TEST, "bot2", "rooms",
                                     "hang-room")))

    s, body = signed_webhook("bot3", new_payload("hi via bot3",
                                                 room_token="tok3nabc",
                                                 room_name="Team Bot"))
    t = wait_room_done("bot3/rooms/team-bot")
    check("bot3 gets own folder + session",
          s == 202 and bool(t) and t["status"] == "done",
          f"{s} {body} {t}")
