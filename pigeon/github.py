"""GitHub access for the github skill.

The token is a fine-grained PAT (machine user, repo-scoped, expiring)
stored in the "github" section of .pigeon.json in the bots folder — the
same file as the STT config. Pigeon injects it as GITHUB_TOKEN / GH_TOKEN
into every pi process (sandbox: bwrap --setenv, otherwise subprocess
env), so the skill authenticates with `gh` / curl without the token ever
being written into the room workspace.

redact() masks every configured secret out of text that leaves the
sandbox (chat posts, logs) — the model may echo the token, the room may
not.
"""

import json
import os

from .config import log
from .stt import PIGEON_JSON

MASK = "\u2026"  # …


def _read():
    try:
        with open(PIGEON_JSON) as fh:
            return json.load(fh) or {}
    except (OSError, ValueError):
        return {}


def _write(data):
    os.makedirs(os.path.dirname(PIGEON_JSON), exist_ok=True)
    with open(PIGEON_JSON, "w") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")


def github_token():
    """The configured GitHub token, or None."""
    tok = str((_read().get("github") or {}).get("token") or "").strip()
    return tok or None


def github_status():
    """The !github status line (never contains the token itself)."""
    tok = github_token()
    if tok:
        return "GitHub: configured (token {}{}).".format(MASK, tok[-4:])
    return ("GitHub: not configured. Send !github <fine-grained-PAT> "
            "(in a room where only you are) or set github.token in "
            ".pigeon.json.")


def github_set(token):
    data = _read()
    data["github"] = {"token": token}
    _write(data)
    log.info("github token stored in %s", PIGEON_JSON)


def github_clear():
    data = _read()
    if "github" in data:
        data["github"] = {"token": ""}
        _write(data)
        log.info("github token removed from %s", PIGEON_JSON)


def secrets():
    """Every configured secret in .pigeon.json — for redact()."""
    data = _read()
    out = []
    tok = str((data.get("github") or {}).get("token") or "").strip()
    if tok:
        out.append(tok)
    key = str((data.get("transcription") or {}).get("api_key")
              or "").strip()
    if key:
        out.append(key)
    return out


def redact(text):
    """Mask every configured secret in text (chat posts, logs)."""
    for s in secrets():
        if s in text:
            text = text.replace(s, MASK)
    return text
