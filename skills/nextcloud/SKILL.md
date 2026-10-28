---
name: nextcloud
description: "Nextcloud access (files, calendar, contacts). Use whenever
the user asks about files, documents, appointments or people; always
for 'find/search the file X'."
---

Nextcloud is reached through Pigeon's local proxy — the room's
credentials live there, not in your env (no Nextcloud URL, no app
password). The proxy is the only door; every request is logged with
this room's token. No auth header needed.

Base URL: http://pigeon:8080/nc/<bot>/<room>
(PIGEON_BOT_NAME / PIGEON_ROOM_NAME come from your env.)

Files (WebDAV), path relative to the Nextcloud root, case auto-corrected:

    GET    files?path=DIR                    list (direct children)
    GET    files?path=DIR/FILE&download=1    download (raw bytes)
    PUT    files?path=DIR/FILE               upload (body = content)
    DELETE files?path=DIR/FILE               delete

Calendar (CalDAV), `<id>` = calendar id or display name (case-insens.;
check `GET calendars` — the error lists all calendars):

    GET    calendars                            list calendars
    GET    calendars/<id>?from=..&to=..         events (RFC3339 range)
    GET    calendars/<id>/<uid>                 full event
    PUT    calendars/<id>                       new appointment (JSON)
    PUT    calendars/<id>/<uid>                 change (JSON, partial)
    DELETE calendars/<id>/<uid>                 delete (uid from listing)

New appointment JSON: `title`, `start` (RFC3339 or "YYYY-MM-DD [HH:MM]"),
optionally `end`, `location`, `description`, `all_day`, `rrule`
("FREQ=WEEKLY;BYDAY=MO,FR"), `status`, `organizer`, `attendees`
([{"email","cn"}]), `alarms` ([{"minutes": -30}]).
Change: same fields — given ones replace (null clears), omitted keep;
`calendar: "<name or id>"` moves the event.

Contacts (CardDAV), `<id>` = address book id or display name (same):

    GET    addressbooks                             list address books
    GET    addressbooks/<id>          list contacts (fn may be empty)
    GET    addressbooks/<id>?q=john   find by name (case-insens., has fn)
    GET    addressbooks/<id>/<uri>      full contact (fn, email, tel, ...)
    PUT    addressbooks/<id>             new contact (JSON)
    PUT    addressbooks/<id>/<uri>       change (JSON, partial)
    DELETE addressbooks/<id>/<uri>       delete (uri from listing)

New contact JSON: `fn`, optionally `org`, `title`, `note`, `bday`,
`email`/`tel`/`url` (string or list). Change: given fields replace
(null clears), omitted keep — as with appointments.
