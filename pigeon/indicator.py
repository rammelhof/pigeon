"""Live progress indicator: a bot reaction on the user's chat message.
Event-driven, add-only:

    pi agent started           -> robot (and then it stays)

Nothing else: no reaction when the webhook receives the message,
nothing while pi thinks, nothing when the run finishes. Reactions are
only ever set, never removed — any reaction change reaches the room
as a notification, so the indicator stays as quiet as possible.

The Talk API call happens in one small thread fed by a queue, so the
worker never blocks on HTTP. Any API error (e.g. an older Talk without
the bot reaction endpoints) disables the indicator for good — the run
itself is never affected.
"""

import queue
import threading

from .config import log
from .nextcloud import react

EMOJI_STARTED = "\U0001f916"  # 🤖 pi agent started


class Indicator:
    """One per incoming chat message. Feed it events from any thread;
    the Talk API call is applied in order by the indicator thread."""

    def __init__(self, room, message_id):
        self._room = room
        self._mid = str(message_id)
        self._q = queue.Queue()
        t = threading.Thread(target=self._run, daemon=True,
                             name=f"indicator-{self._mid}")
        self._thread = t
        t.start()

    # -- events (safe from any thread) ---------------------------------

    def started(self):
        """pi (re)started for this job — also used before a retry."""
        self._post(EMOJI_STARTED)

    def stop(self):
        """Shut the thread down. Idempotent. The reaction is KEPT —
        removing it would send a notification into the room."""
        self._q.put(None)
        self._thread.join(timeout=5)

    # -- the thread ------------------------------------------------------

    def _post(self, emoji):
        self._q.put(emoji)

    def _run(self):
        last = None
        while True:
            emoji = self._q.get()
            if emoji is None:
                break
            if emoji == last:
                continue
            try:
                # add-only: Talk keeps one row per (actor, message,
                # emoji), so no replacement, no removal, no extra
                # notifications.
                react(self._room, self._mid, emoji)
                last = emoji
            except Exception as e:
                log.debug("indicator %s stopped: %s", self._mid, e)
                return
