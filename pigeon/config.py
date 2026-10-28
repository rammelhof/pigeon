"""Configuration: environment variables + logging (single source of truth)."""

import logging
import os

# build/version info (PIGEON_COMMIT is set by the Dockerfile build arg)
VERSION = "1.0.19"
COMMIT = os.environ.get("PIGEON_COMMIT", "unknown")

NEXTCLOUD_URL = os.environ.get("NEXTCLOUD_URL", "").rstrip("/")
# Secret given to `occ talk:bot:install <name> <secret> <url> ...`
# (40-128 chars). Verifies incoming webhooks and signs outgoing API calls.
TALK_BOT_SECRET = os.environ.get("TALK_BOT_SECRET", "")

BOTS_DIR = os.environ.get("BOTS_DIR", "./bots")
MODES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "modes")
DEFAULT_MODE = "office"  # persona for rooms that never set one
PI_BIN = os.environ.get("PI_BIN", "pi")
PI_CONFIG_DIR = os.environ.get(
    "PI_CODING_AGENT_DIR",
    os.path.join(os.path.expanduser("~"), ".pi", "agent"))

PI_TIMEOUT = float(os.environ.get("PI_TIMEOUT", "600"))           # seconds
HEARTBEAT_INTERVAL = float(os.environ.get("HEARTBEAT_INTERVAL", "30"))
# scan state files for stale jobs every Nth heartbeat (crash self-healing)
STATE_SCAN_EVERY = int(os.environ.get("STATE_SCAN_EVERY", "10"))
# how often the scheduler checks schedules.json files (seconds)
SCHEDULER_INTERVAL = float(os.environ.get("SCHEDULER_INTERVAL", "30"))
MAX_RETRIES = int(os.environ.get("MAX_RETRIES", "1"))
# Incoming-message batching: after a chat message, the room worker waits up
# to this many seconds for more messages from the same room and sends the
# whole burst to pi in a single run (0 = off, immediate). Scheduled
# prompts are never delayed/batched.
MESSAGE_BATCH_WINDOW = float(os.environ.get("MESSAGE_BATCH_WINDOW", "5"))
# Run every pi invocation in a bubblewrap sandbox: read-only root, only
# the room's workspace/ and PI_CODING_AGENT_DIR writable, fresh PID/IPC/
# UTS namespaces, full network access (0 = off). There is NO fallback:
# if SANDBOX is on but bwrap is missing or cannot start, pi runs fail.
SANDBOX = os.environ.get("SANDBOX", "1") == "1"
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
# max size in MB of file attachments downloaded into the room's
# workspace/chat/files/
MAX_FILE_SIZE_MB = float(os.environ.get("MAX_FILE_SIZE_MB", "100"))
# bundled skills shipped in the image, synced into the pi home at startup
SKILLS_SRC = os.environ.get("SKILLS_SRC", "/app/skills")

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
log = logging.getLogger("pigeon")
