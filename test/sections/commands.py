"""Section 4c: room commands — !help (+ !h), !version (+ !v),
!schedule list|rm, !stop (+ !s, kills the running pi, reaction
removed), the burst reaction rule (only the LAST message of a batch is
reacted) and !mention gating (on / off / status; only @<bot name> or
@bot trigger, mention stripped before pi)."""
import json
import os
import time

import nc_stub
from harness import (BOTS_TEST, check, join_leave_payload, new_payload,
                     signed_webhook, tasks, wait_prompt, wait_room_done)

E_START = "\U0001f916"     # 🤖 pi started (kept afterwards)


def _reactions_since(before):
    """(message_id, emoji, action) triples added to the stub's log
    (the stub records (room, message_id, emoji, action))."""
    return [(r[1], r[2], r[3])
            for r in nc_stub.H.bot_reactions[before:]]


def _wait_running(cwd, timeout=2):
    deadline = time.time() + timeout
    while time.time() < deadline:
        h = tasks()
        mine = [t for t in h["tasks"]
                if t["room_cwd"].replace("\\", "/").endswith(cwd)]
        if mine and mine[-1]["status"] == "running":
            return True
        time.sleep(0.1)
    return False


def _posts(room_token):
    return [m for room, m in nc_stub.H.received if room == room_token]


def run():
    # 4c. /help (+ /h) and /version — display only, never reach pi
    s, body = signed_webhook("bot1", new_payload("!h",
                                                 room_token="hlp00001",
                                                 room_name="Help Room"))
    check("!help: handled, not queued",
          s == 200 and body.get("handled") == "!help", str(body))
    posted = _posts("hlp00001")
    check("!help: lists the commands",
          any("!stop" in m and "!thinking" in m and "!schedule" in m
              for m in posted), str(posted)[:300])

    s, body = signed_webhook("bot1", new_payload("!version",
                                                 room_token="hlp00001",
                                                 room_name="Help Room"))
    check("!version: handled, not queued",
          s == 200 and body.get("handled") == "!version", str(body))
    posted = _posts("hlp00001")
    check("!version: posts pigeon version",
          any(m.startswith("pigeon ") for m in posted), str(posted)[:200])

    s, body = signed_webhook("bot1", new_payload("!v",
                                                 room_token="hlp00001",
                                                 room_name="Help Room"))
    check("!v alias: handled like !version",
          s == 200 and body.get("handled") == "!version", str(body))
    posted = _posts("hlp00001")
    check("!v alias: posts the version again",
          sum(m.startswith("pigeon ") for m in posted) >= 2,
          str(posted)[-200:])

    # 4c2. /schedule list + rm (schedules.json in the workspace)
    sch_cwd = os.path.join(BOTS_TEST, "bot1", "rooms", "sched-room")
    s, body = signed_webhook("bot1", new_payload("!schedule list",
                                                 room_token="sch00001",
                                                 room_name="Sched Room"))
    check("!schedule list: handled",
          s == 200 and body.get("handled") == "!schedule", str(body))
    check("!schedule list: empty room says so",
          any("No scheduled tasks" in m for m in _posts("sch00001")),
          str(_posts("sch00001"))[:200])

    ws = os.path.join(sch_cwd, "workspace")
    os.makedirs(ws, exist_ok=True)
    far = time.time() + 10 ** 7   # never due during the test
    schedules = {
        "schedules": [
            {"id": "at-news", "prompt": "Fetch the news headlines",
             "time": "08:00", "tz": "Europe/Vienna",
             "next_run": far + 1},
            {"id": "daily-weather", "prompt": "Report the weather",
             "time": "07:30", "next_run": far + 2},
        ],
    }
    with open(os.path.join(ws, "schedules.json"), "w") as f:
        json.dump(schedules, f)

    s, body = signed_webhook("bot1", new_payload("!schedule list",
                                                 room_token="sch00001",
                                                 room_name="Sched Room"))
    posted = _posts("sch00001")
    check("!schedule list: ids backticked for rm, next run shown",
          any("- `at-news`:" in m and "- `daily-weather`:" in m and
              "08:00" in m and "Europe/Vienna" in m and
              "[next:" in m
              for m in posted), str(posted)[:400])

    s, body = signed_webhook("bot1", new_payload(
        "!schedule rm daily-weather", room_token="sch00001",
        room_name="Sched Room"))
    check("!schedule rm: handled",
          s == 200 and body.get("handled") == "!schedule", str(body))
    data = json.load(open(os.path.join(ws, "schedules.json")))
    check("!schedule rm: entry removed from file",
          [e["id"] for e in data["schedules"]] == ["at-news"],
          str(data)[:200])
    posted = _posts("sch00001")
    check("!schedule rm: confirmation posted",
          any("Removed" in m and "daily-weather" in m for m in posted),
          str(posted)[:200])

    s, body = signed_webhook("bot1", new_payload(
        "!schedule rm daily-weather", room_token="sch00001",
        room_name="Sched Room"))
    posted = _posts("sch00001")
    check("!schedule rm: unknown id reported",
          any("No scheduled task with id" in m for m in posted),
          str(posted)[:200])

    # 4c2b. bare !schedule -> short usage; !sch alias works
    s, body = signed_webhook("bot1", new_payload("!schedule",
                                                 room_token="sch00001",
                                                 room_name="Sched Room"))
    check("bare !schedule: short usage help",
          s == 200 and body.get("handled") == "!schedule"
          and "!schedule list" in _posts("sch00001")[-1]
          and "rm <id>" in _posts("sch00001")[-1],
          str(_posts("sch00001"))[-200:])
    s, body = signed_webhook("bot1", new_payload("!sch list",
                                                 room_token="sch00001",
                                                 room_name="Sched Room"))
    check("!sch list alias: shows the remaining entry",
          s == 200
          and any("- `at-news`:" in m for m in _posts("sch00001")),
          str(_posts("sch00001"))[-200:])

    # 4c3. /stop (+ /s): kills the running pi, ends quietly, removes the
    # robot reaction — the only reaction removal pigeon ever does
    rea_before = len(nc_stub.H.bot_reactions)
    s, body = signed_webhook("bot1", new_payload("hang the pipeline",
                                                 room_token="stp00001",
                                                 room_name="Stop Room"))
    check("!stop setup: prompt queued", s == 202, str(body))
    check("!stop setup: task running",
          _wait_running("bot1/rooms/stop-room"), str(tasks()["tasks"]))

    s, body = signed_webhook("bot1", new_payload("!s",
                                                 room_token="stp00001",
                                                 room_name="Stop Room"))
    check("!stop: handled, not queued",
          s == 200 and body.get("handled") == "!stop", str(body))
    done = wait_room_done("bot1/rooms/stop-room", timeout=25)
    check("!stop: task ended as stopped",
          done is not None and done["status"] == "stopped", str(done))
    posted = _posts("stp00001")
    check("!stop: exactly one reply, no failure notice",
          len(posted) == 1 and "stopped" in posted[0]
          and "failed" not in posted[0], str(posted)[:200])
    # the indicator thread is async by design — poll briefly for the
    # removal to land
    reacts = _reactions_since(rea_before)
    for _ in range(20):
        if reacts == [("999", E_START, "add"), ("999", E_START, "remove")]:
            break
        time.sleep(0.3)
        reacts = _reactions_since(rea_before)
    check("!stop: robot reacted on start, then was removed",
          reacts == [("999", E_START, "add"), ("999", E_START, "remove")],
          str(reacts))

    # /stop when nothing is running: friendly note, no task
    s, body = signed_webhook("bot1", new_payload("!stop",
                                                 room_token="stp00001",
                                                 room_name="Stop Room"))
    check("!stop idle: handled",
          s == 200 and body.get("handled") == "!stop", str(body))
    posted = _posts("stp00001")
    check("!stop idle: says nothing is running",
          posted and "Nothing is running" in posted[-1], str(posted)[:200])

    # 4c4. burst reaction rule: two messages within the batch window =
    # one pi run, and only the LAST message is reacted
    rea_before = len(nc_stub.H.bot_reactions)
    p1 = new_payload("part one of the burst", room_token="bst00001",
                     room_name="Burst Room")
    p1["object"]["id"] = "1001"
    p2 = new_payload("part two of the burst", room_token="bst00001",
                     room_name="Burst Room")
    p2["object"]["id"] = "1002"
    s1, _ = signed_webhook("bot1", p1)
    s2, _ = signed_webhook("bot1", p2)
    check("burst: both messages queued", s1 == 202 and s2 == 202,
          f"{s1} {s2}")
    done = wait_room_done("bot1/rooms/burst-room", timeout=25, min_count=1)
    check("burst: one run, merged prompt, done",
          done is not None and done["status"] == "done"
          and done["retries"] == 0, str(done))
    prompt = wait_prompt("part one of the burst",
                         "bot1/rooms/burst-room/workspace")
    check("burst: pi saw both parts in order",
          prompt is not None and "part two of the burst" in prompt
          and prompt.index("part one") < prompt.index("part two"),
          str(prompt)[:200])
    reacts = _reactions_since(rea_before)
    check("burst: only the LAST message is reacted (one notification)",
          reacts == [("1002", E_START, "add")], str(reacts))

    # 4c5. !mention gating: on -> only @<bot name> or @bot are handled
    # (mention stripped), status, off -> everything again. Commands
    # always work, even in mention mode. The join webhook first, so the
    # bot's Talk name ("Pigeon") is known to the gate.
    s, body = signed_webhook("bot1", join_leave_payload("qut00001",
                                                        "Quiet Room"))
    check("!mention: join handled", s == 200, str(body))
    meta = json.load(open(os.path.join(BOTS_TEST, "bot1", "rooms",
                                       "quiet-room", ".room.json")))
    check("!mention: bot name persisted from the join webhook",
          meta.get("bot_name") == "Pigeon", str(meta))

    s, body = signed_webhook("bot1", new_payload("!m on",
                                                 room_token="qut00001",
                                                 room_name="Quiet Room"))
    check("!mention on: handled",
          s == 200 and body.get("handled") == "!mention", str(body))
    meta = json.load(open(os.path.join(BOTS_TEST, "bot1", "rooms",
                                       "quiet-room", ".room.json")))
    check("!mention on: persisted in .room.json",
          meta.get("mention_only") is True, str(meta))

    posted = _posts("qut00001")
    check("!mention on: reply names both mention forms",
          posted and "@Pigeon" in posted[-1] and "@bot" in posted[-1],
          str(posted)[-200:])

    s, body = signed_webhook("bot1", new_payload("hello stranger",
                                                 room_token="qut00001",
                                                 room_name="Quiet Room"))
    check("!mention on: plain message ignored",
          s == 200 and body.get("ignored") == "no mention", str(body))
    check("!mention on: no task created for the ignored message",
          not any(t["room_cwd"].replace("\\", "/").endswith(
                      "bot1/rooms/quiet-room")
                  for t in tasks()["tasks"]),
          str(tasks()["tasks"])[:200])

    s, body = signed_webhook("bot1", new_payload("@raphael hi",
                                                 room_token="qut00001",
                                                 room_name="Quiet Room"))
    check("!mention on: a foreign mention is ignored too",
          s == 200 and body.get("ignored") == "no mention", str(body))

    s, body = signed_webhook("bot1", new_payload("@pigeon hi there",
                                                 room_token="qut00001",
                                                 room_name="Quiet Room"))
    check("!mention on: mentioned message queued (case-insensitive)",
          s == 202, str(body))
    prompt = wait_prompt("hi there", "bot1/rooms/quiet-room/workspace")
    check("!mention on: mention stripped before pi",
          prompt is not None and "@pigeon" not in prompt, str(prompt)[:200])
    done = wait_room_done("bot1/rooms/quiet-room", timeout=25, min_count=1)
    check("!mention on: mentioned run finished",
          done is not None and done["status"] == "done", str(done))

    s, body = signed_webhook("bot1", new_payload("@bot hey",
                                                 room_token="qut00001",
                                                 room_name="Quiet Room"))
    check("!mention on: the @bot alias works too", s == 202, str(body))
    done = wait_room_done("bot1/rooms/quiet-room", timeout=25, min_count=2)
    check("!mention on: @bot run finished",
          done is not None and done["status"] == "done", str(done))

    s, body = signed_webhook("bot1", new_payload("!mention status",
                                                 room_token="qut00001",
                                                 room_name="Quiet Room"))
    check("!mention status: handled (commands work in mention mode)",
          s == 200 and body.get("handled") == "!mention", str(body))
    posted = _posts("qut00001")
    check("!mention status: says required",
          posted and "required" in posted[-1]
          and "not required" not in posted[-1], str(posted)[:200])

    s, body = signed_webhook("bot1", new_payload("!m off",
                                                 room_token="qut00001",
                                                 room_name="Quiet Room"))
    meta = json.load(open(os.path.join(BOTS_TEST, "bot1", "rooms",
                                       "quiet-room", ".room.json")))
    check("!mention off: persisted",
          meta.get("mention_only") is False, str(meta))
    posted = _posts("qut00001")
    check("!mention off: confirmation posted",
          posted and "no longer required" in posted[-1], str(posted)[:200])

    s, body = signed_webhook("bot1", new_payload("back to normal",
                                                 room_token="qut00001",
                                                 room_name="Quiet Room"))
    check("!mention off: plain message queued again", s == 202, str(body))
    done = wait_room_done("bot1/rooms/quiet-room", timeout=25, min_count=2)
    check("!mention off: run finished",
          done is not None and done["status"] == "done", str(done))

    # 4c5b. bare !mention -> short usage
    s, body = signed_webhook("bot1", new_payload("!mention",
                                                 room_token="qut00001",
                                                 room_name="Quiet Room"))
    check("bare !mention: short usage help",
          s == 200 and body.get("handled") == "!mention"
          and "!mention on" in _posts("qut00001")[-1]
          and "status" in _posts("qut00001")[-1],
          str(_posts("qut00001"))[-200:])
    # 4c6. !mode: personas (default = office, finance w/ start prompt,
    # trader, programmer = native pi prompt) + !memory
    # Start prompts are NOT posted by pigeon — they are appended to pi's
    # system prompt so pi generates the setup questions itself on the
    # first reply of a fresh session (mode switches wipe the session).
    s, body = signed_webhook("bot1", new_payload("!mode",
                                                 room_token="mdm00001",
                                                 room_name="Mode Room"))
    p = _posts("mdm00001")
    check("!mode (bare): lists modes, marks the default, shows current",
          s == 200 and body.get("handled") == "!mode" and p
          and "programmer" in p[-1] and "office (default)" in p[-1]
          and "finance" in p[-1] and "trader" in p[-1] and "this room uses: office" in p[-1]
          and "start_prompt" not in p[-1],
          str(p)[-400:])

    # switching personas starts a fresh pi session — fabricate one
    # (fake-pi writes no real session files) so the wipe is observable
    ws = os.path.join(BOTS_TEST, "bot1", "rooms", "mode-room")
    from pigeon.pi import pi_command, session_dir
    sdir = session_dir(os.path.join(ws, "workspace"))
    os.makedirs(sdir, exist_ok=True)
    old_session = os.path.join(
        sdir, "2026-01-01T00-00-00Z_bot1_mode-room.jsonl")
    with open(old_session, "w") as f:
        f.write('{"type":"session","version":3}\n')

    s, body = signed_webhook("bot1", new_payload("!mode finance",
                                                 room_token="mdm00001",
                                                 room_name="Mode Room"))
    meta = json.load(open(os.path.join(BOTS_TEST, "bot1", "rooms",
                                       "mode-room", ".room.json")))
    p = _posts("mdm00001")
    check("!mode finance: confirmed, persisted, fresh session started",
          s == 200 and body.get("handled") == "!mode"
          and "Mode: finance" in p[-1] and "fresh pi session" in p[-1]
          and not os.path.exists(old_session)
          and meta.get("mode") == "finance",
          str((body, meta, p[-1]))[:300])

    s, body = signed_webhook("bot1", new_payload("!mode finance",
                                                 room_token="mdm00001",
                                                 room_name="Mode Room"))
    check("!mode finance again: already active, no switch",
          body.get("handled") == "!mode"
          and "already active" in _posts("mdm00001")[-1],
          str(_posts("mdm00001")[-1])[:200])

    # the persona text AND the start-prompt instruction reach pi via
    # --append-system-prompt (pi asks its setup questions itself)
    cmd = pi_command("hi", "bot1_mode-room", ws, "bot1", "mdm00001",
                     "finance")
    check("pi_command: finance appends persona + start prompt",
          cmd.count("--append-system-prompt") == 2
          and "finance assistant" in " ".join(cmd)
          and "FIRST reply" in " ".join(cmd), " ".join(cmd[:24]))
    cmd = pi_command("hi", "bot1_mode-room", ws, "bot1", "mdm00001",
                     "trader")
    check("pi_command: trader mode appends the persona (no start prompt)",
          cmd.count("--append-system-prompt") == 1
          and "trading assistant" in " ".join(cmd), " ".join(cmd[:24]))
    cmd = pi_command("hi", "bot1_mode-room", ws, "bot1", "mdm00001",
                     "office")
    check("pi_command: office mode appends the office persona "
          "(no start prompt)",
          cmd.count("--append-system-prompt") == 1
          and "office assistant" in " ".join(cmd), " ".join(cmd[:24]))
    cmd = pi_command("hi", "bot1_mode-room", ws, "bot1", "mdm00001",
                     "programmer")
    check("pi_command: programmer mode injects nothing (native prompt)",
          "--append-system-prompt" not in cmd, " ".join(cmd[:20]))

    # !memory: empty at first
    s, body = signed_webhook("bot1", new_payload("!memory",
                                                 room_token="mdm00001",
                                                 room_name="Mode Room"))
    p = _posts("mdm00001")
    check("!memory (empty): handled, says no entries yet",
          s == 200 and body.get("handled") == "!memory" and p
          and "no memory entries" in p[-1], str(p)[-200:])

    # pi (the memory skill) would write durable facts here — simulate it
    os.makedirs(os.path.join(ws, "workspace"), exist_ok=True)
    with open(os.path.join(ws, "workspace", "memory.md"), "w",
              encoding="utf-8") as f:
        f.write("2026-01-05: user is a freelancer in Austria (AT)\n")

    s, body = signed_webhook("bot1", new_payload("!memory",
                                                 room_token="mdm00001",
                                                 room_name="Mode Room"))
    p = _posts("mdm00001")
    check("!memory: shows the memory entries",
          s == 200 and "freelancer in Austria" in p[-1], str(p)[-300:])

    # !clean wipes the session + workspace, but memory.md survives;
    # the start prompt is NOT re-posted — it rides in pi's system
    # prompt on the next fresh run.
    sdir = session_dir(os.path.join(ws, "workspace"))
    os.makedirs(sdir, exist_ok=True)
    with open(os.path.join(
            sdir, "2026-01-01T00-00-00Z_bot1_mode-room.jsonl"), "w") as f:
        f.write('{"type":"session","version":3}\n')
    with open(os.path.join(ws, "workspace", "leftover.txt"), "w") as f:
        f.write("junk")
    s, body = signed_webhook("bot1", new_payload("!clean",
                                                 room_token="mdm00001",
                                                 room_name="Mode Room"))
    p = _posts("mdm00001")
    mem = os.path.join(ws, "workspace", "memory.md")
    check("!clean: memory survives, no start prompt posted",
          s == 200 and body.get("handled") == "!clean"
          and "pi session cleared" in p[-1]
          and os.path.isfile(mem) and "country" not in p[-1]
          and not os.path.exists(os.path.join(ws, "workspace",
                                              "leftover.txt")),
          str((body, p[-1]))[:300])

    s, body = signed_webhook("bot1", new_payload("!memory",
                                                 room_token="mdm00001",
                                                 room_name="Mode Room"))
    check("!memory after !clean: entries still there",
          "freelancer in Austria" in _posts("mdm00001")[-1],
          str(_posts("mdm00001")[-1])[:300])

    s, body = signed_webhook("bot1", new_payload("!mode nope",
                                                 room_token="mdm00001",
                                                 room_name="Mode Room"))
    p = _posts("mdm00001")
    check("!mode nope: unknown mode -> the list",
          s == 200 and body.get("handled") == "!mode" and p
          and "Unknown mode" in p[-1] and "office" in p[-1],
          str(p)[-300:])

    s, body = signed_webhook("bot1", new_payload("!mode programmer",
                                                 room_token="mdm00001",
                                                 room_name="Mode Room"))
    meta = json.load(open(os.path.join(BOTS_TEST, "bot1", "rooms",
                                       "mode-room", ".room.json")))
    check("!mode programmer: native prompt, persisted",
          s == 200 and "native programmer prompt" in _posts("mdm00001")[-1]
          and meta.get("mode") == "programmer",
          str((body, meta)))

    s, body = signed_webhook("bot1", new_payload("!mode default",
                                                 room_token="mdm00001",
                                                 room_name="Mode Room"))
    meta = json.load(open(os.path.join(BOTS_TEST, "bot1", "rooms",
                                       "mode-room", ".room.json")))
    check("!mode default: back to the default (office), meta cleared",
          s == 200 and "Mode: office" in _posts("mdm00001")[-1]
          and "mode" not in meta,
          str((body, meta)))

    # a plain run in the default office mode still completes
    s, body = signed_webhook("bot1", new_payload("write a memo",
                                                 room_token="mdm00001",
                                                 room_name="Mode Room"))
    check("!mode default: message queued", s == 202, str(body))
    done = wait_room_done("bot1/rooms/mode-room", min_count=1)
    check("!mode default: office-mode run finished",
          done is not None and done["status"] == "done", str(done))
