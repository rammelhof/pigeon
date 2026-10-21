---
name: memory
description: Long-term room memory. Read memory.md at the start of a session and append durable facts you learn about the user and this room. It survives the !clean command.
---

# Room memory

This room has a long-term memory file: `memory.md` in the working
directory (the workspace root). Unlike everything else in the
workspace, it survives the `!clean` command.

Rules:

- At the start of a session (the first message after a reset), read
  `memory.md` if it exists and let it inform what you say.
- Append a dated one-liner when you learn something durable: the
  user's name, company or business form; country and legal setup;
  preferences (language, formats, tone); ongoing projects, recurring
  obligations, decisions made in this room.
- Keep it small and current: one line per fact, prefix the date
  (`2026-01-05: …`). When a fact changes, update or strike the old
  line instead of adding a contradicting one.
- When the user asks "do you remember …", check `memory.md` before
  answering.
- Never store secrets (passwords, tokens, full card or IBAN data,
  health data). Store pointers ("invoice template in workspace/…"),
  not the data itself.
