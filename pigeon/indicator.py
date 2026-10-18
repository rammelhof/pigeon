"""Live progress indicator: a bot reaction on the user's chat message
that is updated as the job progresses — event-driven, not timer-driven:

    webhook message received   -> thumbs up
    pi agent started           -> robot
    pi thinking (per block)    -> thinking face / thought balloon
    run finished               -> reaction removed, result is posted

All Talk API calls happen in one small thread fed by a queue, so the
worker and the pi stream never block on HTTP. Any API error (e.g. an
older Talk without the bot reaction endpoints) disables the indicator
for good — the run itself is never affected.
"""

import queue
import threading

from .config import log
from .nextcloud import react, unreact

EMOJI_RECEIVED = "\U0001f44d"  # 👍 message received
EMOJI_STARTED = "\U0001f916"   # 🤖 pi agent started
EMOJI_THINK_START = "\U0001f914"  # 🤔 a thinking block started
EMOJI_THINK_END = "\U0001f4ad"    # 💭 a thinking block finished


class Indicator:
    """One per incoming chat message. Feed it events from any thread;
    the Talk API calls are applied in order by the indicator thread."""

    def __init__(self, room, message_id):
        self._room = room
        self._mid = str(message_id)
        self._q = queue.Queue()
        t = threading.Thread(target=self._run, daemon=True,
                             name=f"indicator-{self._mid}")
        self._thread = t
        t.start()

    # -- events (safe from any thread) ---------------------------------

    def received(self):
        """The webhook accepted the user's message."""
        self._post(EMOJI_RECEIVED)

    def started(self):
        """pi (re)started for this job — also used before a retry."""
        self._post(EMOJI_STARTED)

    def thinking(self, phase):
        """pi streamed a thinking block edge: 'start' or 'end'."""
        self._post(EMOJI_THINK_START if phase == "start"
                   else EMOJI_THINK_END)

    def stop(self):
        """Clear the reaction and shut the thread down. Idempotent."""
        self._q.put(None)
        self._thread.join(timeout=5)

    # -- the thread ------------------------------------------------------

    def _post(self, emoji):
        self._q.put(emoji)

    def _run(self):
        current = None
        while True:
            emoji = self._q.get()
            if emoji is None:
                break
            if emoji == current:
                continue
            try:
                if current is not None:
                    unreact(self._room, self._mid, current)
                react(self._room, self._mid, emoji)
                current = emoji
            except Exception as e:
                log.debug("indicator %s stopped: %s", self._mid, e)
                return
        if current is not None:
            try:
                unreact(self._room, self._mid, current)
            except Exception as e:
                log.debug("indicator %s: could not remove %s",
                          self._mid, current)
