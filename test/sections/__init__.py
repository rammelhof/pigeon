"""Smoke-test sections, run in this order by test/smoke.py.

Each module exposes run() and uses the shared helpers from test/harness.py.
Order matters: later sections build on the state earlier ones created
(e.g. the bubble fallback check relies on the first plain message).
"""
SECTIONS = [
    "basic",      # pi-config gate, health, signature, bot name
    "message",    # webhook -> task done, bubblewrap sandbox
    "room",       # !pw, !clean, bot Join/Leave
    "commands",   # !help, !version, !stop, !mention, !schedule, burst
                  #     ack rule
    "files",      # file messages (no pw, by id, text event, bad credentials)
    "thinking",   # silent progress ack, thinking never posted
    "voice",      # STT transcription (wav + mp3)
    "ncproxy",    # /nc proxy: files + calendar, token auth
    "pipeline",   # ignored events, retry, batch window, heartbeat kill
    "restart",    # restart re-queue, stale disk job scan
    "scheduler",  # firing, time windows, start_time anchor
    "skills",     # sync_skills
]
