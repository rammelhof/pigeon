"""Sections 0-3: pi-config gate, health output, HMAC signature, bot name."""
import json
import os

from harness import PI_CFG, check, get, new_payload, signed_webhook


def run():
    # 0. pi not configured -> webhook 503 + health degraded
    auth_file = os.path.join(PI_CFG, "auth.json")
    os.path.exists(auth_file) and os.remove(auth_file)
    s, h = get("/health")
    check("health 503 while pi unconfigured", s == 503, str(s))
    check("health reports pi unconfigured",
          h.get("pi", {}).get("configured") is False)
    s, body = signed_webhook("bot1", new_payload("hi"))
    check("webhook 503 while pi unconfigured",
          s == 503 and "configured" in body.get("error", ""), str(s))

    # now configure pi
    json.dump({"fake": "credential"}, open(auth_file, "w"))

    # 1. health ok
    s, h = get("/health")
    check("health 200 after config", s == 200, str(s))
    check("health shape", h.get("status") == "ok"
          and h.get("pi", {}).get("configured") is True
          and "heartbeat" in h and "tasks" in h)
    check("health reports version + commit",
          h.get("version") == "1.0.0" and "commit" in h,
          str(h.get("version")))
    s, idx = get("/")
    check("index reports version + commit",
          idx.get("version") == "1.0.0" and "commit" in idx, str(idx))

    # 2. bad signature
    s, body = signed_webhook("bot1", new_payload("hi"), secret="y" * 64)
    check("bad signature 403", s == 403, str(s))

    # 3. invalid bot name
    s, _ = signed_webhook("bad..name", new_payload("hi"))
    check("invalid bot name 400", s == 400, str(s))
