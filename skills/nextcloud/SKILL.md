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
password`, tell the user to send `!pw <nextcloud-user> <app-password>` in
this room — do not try to work around it.

## Files

| What | Command |
|---|---|
| list a directory | `curl -sf -H "Authorization: Bearer $PIGEON_NC_TOKEN" "http://127.0.0.1:8080/nc/$BOT/$ROOM/files?path=temp"` |
| download | `curl -sf ... "/nc/$BOT/$ROOM/files?path=temp/doc.md&download=1" -o doc.md` |
| upload | `curl -sf ... -X PUT --data-binary @doc.md "/nc/$BOT/$ROOM/files?path=temp/doc.md"` |
| delete | `curl -sf ... -X DELETE "/nc/$BOT/$ROOM/files?path=temp/doc.md"` |

Paths are relative to the user's Nextcloud root (`""` = root). The list
answer is `{"path": ..., "entries": [{name, path, type, size, modified}]}`
— **both folders and files** (`type` = `dir` or `file`); use each entry's
`path` in your next request. To **edit a Nextcloud file**: download it,
edit it locally, upload it back.

### Case sensitivity

Nextcloud paths are **case-sensitive** (`Docs` ≠ `docs`), but the proxy
matches them case-insensitively, so `temp` finds `Temp`; report the exact
name. If a path still 404s, list the parent folder and propose the
closest candidates; ask the user if ambiguous.

## Calendar

| What | Command |
|---|---|
| list calendars | `curl -sf ... "/nc/$BOT/$ROOM/calendars"` → `[{name, id}, ...]` |
| list appointments | `curl -sf ... "/nc/$BOT/$ROOM/calendars/<id>?from=2026-10-15T00:00:00Z&to=2026-10-15T23:59:59Z"` |
| event detail | `curl -sf ... "/nc/$BOT/$ROOM/calendars/<id>/<uid>"` → all fields (description, location, rrule, attendees, alarms, …) |
| create appointment | `curl -sf ... -X PUT -H 'Content-Type: application/json' -d '{"title":"r: Training","start":"2026-10-15T10:00:00+02:00"}' "/nc/$BOT/$ROOM/calendars/<id>"` |
| change event | `curl -sf ... -X PUT -H 'Content-Type: application/json' -d '{"title":"New","calendar":"Privat"}' "/nc/$BOT/$ROOM/calendars/<id>/<uid>"` |
| delete appointment | `curl -sf ... -X DELETE "/nc/$BOT/$ROOM/calendars/<id>/<uid>"` |

* `calid` accepts the calendar **id or its display name**, any casing —
  the proxy resolves it. If it still can't resolve it, the 404 error body
  contains the full `calendars` list: pick the right one from there.
* Change: only passed fields are replaced, omitted ones are kept, `null`
  clears. Fields: `title`, `start`, `end`, `location`, `description`,
  `all_day`, `rrule`, `status`, `sequence`, `organizer`, `attendees`,
  `alarms`; `calendar:"<name>"` moves the event. `start`/`end` are RFC
  3339 with the user's timezone offset; `alarms`: `[{action, minutes}]`
  (negative = before start). Without `end`, the appointment is zero-length.
* The create response contains the `uid` — keep it if the user may
  change or delete the appointment later. Create accepts `title`
  (required), `start`, `end`, `location`, `description`, `attendees`,
  `alarms` (same shape as change).
* "Today"/"this week" appointments: compute the date range yourself and
  fetch it.

## Rules

- Never print, store or echo the token.
- Never invent a Nextcloud URL — the proxy is the only way.
- On `404 not found` from a file request: scan the parent folder and
  propose the closest candidates before giving up (see above).
- When the proxy fails, report the error to the user in their language.
