"""Section 4e: voice messages (audio/wav, audio/mpeg). Without STT config an
error is posted, not queued; with config but no app password the user is
told to /pw; otherwise the file is downloaded, transcribed via the STT
endpoint, the text is posted back with an ear emoji and handed to pi.
"""
import json
import os

import nc_stub
from harness import (BOTS_TEST, FakePopen, check, file_payload, new_payload,
                     signed_webhook, wait_prompt, wait_room_done)


def run():
    stt_json = os.path.join(BOTS_TEST, ".pigeon.json")
    check("startup created the .pigeon.json boilerplate in the bots folder",
          json.load(open(stt_json)) == {
              "transcription": {"endpoint": "", "model": "", "api_key": ""}},
          open(stt_json).read())

    s, body = signed_webhook("bot1",
                             file_payload("voicemail", "voicerm001",
                                          "Voice Room", file_id=77001,
                                          name="Sprachnachricht test.wav",
                                          mimetype="audio/wav"))
    voic_cwd = os.path.join(BOTS_TEST, "bot1", "rooms", "voice-room")
    voic_posts = [m for r, m in nc_stub.H.received if r == "voicerm001"]
    check("wav without STT config: not queued, error posted to the chat",
          s == 200 and body.get("handled") == "stt-unconfigured"
          and any(".pigeon.json" in m for m in voic_posts)
          and not os.path.isdir(os.path.join(voic_cwd, "workspace", "chat")),
          str(body) + str(voic_posts)[:200])

    json.dump({"transcription": {"endpoint": "http://127.0.0.1:8099",
                                 "model": "STT", "api_key": "TEST-KEY"}},
              open(stt_json, "w"))

    s, body = signed_webhook("bot1",
                             file_payload("voicemail", "voicerm001",
                                          "Voice Room", file_id=77002,
                                          name="Sprachnachricht 1.wav",
                                          mimetype="audio/wav"))
    voic_posts = [m for r, m in nc_stub.H.received if r == "voicerm001"]
    check("wav with config but no app password: error posted, nothing sent",
          s == 200 and body.get("handled") == "stt-no-credentials"
          and "/pw <nextcloud-user> <app-password>" in voic_posts[-1]
          and not nc_stub.H.stt_calls,
          str(body) + str(voic_posts[-1:]))

    signed_webhook("bot1", new_payload("/pw ncuser ncpw123",
                                       room_token="voicerm001",
                                       room_name="Voice Room"))
    wav_bytes = b"RIFF\x00\x01\x02\x03WAVEfmt \xff\xfe\x00fake-wav"
    nc_stub.H.file_bytes = wav_bytes
    s, body = signed_webhook("bot1",  # empty message text: Talk voice events
                             file_payload("", "voicerm001", "Voice Room",
                                          file_id=77003,
                                          name="Sprachnachricht 2.wav",
                                          mimetype="audio/wav"))
    nc_stub.H.file_bytes = b"FAKE-IMAGE-BYTES-1234"
    check("wav transcribed: queued for pi",
          s == 202 and body.get("transcribed") is True, str(body))
    vfiles = os.path.join(voic_cwd, "workspace", "chat", "files")
    vsaved = [f for f in (os.listdir(vfiles) if os.path.isdir(vfiles) else [])
              if f.endswith(".wav")]
    check("wav downloaded into chat/files/", len(vsaved) == 1, str(vsaved))
    if vsaved:
        check("wav bytes match the share",
              open(os.path.join(vfiles, vsaved[0]), "rb").read() == wav_bytes)
    check("transcription POSTed with api key, model + file",
          len(nc_stub.H.stt_calls) == 1
          and nc_stub.H.stt_calls[0] == ("Bearer TEST-KEY", "STT",
                                         vsaved[0] if vsaved else None),
          str(nc_stub.H.stt_calls))
    voic_posts = [m for r, m in nc_stub.H.received if r == "voicerm001"]
    check("transcribed text posted back with an ear emoji",
          any(m.startswith("👂 ") and nc_stub.H.stt_text in m
              for m in voic_posts), str(voic_posts)[:200])
    t = wait_room_done("bot1/rooms/voice-room")
    check("voice task done", bool(t) and t["status"] == "done", str(t))
    check("pi got the transcribed text (not the wav)",
          wait_prompt(nc_stub.H.stt_text,
                      "bot1/rooms/voice-room/workspace") is not None,
          str([p[:60] for c, p in FakePopen._seen
               if c.replace("\\", "/").endswith(
                   "bot1/rooms/voice-room/workspace")])[:250])

    # mp3 voice messages go through the same path
    s, body = signed_webhook("bot1",
                             file_payload("", "voicerm001", "Voice Room",
                                          file_id=77004,
                                          name="Sprachnachricht 3.mp3",
                                          mimetype="audio/mpeg"))
    check("mp3 voice message transcribed via the same path",
          s == 202 and body.get("transcribed") is True
          and len(nc_stub.H.stt_calls) == 2
          and nc_stub.H.stt_calls[1][2] is not None
          and nc_stub.H.stt_calls[1][2].endswith(".mp3")
          and nc_stub.H.stt_calls[1][0] == "Bearer TEST-KEY",
          f"{s} {body} {nc_stub.H.stt_calls}")
    t = wait_room_done("bot1/rooms/voice-room", min_count=2)
    check("mp3 task done", bool(t) and t["status"] == "done", str(t))
