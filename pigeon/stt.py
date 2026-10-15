"""Speech-to-text for voice messages.

Talk delivers voice messages as ``audio/wav`` or ``audio/mpeg`` file
attachments (``*.wav`` / ``*.mp3``). pigeon downloads them (WebDAV SEARCH
+ GET, like any other file) and sends them
to an OpenAI-compatible transcription endpoint
(``POST {endpoint}/v1/audio/transcriptions``, multipart with ``model`` and
``file``). The endpoint, model and API key live in ``.pigeon.json`` in
the bots root folder (created as an empty boilerplate at startup when
missing):

    {
      "transcription": {
        "endpoint": "https://ai.example.com",
        "model": "STT",
        "api_key": "..."
      }
    }

The endpoint is the base URL — pigeon appends ``/v1/audio/transcriptions``.
"""

import json
import os

import requests

from .config import BOTS_DIR, log

PIGEON_JSON = os.path.join(BOTS_DIR, ".pigeon.json")
TIMEOUT = 120  # seconds for the whole transcription request

BOILERPLATE = {
    "transcription": {
        "endpoint": "",
        "model": "",
        "api_key": "",
    },
}


def ensure_config_file():
    """Create .pigeon.json as an empty boilerplate if it does not exist.
    Returns True when the file was created."""
    if os.path.isfile(PIGEON_JSON):
        return False
    os.makedirs(BOTS_DIR, exist_ok=True)
    with open(PIGEON_JSON, "w") as fh:
        json.dump(BOILERPLATE, fh, indent=2)
        fh.write("\n")
    log.info("created %s — fill in the transcription section", PIGEON_JSON)
    return True


def transcription_config():
    """The complete transcription config, or None when any of endpoint /
    model / api_key is missing or the file is unreadable."""
    try:
        with open(PIGEON_JSON) as fh:
            cfg = (json.load(fh) or {}).get("transcription") or {}
        if not isinstance(cfg, dict):
            return None
    except (OSError, ValueError) as e:
        log.warning("cannot read %s: %s", PIGEON_JSON, e)
        return None
    endpoint = str(cfg.get("endpoint") or "").strip().rstrip("/")
    model = str(cfg.get("model") or "").strip()
    api_key = str(cfg.get("api_key") or "").strip()
    if not (endpoint and model and api_key):
        return None
    return {"endpoint": endpoint, "model": model, "api_key": api_key}


def is_voice_file(f):
    """True for Talk voice messages: *.wav / *.mp3 attachments."""
    if not isinstance(f, dict):
        return False
    name = str(f.get("name") or "").lower()
    mime = str(f.get("mimetype") or "").lower()
    return (name.endswith((".wav", ".mp3"))
            or mime in ("audio/wav", "audio/x-wav", "audio/mpeg"))


def transcribe(cfg, wav_path):
    """Transcribe one wav file. Returns the extracted text.
    Raises RuntimeError on HTTP/decode failures or empty results."""
    url = cfg["endpoint"] + "/v1/audio/transcriptions"
    with open(wav_path, "rb") as fh:
        r = requests.post(
            url,
            data={"model": cfg["model"]},
            files={"file": (os.path.basename(wav_path), fh, "audio/wav")},
            headers={"Authorization": f"Bearer {cfg['api_key']}",
                     "Accept": "application/json"},
            timeout=TIMEOUT)
    if r.status_code >= 400:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
    try:
        text = (r.json().get("text") or "").strip()
    except ValueError:
        raise RuntimeError(f"invalid JSON response: {r.text[:200]}")
    if not text:
        raise RuntimeError("transcription returned no text")
    log.info("transcribed %s -> %d chars", os.path.basename(wav_path),
             len(text))
    return text
