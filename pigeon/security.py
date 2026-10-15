"""Signing: webhook signature verification, outgoing bot API signatures,
and rolling 24h room-scoped tokens for the /nc Nextcloud proxy."""

import hashlib
import hmac
import os
import time

from .config import TALK_BOT_SECRET


def verify_webhook_signature(secret, random_str, signature, raw_body):
    """Verify the X-Nextcloud-Talk-Signature header of an incoming webhook."""
    expected = hmac.new(secret.encode(),
                        ((random_str or "") + raw_body).encode(),
                        hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature or "")


def sign_request(secret, body):
    """Headers for bot API calls (random + HMAC-SHA256(random+body, secret))."""
    random_str = os.urandom(32).hex()
    sig = hmac.new(secret.encode(),
                   (random_str + body).encode(), hashlib.sha256).hexdigest()
    return {
        "X-Nextcloud-Talk-Bot-Random": random_str,
        "X-Nextcloud-Talk-Bot-Signature": sig,
        "OCS-APIRequest": "true",
        "Content-Type": "application/json",
    }


# ----------------------------------------------------------------- /nc ----
# Room-scoped proxy token: HMAC of the webhook secret over a rolling 24h
# window — no storage, and a leaked token dies within a day.

TOKEN_TTL_H = 24


def nc_token(bot, room, hour=None):
    h = int(time.time() // 3600) if hour is None else hour
    return hashlib.sha256(
        (f"{TALK_BOT_SECRET}\x00nc:{bot}:{room}:{h}").encode()).hexdigest()[:16]


def nc_verify(token, bot, room):
    """Accept the current and the previous hour's token."""
    now = int(time.time() // 3600)
    return token in (nc_token(bot, room, now),
                     nc_token(bot, room, now - 1))
