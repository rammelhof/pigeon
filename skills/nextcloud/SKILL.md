---
name: nextcloud
description: Interact with the user's Nextcloud (files, calendar) through the local pigeon proxy. Use for any request about the user's Nextcloud files or appointments.
---

# Nextcloud (via the local pigeon proxy)

Your user's Nextcloud is **only** reachable through the local proxy
`http://127.0.0.1:8080/nc/<bot>/<room>/...`. You have **no credentials and no
Nextcloud URL** — that is by design. Every request needs the header
`Authorization: Bearer $PIGEON_NC_TOKEN` (from your environment; the token
lives ~24 h).

`<bot>` and `<room>` are the two parts of your session id
(`<bot>_<room>`). If the proxy answers `403 room has no Nextcloud app
password`, tell the user to send `/pw <nextcloud-user> <app-password>` in
this room — do not try to work around it.

## Files

| What | Command |
|---|---|
| list a directory | `curl -sf -H "Authorization: Bearer $PIGEON_NC_TOKEN" "http://127.0.0.1:8080/nc/$BOT/$ROOM/files?path=temp"` |
| download | `curl -sf ... "/nc/$BOT/$ROOM/files?path=temp/doc.md&download=1" -o doc.md` |
| upload | `curl -sf ... -X PUT --data-binary @doc.md "/nc/$BOT/$ROOM/files?path=temp/doc.md"` |
| delete | `curl -sf ... -X DELETE "/nc/$BOT/$ROOM/files?path=temp/doc.md"` |

Paths are relative to the user's Nextcloud root (`""` = root). To **edit a
Nextcloud file**: download it, edit it locally, upload it back.

## Calendar

| What | Command |
|---|---|
| list calendars | `curl -sf ... "/nc/$BOT/$ROOM/calendars"` → `[{name, id}, ...]` |
| list appointments | `curl -sf ... "/nc/$BOT/$ROOM/calendars/<id>?from=2026-10-15T00:00:00Z&to=2026-10-15T23:59:59Z"` |
| create appointment | `curl -sf ... -X PUT -H 'Content-Type: application/json' -d '{"title":"r: Training","start":"2026-10-15T10:00:00+02:00"}' "/nc/$BOT/$ROOM/calendars/<id>"` |
| delete appointment | `curl -sf ... -X DELETE "/nc/$BOT/$ROOM/calendars/<id>/<uid>"` |

* Pick the calendar by **name** from the list (e.g. "family" → its `id`).
* `start`/`end` are RFC 3339 — include the user's timezone offset when they
  give a local time. Without `end`, the appointment is all-day/zero-length.
* The create response contains the `uid` — mention it only if the user asks
  to change or delete the appointment later.
* "Today"/"this week" appointments: compute the date range yourself and
  fetch it.

## Rules

- Never print, store or echo the token.
- Never invent a Nextcloud URL — the proxy is the only way.
- When the proxy fails, report the error to the user in their language.
