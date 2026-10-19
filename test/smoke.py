"""Local smoke test: fake pi + fake Nextcloud (no docker needed).

Run:  python test/smoke.py
Covers: pi-config gate, HMAC webhook verification, payload parsing,
queueing, pi JSONL parsing (thinking + answer), posting via the bot API
(with outgoing signature verification), retry, heartbeat timeout kill,
!pw, !clean (session + workspace wipe), bot Join/Leave webhooks, file +
voice (STT) messages, the /nc proxy, batch window, restart recovery,
scheduler, skills sync.

The sections live in test/sections/ (one module per topic) and share
test/harness.py (fakes + helpers). Section order matters: later sections
build on the state earlier ones created.
"""
import importlib
import sys

import harness  # noqa: F401  (importing starts fakes + the app under test)
from sections import SECTIONS  # noqa: E402


for name in SECTIONS:
    importlib.import_module(f"sections.{name}").run()

print()
if harness.failures:
    print("FAILED:", harness.failures)
    sys.exit(1)
print("ALL TESTS PASSED")
harness.cleanup()
