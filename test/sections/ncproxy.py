"""Section 4d3 (/nc): the room's Nextcloud proxy (files + calendar) with
room-scoped token auth: case-correcting lists, upload, download, delete,
calendar list/resolve, appointment CRUD, cross-room token rejection, and
the token reaching pi as the last bwrap --setenv flag (pi's own upload via
the proxy is faked inside FakePopen)."""
import json
import os

import nc_stub
from pigeon.security import nc_token, nc_verify
from harness import (FakePopen, check, http, new_payload, signed_webhook,
                     wait_room_done)


def run():
    ncx_cwd = os.path.join(os.environ["BOTS_DIR"], "bot1", "rooms", "ncx")
    s, body = signed_webhook("bot1", new_payload("!pw ncuser ncpw123",
                                                 room_token="ncx0001",
                                                 room_name="Ncx"))
    check("/nc room pw set", s == 200 and body.get("handled") == "!pw",
          str(body))
    H = nc_stub.H
    tok = nc_token("bot1", "ncx")
    auth = {"Authorization": "Bearer " + tok}

    c, _ = http("/nc/bot1/ncx/files")
    check("/nc without token: 401", c == 401, str(c))
    c, _ = http("/nc/bot1/ncx/files",
                headers={"Authorization": "Bearer deadbeef00000000"})
    check("/nc with foreign token: 401", c == 401, str(c))
    c, b = http("/nc/bot1/ncx/files", headers=auth)
    check("/nc list root shows the stub folder", c == 200
          and any(e["name"] == "Temp" and e["type"] == "dir"
                  for e in b.get("entries", [])), str(b))
    c, b = http("/nc/bot1/ncx/files?path=Temp", headers=auth)
    check("/nc list folder shows the stub file", c == 200
          and any(e["name"] == "notes.txt" and e["type"] == "file"
                  for e in b.get("entries", [])), str(b))
    c, b = http("/nc/bot1/ncx/files?path=temp", headers=auth)
    check("/nc list case-corrects a folder (temp -> Temp)",
          c == 200 and b.get("path") == "Temp"
          and any(e["name"] == "notes.txt" for e in b.get("entries", [])),
          str(b))
    c, b = http("/nc/bot1/ncx/files?path=temp%2Ffrom-pi.txt",
                body=b"proxy upload test", method="PUT",
                headers={**auth, "Content-Type": "text/plain"})
    check("/nc upload (PUT) stored via WebDAV with the room creds",
          c == 201 and b.get("bytes") == len(b"proxy upload test")
          and H.files.get("Temp/from-pi.txt") == b"proxy upload test",
          str(b) + str(list(H.files)))
    c, b = http("/nc/bot1/ncx/files?path=temp%2Fnotes.txt&download=1",
                headers=auth)
    check("/nc download resolves the case (temp/notes.txt -> Temp/notes.txt)",
          c == 200 and b == b"hello nextcloud", repr(b)[:60])
    c, b = http("/nc/bot1/ncx/files?path=temp%2Ffrom-pi.txt", method="DELETE",
                headers=auth)
    check("/nc delete removes the file",
          c == 200 and "Temp/from-pi.txt" not in H.files, str(b))
    c, b = http("/nc/bot1/ncx/calendars", headers=auth)
    check("/nc lists calendars (CalDAV PROPFIND)",
          c == 200 and {x["name"] for x in b.get("calendars", [])}
          == {"Family", "Work", "Privat", "Privat2"}, str(b))
    check("/nc does not offer the calendar-home collection as a calendar",
          all(x["id"] != "u" for x in b.get("calendars", []))
          and len(b.get("calendars", [])) == 4, str(b))
    c, b = http("/nc/bot1/ncx/calendars/Privat2?from=2026-10-15T00:00:00Z"
                "&to=2026-10-15T23:59:59Z", headers=auth)
    check("/nc calendar with a special id (space/parens) works by name",
          c == 200 and b.get("events") == [], str(b)[:200])
    H.events["family"].append({"uid": "ev2", "summary": "In range",
                               "start": "2026-10-15T09:00:00Z"})
    c, b = http("/nc/bot1/ncx/calendars/FAMILY?from=2026-10-15T00:00:00Z"
                "&to=2026-10-15T23:59:59Z", headers=auth)
    check("/nc resolves a calendar by display name, case-insensitively",
          c == 200 and [e.get("summary") for e in b.get("events", [])]
          == ["In range"], str(b)[:200])
    H.events["family"].pop()
    c, b = http("/nc/bot1/ncx/calendars/nope", headers=auth)
    check("/nc unknown calendar: 404 with the list to choose from",
          c == 404 and "unknown calendar" in b.get("error", "")
          and {x["id"] for x in b.get("calendars", [])}
          == {"family", "work", "privat", "Privat (2)"},
          str(b))
    c, b = http("/nc/bot1/ncx/calendars/nope/ev1", method="DELETE",
                headers=auth)
    check("/nc delete with unknown calendar: 404 with the list",
          c == 404 and "calendars" in b, str(b))
    c, b = http("/nc/bot1/ncx/calendars/family",
                body=json.dumps({"title": "r: Training",
                                 "start": "2026-10-15T10:00:00+02:00",
                                 "end": "2026-10-15T11:00:00+02:00",
                                 "location": "gym"}), method="PUT",
                headers={**auth, "Content-Type": "application/json"})
    new_uid = b.get("uid") if isinstance(b, dict) else None
    check("/nc create appointment (ICS PUT) in the right calendar",
          c == 201 and bool(new_uid)
          and any("SUMMARY:r: Training" in (e.get("summary", "") and "") or
                  e["uid"] == new_uid for e in H.events.get("family", [])),
          str(b))
    c, b = http("/nc/bot1/ncx/calendars/family?from=2026-10-15T00:00:00Z"
                "&to=2026-10-15T23:59:59Z", headers=auth)
    check("/nc lists today's appointments (time-range filter works)",
          c == 200 and any(e.get("summary") == "r: Training"
                           and e.get("start", "").endswith("T080000Z")
                           for e in b.get("events", []))
          and not any(e.get("summary") == "Old thing"
                      for e in b.get("events", [])), str(b)[:250])
    c, b = (http(f"/nc/bot1/ncx/calendars/family/{new_uid}", method="DELETE",
                 headers=auth) if new_uid else (0, {}))
    check("/nc delete appointment by uid",
          c == 200 and all(e["uid"] != new_uid
                           for e in H.events.get("family", [])), str(b))
    # full event detail: seed one event with every readable property
    H.events["work"].append({"uid": "evd",
                             "summary": "Team standup (quarterly review)",
                             "start": "20261015T100000Z",
                             "end": "20261015T103000Z",
                             "location": "Room 4B",
                             "description": "Agenda:\n1. Q3 numbers",
                             "rrule": "FREQ=WEEKLY;BYDAY=MO",
                             "status": "CONFIRMED", "sequence": 0,
                             "organizer": "boss@example.com",
                             "attendees": [{"cn": "Jane Doe",
                                            "email": "jane@example.com"}],
                             "alarms": [{"action": "DISPLAY",
                                         "minutes": -15},
                                        {"action": "EMAIL", "minutes": -1440}]})
    c, e = http("/nc/bot1/ncx/calendars/family/nosuchuid", headers=auth)
    check("/nc event detail unknown uid: 404", c == 404, f"{c}: {e}")
    c, e = http("/nc/bot1/ncx/calendars/work/evd", headers=auth)
    check("/nc event detail: summary/start/end/location/description",
          c == 200 and e.get("uid") == "evd"
          and e.get("summary") == "Team standup (quarterly review)"
          and e.get("start") == "20261015T100000Z"
          and e.get("end") == "20261015T103000Z" and e.get("all_day")
          is False and e.get("location") == "Room 4B"
          and e.get("description") == "Agenda:\n1. Q3 numbers" and
          "etag" in e, f"{c}: {e}")
    check("/nc event detail: rrule/status/sequence/organizer",
          e.get("rrule") == "FREQ=WEEKLY;BYDAY=MO" and e.get("status")
          == "CONFIRMED" and e.get("sequence") == 0
          and e.get("organizer") == "boss@example.com", str(e))
    check("/nc event detail: attendees + alarms",
          e.get("attendees") == [{"cn": "Jane Doe",
                                  "email": "jane@example.com"}]
          and e.get("alarms") == [{"action": "DISPLAY", "minutes": -15},
                                  {"action": "EMAIL", "minutes": -1440}],
          str(e))
    H.put_calls.clear()
    c, e = http("/nc/bot1/ncx/calendars/work/evd",
                method="PUT",
                body=json.dumps({"title": "Sync (moved room)",
                                 "location": None}),
                headers={**auth, "Content-Type": "application/json"})
    check("/nc update: partial change keeps the rest, bumps sequence",
          c == 200 and e.get("ok") is True and e.get("calendar") == "work"
          and e["event"]["summary"] == "Sync (moved room)"
          and e["event"]["location"] is None
          and e["event"]["start"] == "20261015T100000Z"
          and e["event"]["sequence"] == 1 and len(e["event"]["alarms"]) == 2
          and e["event"]["rrule"] == "FREQ=WEEKLY;BYDAY=MO", str(e))
    H.put_calls.clear()
    c, e = http("/nc/bot1/ncx/calendars/work/evd", headers=auth)
    check("/nc update: persisted (new title, location cleared, alarms)",
          c == 200 and e.get("summary") == "Sync (moved room)"
          and e.get("location") == "" and e.get("sequence") == 1
          and e.get("alarms") == [{"action": "DISPLAY", "minutes": -15},
                                  {"action": "EMAIL", "minutes": -1440}],
          f"{c}: {e}")
    H.put_calls.clear()
    c, e = http("/nc/bot1/ncx/calendars/work/evd",
                method="PUT", body=json.dumps({"calendar": "Privat"}),
                headers={**auth, "Content-Type": "application/json"})
    check("/nc update: move calendar by display name",
          c == 200 and e.get("calendar") == "privat"
          and e["event"]["summary"] == "Sync (moved room)", str(e))
    check("/nc move: PUT to the new calendar, DELETE from the old one",
          ("PUT", "/remote.php/dav/calendars/ncuser/privat/evd.ics")
          in H.put_calls and
          ("DELETE", "/remote.php/dav/calendars/ncuser/work/evd.ics")
          in H.put_calls, str(H.put_calls))
    check("/nc move: event now in the target calendar only",
          [x["uid"] for x in H.events["privat"]] == ["evd"]
          and all(x["uid"] != "evd" for x in H.events["work"]),
          str(H.events))
    c, e = http("/nc/bot1/ncx/calendars/work/evd", headers=auth)
    check("/nc moved event: gone from the old calendar (404)", c == 404,
          f"{c}: {e}")
    c, e = http("/nc/bot1/ncx/calendars/work/ev9",
                method="PUT", body=json.dumps({"title": "x"}),
                headers={**auth, "Content-Type": "application/json"})
    check("/nc update unknown uid: 404", c == 404, f"{c}: {e}")
    c, e = http("/nc/bot1/ncx/calendars/work/ev9",
                method="PUT", body=json.dumps({}),
                headers={**auth, "Content-Type": "application/json"})
    check("/nc update with empty body: 400", c == 400, f"{c}: {e}")
    c, e = http("/nc/bot1/ncx/calendars/nope/evd",
                method="PUT", body=json.dumps({"title": "x"}),
                headers={**auth, "Content-Type": "application/json"})
    check("/nc update unknown calendar: 404 + calendar list",
          c == 404 and isinstance(e.get("calendars"), list),
          f"{c}: {e}")
    H.put_calls.clear()
    # token is room-scoped: the same token must not open another room
    s, body = signed_webhook("bot1", new_payload("!pw ncuser ncpw123",
                                                 room_token="othrm001",
                                                 room_name="Oth"))
    c, _ = http("/nc/bot1/oth/files",
                headers={"Authorization": "Bearer " + tok})
    check("/nc token is room-scoped (cross-room token rejected)", c == 401,
          str(c))
    # fake-pi gets the token as its LAST bwrap flag (right before the binary)
    signed_webhook("bot1", new_payload("hello ncx", room_token="ncx0001",
                                       room_name="Ncx"))
    check("ncx task done", (t := wait_room_done("bot1/rooms/ncx")) is not None
          and t["status"] == "done", str(t))
    last = [c[1] for c in FakePopen.cmds
            if os.path.basename(c[1][0]).casefold().startswith("bwrap")][-1]
    tok_i = last.index("PIGEON_NC_TOKEN") if "PIGEON_NC_TOKEN" in last else -1
    run_tok = last[tok_i + 1] if tok_i >= 0 and tok_i + 1 < len(last) else ""
    check("pi run gets PIGEON_NC_TOKEN as the last bwrap flag (before pi)",
          tok_i >= 0 and last[tok_i - 1] == "--setenv"
          and nc_verify(run_tok, "bot1", "ncx")
          and last[tok_i + 2] == "fake-pi",
          str(last[tok_i - 1:tok_i + 3]))
    check("pi run uploads through the /nc proxy with the token",
          H.files.get("Temp/fake-pi-upload.txt") == b"hello from sandboxed pi",
          str(list(H.files)))
