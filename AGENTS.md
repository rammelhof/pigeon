# AGENTS.md

Instructions for AI agents (and humans) working on this repo with a clean session.

## Project

`pigeon` — a tiny Flask webhook server that bridges Nextcloud Talk bots to the
`pi` coding agent. One Docker container, gunicorn (1 worker, 32 threads), all
state in per-room folders under `$BOTS_DIR`. See `README.md` for user docs.

## Layout

| Path | What it is |
|------|-----------|
| `pigeon/config.py` | All config from env + `VERSION` / `COMMIT` |
| `pigeon/security.py` | HMAC signature verification, room tokens |
| `pigeon/room.py` | Per-room state (`.room.json`, session/workspace, lifecycle) |
| `pigeon/nextcloud.py` | Talk bot API + WebDAV client (messages, reactions, files) |
| `pigeon/pi.py` | pi subprocess management, bwrap sandbox, session wipe |
| `pigeon/tasks.py` | Task queue, retry, `stop_running`, stats |
| `pigeon/indicator.py` | Reaction indicator (single robot emoji, add-only) |
| `pigeon/scheduler.py` | `workspace/schedules.json` engine + `!schedule` command |
| `pigeon/skills.py` | Bundled skill syncing into the pi config dir |
| `pigeon/stt.py` | Voice message transcription (external STT endpoint) |
| `pigeon/nc_proxy.py` | `/nc` proxy: bot credentials -> Talk/WebDAV/CalDAV |
| `pigeon/web.py` | Flask app, webhook parse/dispatch, all room commands |
| `test/smoke.py` | Entry point: starts fake Talk/Nextcloud + app, runs all sections |
| `test/harness.py` | Shared fake servers + helpers used by every section |
| `test/sections/*.py` | One module per topic, executed in list order |

## Rules for changes

1. **Bump the version before committing.** Before `git add`, increment the
   patch number of `VERSION` in `pigeon/config.py`
   (`1.0.0` → `1.0.1` → `1.0.2` → …).
2. **Run the full smoke suite before committing:**
   `python test/smoke.py` must end with `ALL TESTS PASSED`.
   Tests are plain Python (no pytest) and must stay idempotent.
3. **KISS.** Small modules, one concern each. No circular imports: a module
   may only depend on `config` and layers below it (e.g. `room.py` may not
   import `pi` or `nextcloud` at module level — use local imports).
4. **Room commands use the `!` prefix** (`!clean`, `!thinking`, `!stop`,
   `!schedule`/`!sch`, `!mention`/`!m`, `!mode`, `!pw`, `!help`/`!h`,
   `!version`/`!v`). Keep the short aliases working.
5. **Tests**: add a check to the matching `test/sections/` module; the check
   helper prints PASS/FAIL and collects failures. New section? Append it to
   `SECTIONS` in `test/sections/__init__.py` (order matters — later sections
   build on earlier state).
6. **Commits**: descriptive subject line + short body, commit to `main`,
   push to `https://github.com/rammelhof/pigeon.git`.

## Gotchas

- Nextcloud file/folder names are case-sensitive; the `/nc` proxy resolves
  paths segment-wise case-insensitively.
- CalDAV: use PROPFIND Depth:1 with `<c:calendar-data/>` (REPORT
  calendar-query is broken on the target server). Match XML namespace
  prefixes independently (`<\w+:calendar`).
- Pi sessions live at `$PI_CODING_AGENT_DIR/sessions/--<workspace-dashes>--/`.
- Talk bot API can only **post** messages, never edit; the indicator is a
  single robot reaction, add-only (removed only by `!stop`).
- Thinking is saved to `<room>/last-thinking.md`, never auto-posted.
- Gunicorn runs without an access log (error log to console only);
  app-level `@logged` decorator covers request logging.
