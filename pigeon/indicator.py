"""Live progress indicator: a short SILENT chat message that tells the
room the bot heard the message and pi is working on it.

One emoji pair — robot + ear (🤖 👂) — posted through the bot API with
the ``silent`` flag, so it reaches the chat without triggering a
notification. Posted once when pi (re)starts for a job; it is never
removed or edited. (Reactions were used before, but every reaction
change reaches the room as a notification.)

The Talk API call happens in a small thread so the worker never blocks
on HTTP; any API error is logged and dropped — the run itself is never
affected.
"""

import threading

from .config import log
from .nextcloud import post_message

# robot + ear: "the bot heard it and is working on it"
ACK = "\U0001f916 \U0001f442"


class Indicator:
    """One per incoming chat message. Fire ``started()`` from any
    thread (including per retry); the ack is posted at most once,
    asynchronously."""

    def __init__(self, room):
        self._room = room
        self._fired = False

    def started(self):
        """pi started (or was restarted for a retry)."""
        if self._fired:
            return
        self._fired = True
        threading.Thread(target=self._post, daemon=True,
                         name="indicator").start()

    def stop(self):
        """Shut down. Idempotent no-op: the ack stays in the chat."""

    def _post(self):
        try:
            post_message(self._room, ACK, silent=True)
        except Exception as e:
            log.debug("indicator ack could not be posted: %s", e)
