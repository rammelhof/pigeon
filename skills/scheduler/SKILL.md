---
name: scheduler
description: Create, list, change or delete scheduled tasks for this chat room — one-time, daily at HH:MM (with timezone) or every N minutes. Use when the user asks to send or do something at a certain time, daily, or at an interval, or asks which tasks are scheduled.
---

# Room scheduler (pigeon)

The pigeon server runs a scheduler that watches **`schedules.json` in the
current working directory** (this room's folder). When an entry is due,
pigeon sends its `prompt` to this room exactly like a normal chat message —
you (pi) then handle it with full room context and skills.

## File format

```json
{
  "schedules": [
    {"id": "at-news", "prompt": "Send the top Austrian news of the day.", "time": "09:00", "tz": "Europe/Vienna"},
    {"id": "standup", "prompt": "Summarize open tasks for today.", "every_minutes": 60,
     "window": {"start": "10:00", "end": "22:00", "tz": "Europe/Vienna"}}
  ]
}
```

| field | meaning |
|---|---|
| `id` | short unique slug per room (`at-news`, `backup-check`, …) |
| `prompt` | self-contained instruction of what to do when it fires. Include all context the fired run needs (no one types extra text). |
| `time` | `"HH:MM"` 24h local time (daily). Combine with `tz`. |
| `tz` | IANA timezone name, e.g. `Europe/Vienna`. Default it to the user's local zone; `Europe/Vienna` if unknown. |
| `every_minutes` | number, ≥ 5 — repeat at this interval in minutes instead of a clock time. |
| `start_time` | optional `"HH:MM"` 24h phase anchor for `every_minutes`: runs happen at `start_time`, `start_time + interval`, `start_time + 2·interval`, … (wall clock, in the entry's timezone — `window.tz` if there is a window, else `tz`, else UTC). Makes the next run predictable and stable across restarts. Without it, the next run is simply "now + interval". |
| `window` | optional, only with `every_minutes`: `{"start": "HH:MM", "end": "HH:MM", "tz": "<IANA>"}` — the run only happens inside this local time window; with `start_time`, anchored candidates outside the window are skipped (the phase is kept); without it, the next run outside snaps to the next window start (e.g. `every_minutes: 60, window 10:00–22:00` → runs at 10, 11, …, 21:00 each day). |

For "every hour between 10:00 and 22:00" that is `every_minutes: 60` plus
the window above. For "every 15 minutes starting at 10:30" (predictable
10:30, 10:45, 11:00, …) use `every_minutes: 15, start_time: "10:30"`.
For "every 15 minutes on workdays mornings" use the
narrowest interval + window the user asked for (no weekday support — tell
the user if they need one).

## Rules

* `schedules.json` is **pi-managed**: read it, edit it with your file tools,
  write it back atomically (temp file + rename) and keep it valid JSON.
  If it does not exist, start from `{"schedules": []}`.
* **Never set or modify `next_run`** — pigeon computes and maintains it.
  (You may read it to tell the user when something runs next.)
* An entry needs either `time`+`tz` (daily) **or** `every_minutes` — never both;
  `window` only goes with `every_minutes` (start before end, no wrap over
  midnight).
* When the user says **"list the scheduled tasks"** (or similar): read
  `schedules.json` and present every entry in human-readable form, e.g.
  `at-news — daily at 09:00 (Europe/Vienna): Send the top Austrian news.`
  If the file is missing or empty: "No scheduled tasks."
* To change a task, edit its entry (keep the `id`); to delete, remove the
  entry. After any change, confirm in the room what is now scheduled.
* Schedules keep working across restarts; the fired run happens in this
  room's pi session, so it sees the conversation context.
