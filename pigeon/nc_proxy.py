"""`/nc` — Pigeon acts as the room's Nextcloud proxy: files (WebDAV) and
calendar (CalDAV). pi never sees the app password (it lives only in
.room.json, outside the sandbox) — it gets a 24h room-scoped token
(PIGEON_NC_TOKEN) that this endpoint verifies. The Nextcloud URL is
not in pi's environment on purpose: the proxy is the only door.

  GET    /nc/<bot>/<room>/files?path=DIR        list a directory
  GET    /nc/<bot>/<room>/files?path=FILE&download=1   download (raw)
  PUT    /nc/<bot>/<room>/files?path=FILE       upload (body, capped)
  DELETE /nc/<bot>/<room>/files?path=FILE       delete
  GET    /nc/<bot>/<room>/calendars             list calendars
  GET    /nc/<bot>/<room>/calendars/<calid>?from=&to=   events (RFC3339)
  PUT    /nc/<bot>/<room>/calendars/<calid>     new appointment (JSON)
  DELETE /nc/<bot>/<room>/calendars/<calid>/<uid>      delete (UID)
"""

import functools
import os
import re
from datetime import datetime, timedelta

import requests
from flask import Blueprint, Response, jsonify, request

from .config import BOTS_DIR, MAX_FILE_SIZE_MB, NEXTCLOUD_URL, log
from .nextcloud import (UnknownCalendar, _dbg, caldav_base,
                        caldav_calendars, caldav_create, caldav_events,
                        caldav_resolve, dav_path, dav_prop)
from .room import ROOM_DIR_RE, room_creds
from .security import nc_verify

bp = Blueprint("nc", __name__)


def logged(view):
    """One info line per /nc request: method, path, args, status code,
    bot and room — for successful and rejected (401/403/404/400/413/
    502) requests alike. No tokens or passwords ever reach the log."""
    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        response = view(*args, **kwargs)
        code = response[1] if isinstance(response, tuple) \
            else response.status_code
        log.info("nc: %s %s -> %s (bot=%s room=%s)",
                 request.method,
                 request.full_path.rstrip("?"), code,
                 request.view_args.get("bot"),
                 request.view_args.get("room"))
        return response
    return wrapper


def _authorized(bot, room):
    """(creds, None) when the caller may proxy for this room,
    else (None, error_response) — name check, room check, token check,
    app-password check."""
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", bot) \
            or not re.fullmatch(ROOM_DIR_RE, room):
        return None, (jsonify({"error": "bad bot or room name"}), 400)
    room_dir = os.path.join(BOTS_DIR, bot, "rooms", room)
    if not os.path.isdir(room_dir):
        return None, (jsonify({"error": "unknown room"}), 404)
    auth = request.headers.get("Authorization", "").split(" ", 1)
    if auth[0] != "Bearer" or not nc_verify(auth[1] if len(auth) > 1 else "",
                                            bot, room):
        return None, (jsonify({"error": "invalid or expired token"}), 401)
    creds = room_creds(room_dir)
    if creds is None:
        return None, (jsonify({"error": "room has no Nextcloud app password "
                                        "— send: !pw <user> <app-password>"}),
                      403)
    return creds, None


def _propfind(rel, creds):
    """PROPFIND directory `rel` (relative to the NC root). Returns
    (entries, status). entries: [{name, path, type, size, modified}]
    for the directory's direct children — files AND folders
    (type 'dir' / 'file'), path relative like `rel`."""
    prop = ("<prop xmlns:d='DAV:'><d:displayname/><d:getcontentlength/>"
            "<d:getlastmodified/><d:resourcetype/></prop>")
    body = f"<?xml version='1.0'?><d:propfind xmlns:d='DAV:'>{prop}</d:propfind>"
    r = requests.request("PROPFIND", NEXTCLOUD_URL + dav_path(rel) + "/",
                         auth=creds, data=body.encode(),
                         headers={"Depth": "1",
                                  "Content-Type": "text/xml"}, timeout=30)
    _dbg(r, f"PROPFIND /{rel.strip('/')}/")
    here = dav_path(rel).rstrip("/")  # the directory itself, excluded
    entries = []
    for m in re.finditer(
            r"<d:response>\s*<d:href>([^<]+)</d:href>(.*?)</d:response>",
            r.text, re.S):
        href, blk = m.group(1), m.group(2)
        if href.rstrip("/") == here:
            continue
        name = href.rstrip("/").rsplit("/", 1)[-1]
        entries.append({
            "name": name,
            "path": (rel.strip("/") + "/" + name) if rel.strip("/") else name,
            "type": "dir" if "<d:collection" in blk else "file",
            "size": int(dav_prop(blk, "getcontentlength") or 0),
            "modified": dav_prop(blk, "getlastmodified"),
        })
    return entries, r.status_code


def _resolve(rel, creds):
    """Resolve a path segment by segment against the live listings.
    Each segment: exact match first, then case-insensitive — Nextcloud
    paths are case-sensitive, but users and bots are not.
    Returns (resolved_rel, last_entry) or (None, None) if any
    segment doesn't exist."""
    cur, last = "", None
    for seg in [s for s in (rel or "").split("/") if s]:
        entries, status = _propfind(cur, creds)
        if status not in (200, 207):
            return None, None
        e = next((x for x in entries if x["name"] == seg), None)
        if e is None:
            e = next((x for x in entries
                      if x["name"].lower() == seg.lower()), None)
        if e is None:
            return None, None
        if e["name"] != seg:
            log.info("nc: case-corrected %r -> %r", seg, e["name"])
        cur = (cur + "/" + e["name"]) if cur else e["name"]
        last = e
    return cur, last


@bp.get("/nc/<bot>/<room>/files")
@logged
def files_list_or_download(bot, room):
    creds, err = _authorized(bot, room)
    if err:
        return err
    rel = request.args.get("path", "")
    url = dav_path(rel)
    if url is None:
        return jsonify({"error": "bad path"}), 400

    if request.args.get("download"):
        resolved, entry = _resolve(rel, creds)
        if entry is None or entry["type"] != "file":
            return jsonify({"error": f"not found: {rel}"}), 404
        with requests.get(NEXTCLOUD_URL + dav_path(resolved),
                          auth=creds, stream=True, timeout=(10, 600)) as r:
            if r.status_code >= 400:
                return jsonify({"error": f"download: HTTP {r.status_code}"}), 502
            data = r.raw.read(MAX_FILE_SIZE_MB * 1024 * 1024 + 1)
        if len(data) > MAX_FILE_SIZE_MB * 1024 * 1024:
            return jsonify({"error": "file too large"}), 413
        return Response(data, mimetype=r.headers.get("Content-Type"),
                        headers={"Content-Disposition":
                                 f'attachment; filename="{entry["name"]}"'})

    entries, status = _propfind(rel, creds)
    if status == 404 and rel:  # wrong case somewhere? resolve it
        resolved, last = _resolve(rel, creds)
        if last is not None:
            rel, (entries, status) = resolved, _propfind(resolved, creds)
    if status not in (200, 207):
        return jsonify({"error": f"list: HTTP {status}"}), 502
    return jsonify({"path": rel, "entries": entries})


def _resolved_parent(rel, creds):
    """rel minus its last segment, case-resolved; None if not found."""
    parent = rel.rpartition("/")[0]
    if not parent:
        return rel  # top-level file: nothing to resolve
    resolved, last = _resolve(parent, creds)
    if last is None:
        return None
    return (resolved + "/" + rel.rpartition("/")[2]) if resolved else \
        rel.rpartition("/")[2]


@bp.put("/nc/<bot>/<room>/files")
@logged
def files_upload(bot, room):
    creds, err = _authorized(bot, room)
    if err:
        return err
    rel = request.args.get("path", "")
    url = dav_path(rel)
    if url is None:
        return jsonify({"error": "bad path"}), 400
    if not rel or rel.endswith("/"):
        return jsonify({"error": "path must include a file name"}), 400
    rel = _resolved_parent(rel, creds) or rel
    url = dav_path(rel)
    cap = int(MAX_FILE_SIZE_MB * 1024 * 1024)
    length = int(request.headers.get("Content-Length") or 0)
    if length > cap:
        return jsonify({"error": f"file exceeds {MAX_FILE_SIZE_MB:.0f} MB"}), 413
    body = request.get_data()[:cap + 1]
    if len(body) > cap:
        return jsonify({"error": f"file exceeds {MAX_FILE_SIZE_MB:.0f} MB"}), 413
    r = requests.put(NEXTCLOUD_URL + url, auth=creds, data=body,
                     timeout=(10, 600))
    if r.status_code not in (201, 204):
        return jsonify({"error": f"upload: HTTP {r.status_code}"}), 502
    return jsonify({"ok": True, "path": rel, "bytes": len(body)}), 201


@bp.delete("/nc/<bot>/<room>/files")
@logged
def files_delete(bot, room):
    creds, err = _authorized(bot, room)
    if err:
        return err
    rel = request.args.get("path", "")
    url = dav_path(rel)
    if url is None:
        return jsonify({"error": "bad path"}), 400
    rel = _resolved_parent(rel, creds) or rel
    url = dav_path(rel)
    r = requests.delete(NEXTCLOUD_URL + url, auth=creds, timeout=30)
    if r.status_code not in (200, 204):
        return jsonify({"error": f"delete: HTTP {r.status_code}"}), 502
    return jsonify({"ok": True, "path": rel})


@bp.get("/nc/<bot>/<room>/calendars")
@logged
def calendars(bot, room):
    creds, err = _authorized(bot, room)
    if err:
        return err
    try:
        return jsonify({"calendars": caldav_calendars(creds)})
    except Exception as e:
        log.warning("nc calendars failed: %s", e)
        return jsonify({"error": str(e)}), 502


def _unknown(e):
    """404 for an unresolvable calendar id, with the list to choose from."""
    return jsonify({"error": str(e), "calendars": e.calendars}), 404


@bp.get("/nc/<bot>/<room>/calendars/<calid>")
@logged
def calendar_events(bot, room, calid):
    creds, err = _authorized(bot, room)
    if err:
        return err
    if not re.fullmatch(r"[^/?#&<>]{1,300}", calid):
        return jsonify({"error": "bad calendar id"}), 400
    t_from = request.args.get("from") or \
        datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    t_to = request.args.get("to") or \
        (datetime.utcnow() + timedelta(hours=48)).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        return jsonify({"calendar": calid, "events":
                        caldav_events(creds, calid, t_from, t_to)})
    except UnknownCalendar as e:
        return _unknown(e)
    except Exception as e:
        log.warning("nc events failed: %s", e)
        return jsonify({"error": str(e)}), 502


@bp.put("/nc/<bot>/<room>/calendars/<calid>")
@logged
def calendar_create(bot, room, calid):
    """New appointment from JSON: {title, start, end?, location?, ...}."""
    creds, err = _authorized(bot, room)
    if err:
        return err
    if not re.fullmatch(r"[^/?#&<>]{1,300}", calid):
        return jsonify({"error": "bad calendar id"}), 400
    spec = request.get_json(silent=True) or {}
    if not spec.get("title") or not spec.get("start"):
        return jsonify({"error": "title and start are required"}), 400
    try:
        uid = caldav_create(creds, calid, spec)
    except UnknownCalendar as e:
        return _unknown(e)
    except Exception as e:
        log.warning("nc appointment failed: %s", e)
        return jsonify({"error": str(e)}), 502
    return jsonify({"ok": True, "uid": uid,
                    "title": spec["title"], "start": spec["start"]}), 201


@bp.delete("/nc/<bot>/<room>/calendars/<calid>/<uid>")
@logged
def calendar_delete(bot, room, calid, uid):
    creds, err = _authorized(bot, room)
    if err:
        return err
    if not (re.fullmatch(r"[^/?#&<>]{1,300}", calid)
            and re.fullmatch(r"[A-Za-z0-9._-]{1,200}", uid)):
        return jsonify({"error": "bad calendar id or uid"}), 400
    try:
        calid = caldav_resolve(creds, calid)
        r = requests.delete(f"{caldav_base(creds[0])}/{calid}/{uid}.ics",
                            auth=creds, timeout=30)
    except UnknownCalendar as e:
        return _unknown(e)
    if r.status_code not in (200, 204):
        return jsonify({"error": f"delete: HTTP {r.status_code}"}), 502
    return jsonify({"ok": True, "uid": uid})
