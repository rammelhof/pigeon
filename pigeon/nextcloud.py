"""Nextcloud Talk: bot API message posting + WebDAV/CalDAV helpers.

File attachments: Talk sends files as a {file} rich-object placeholder.
The "link" in the parameter is a Talk virtual link (not downloadable), but
the file id is a real file id: pigeon resolves it to a WebDAV path via
SEARCH (basicsearch on oc:fileid) and downloads it with plain GET + Basic
auth (the room's app password) into the room's workspace/chat/files/
folder — no direct API, no temp share, no folder iteration.

All WebDAV/CalDAV calls authenticate with the room's app password
((user, password) tuple from .room.json).
"""

import json
import os
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote

import requests

from .config import MAX_FILE_SIZE_MB, NEXTCLOUD_URL, TALK_BOT_SECRET, log
from .room import room_creds
from .security import sign_request


def post_message(room, message):
    """Post a chat message via the signed Talk bot API
    (https://nextcloud-talk.readthedocs.io/en/latest/bots/).
    room must be the conversation token."""
    url = (f"{NEXTCLOUD_URL}/ocs/v2.php/apps/spreed/api/v1"
           f"/bot/{room}/message")
    headers = sign_request(TALK_BOT_SECRET, message)  # sign raw message
    r = requests.post(url, data=json.dumps({"message": message}).encode(),
                      headers=headers, timeout=30)
    if r.status_code >= 400:
        raise RuntimeError(f"post to chat failed: {r.status_code} {r.text[:200]}")


# ------------------------------------------------------------ files -------

def fmt_size(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def payload_file_param(payload):
    """The file rich-object parameter of a webhook payload, or None."""
    try:
        content = json.loads(
            (payload.get("object") or {}).get("content") or "{}")
        f = (content.get("parameters") or {}).get("file")
        return f if isinstance(f, dict) else None
    except Exception:
        return None


def dav_file_url(creds, file_id):
    """WebDAV SEARCH (basicsearch on oc:fileid) -> absolute download URL.
    Single request, no temp share, no folder iteration."""
    user, pw = creds
    uid = str(user).split("@", 1)[0]
    file_id = int(file_id)
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<d:searchrequest xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">'
        "<d:basicsearch><d:select><d:prop><d:displayname/><oc:fileid/>"
        "</d:prop></d:select><d:from><d:scope><d:href>/files/"
        + uid + "</d:href><d:depth>infinity</d:depth></d:scope></d:from>"
        "<d:where><d:eq><d:prop><oc:fileid/></d:prop>"
        f"<d:literal>{file_id}</d:literal></d:eq></d:where><d:orderby/>"
        "</d:basicsearch></d:searchrequest>")
    r = requests.request("SEARCH", f"{NEXTCLOUD_URL}/remote.php/dav",
                         auth=creds, data=xml.encode(),
                         headers={"Content-Type": "application/xml",
                                  "Depth": "0"}, timeout=30)
    _dbg(r, f"SEARCH fileid {file_id}")
    if r.status_code not in (200, 207):
        raise RuntimeError(f"dav search: HTTP {r.status_code}")
    m = re.search(r"<d:href>([^<]+)</d:href>", r.text)
    if not m:
        raise RuntimeError(f"file id {file_id} not found")
    href = m.group(1)
    if not href.startswith("/"):
        href = "/" + href
    return NEXTCLOUD_URL + href


def save_download(dl_url, name, dest_dir, auth=None):
    """Stream dl_url into dest_dir/<ts>-<name>. Returns (basename, bytes)."""
    name = re.sub(r"[^A-Za-z0-9._-]+", "_",
                  os.path.basename(name or "file")).strip("._") or "file"
    name = name[:100]
    os.makedirs(dest_dir, exist_ok=True)
    stamp = int(time.time())
    dest = os.path.join(dest_dir, f"{stamp}-{name}")
    i, dot = 1, name.rfind(".")
    while os.path.exists(dest):
        base, ext = (name[:dot], name[dot:]) if dot > 0 else (name, "")
        dest = os.path.join(dest_dir, f"{stamp}-{base}_{i}{ext}")
        i += 1
    cap = int(MAX_FILE_SIZE_MB * 1024 * 1024)
    size, tmp = 0, dest + ".part"
    try:
        with requests.get(dl_url, auth=auth, stream=True,
                          timeout=(10, 600)) as r:
            if r.status_code >= 400:
                raise RuntimeError(f"download: HTTP {r.status_code}")
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(1 << 16):
                    size += len(chunk)
                    if size > cap:
                        raise RuntimeError(
                            f"file exceeds {MAX_FILE_SIZE_MB:.0f} MB cap")
                    f.write(chunk)
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
    os.replace(tmp, dest)
    return os.path.basename(dest), size


def apply_file_param(message, f, cwd):
    """Download the file from the webhook's file parameter into the room's
    chat/files/ and put its path in front of the message
    ("chat/files/<name> <message>"). Without a room app password, pi is
    instructed to tell the user (in the user's language) that the password
    for file access is missing. Download failures yield a hint with the
    share link so the bot can still react."""
    name = f.get("name") or "file"
    mime = f.get("mimetype") or "unknown type"
    link = f.get("link") or ""
    creds = room_creds(cwd)
    if creds is None:
        log.info("file %s: no app password configured for this room — "
                 "instructing pi", name)
        return ("The user just sent a file ("
                f"{name}, {mime}"
                + (f"; share: {link}" if link else "")
                + "), but this room has no app password configured, so the "
                "server could not download it. Reply to the user — in the "
                "same language they are using — that the app password for "
                "file access is missing, and tell them to enable it by "
                "sending: /pw <nextcloud-user> <app-password>")
    try:
        file_id = int(f.get("id"))
    except (TypeError, ValueError):
        file_id = None
    if file_id is None:
        return (f"[file: {name} ({mime})] — download failed (no file id)"
                + (f"; share link: {link}" if link else ""))
    try:
        dl = dav_file_url(creds, file_id)
        saved, size = save_download(dl, name,
                                    os.path.join(cwd, "workspace", "chat",
                                                 "files"),
                                    auth=creds)
        log.info("downloaded file %s -> chat/files/%s (%s)",
                 name, saved, fmt_size(size))
        hint = f"chat/files/{saved}"
        if "{file}" in message:
            rest = re.sub(r"\s+", " ",
                          message.replace("{file}", " ")).strip()
            return (f"{hint} {rest}" if rest else hint)
        return f"{hint} {message.strip()}".strip()
    except Exception as e:
        log.warning("file download failed: %s", e)
        return (f"[file: {name} ({mime})] — download failed ({e})"
                + (f"; share link: {link}" if link else ""))


# -------------------------------------------------------- dav paths -------

def dav_path(rel):
    """rel (relative, no ..) -> /remote.php/webdav/<rel>; None = bad."""
    rel = (rel or "").strip("/")
    if rel and (".." in rel.split("/") or "\\" in rel or rel.startswith("~")):
        return None
    return f"/remote.php/webdav/{rel}"


def dav_prop(block, tag):
    """Value of <d:tag>...</d:tag> in a PROPFIND response block, or ''."""
    m = re.search(rf"<d:{tag}>([^<]*)</d:{tag}>", block)
    return m.group(1) if m else ""


# ------------------------------------------------------------ calendar ----

def caldav_base(user):
    return (NEXTCLOUD_URL.rstrip("/") + "/remote.php/dav/calendars/"
            + str(user).split("@", 1)[0])


def _dbg(r, what):
    """Log a Nextcloud response: body for XML/JSON, size for the rest
    (file contents etc. stay out of the log)."""
    ct = r.headers.get("Content-Type", "")
    if "xml" in ct or "json" in ct:
        log.debug("nc %s: HTTP %s %s, body: %s",
                  what, r.status_code, ct, r.text[:4000])
    else:
        log.debug("nc %s: HTTP %s %s (%d bytes)",
                  what, r.status_code, ct, len(r.content))


def caldav_calendars(creds):
    """[{name, id}] of the real calendars via PROPFIND with Depth 1.
    Only entries whose resourcetype is a CalDAV calendar count — the
    calendar-home collection itself (and any other collections) are
    listed too, but they are not calendars and 404 on access. The
    namespace prefix in the answer is server-defined, so match
    <anything:calendar> prefix-independently."""
    base = caldav_base(creds[0])
    prop = ("<prop xmlns:d='DAV:' xmlns:c='urn:ietf:params:xml:ns:caldav'>"
            "<d:displayname/><d:resourcetype/><c:calendar-color/></prop>")
    body = ("<?xml version='1.0'?>"
            "<d:propfind xmlns:d='DAV:'>" + prop + "</d:propfind>")
    r = requests.request("PROPFIND", base, auth=creds, data=body.encode(),
                         headers={"Depth": "1", "Content-Type": "text/xml"},
                         timeout=30)
    _dbg(r, f"PROPFIND {base}")
    if r.status_code not in (200, 207):
        raise RuntimeError(f"calendars: HTTP {r.status_code}")
    out = []
    for m in re.finditer(
            r"<d:response>\s*<d:href>([^<]+)</d:href>(.*?)</d:response>",
            r.text, re.S):
        url, blk = m.group(1), m.group(2)
        if not re.search(r"<\w+:calendar[\s/>]", blk):
            continue  # the calendar-home set itself, not a calendar
        dm = re.search(r"<d:displayname>([^<]*)</d:displayname>", blk)
        out.append({"name": dm.group(1).strip() if dm else "",
                    "id": unquote(url.rstrip("/").rsplit("/", 1)[-1])
                    if "/" in url else ""})
    return out


def caldav_resolve(creds, calid):
    """calid (id or name, case-insensitive) -> the real calendar id.
    Nextcloud ids are not guessable and users think in names, so accept
    both. Raises UnknownCalendar (with the full list) on no match."""
    cals = caldav_calendars(creds)
    low = calid.lower()
    for c in cals:
        if low in (c["id"].lower(), c["name"].lower()):
            return c["id"]
    raise UnknownCalendar(calid, cals)


class UnknownCalendar(RuntimeError):
    """calid matches no calendar; .calendars is the full [{name, id}]."""

    def __init__(self, calid, calendars):
        self.calid = calid
        self.calendars = calendars
        names = ", ".join(f"{c['name']} ({c['id']})" for c in calendars)
        super().__init__(f"unknown calendar {calid!r}; available: {names}")


def _ics_dt(value):
    """DTSTART/DTEND (compact RFC5545, ISO, date, UTC, TZID=...) -> naive
    datetime (UTC for offset values), or None."""
    v = re.sub(r"^TZID=[^:]+:", "", (value or "").strip())
    if re.match(r"^\d{4}-\d{2}-\d{2}", v):
        try:
            dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return None
        return dt.astimezone(timezone.utc).replace(tzinfo=None) \
            if dt.tzinfo is not None else dt
    m = re.match(r"^(\d{4})(\d{2})(\d{2})(?:T(\d{2})(\d{2})(\d{2})"
                r"([+-]\d{4}|Z)?)?$", v)
    if not m:
        return None
    y, mo, d, h, mi, s, off = m.groups()
    try:
        dt = datetime(int(y), int(mo), int(d), int(h or 0), int(mi or 0),
                      int(s or 0))
    except ValueError:
        return None
    shift = 0
    if off and off != "Z":
        sign = -1 if off[0] == "-" else 1
        shift = sign * (int(off[1:3]) * 3600 + int(off[3:5]) * 60)
    return dt - timedelta(seconds=shift)


def _rfc3339(value):
    """RFC3339 (or date) -> naive UTC datetime, or None."""
    v = (value or "").strip()
    try:
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def caldav_events(creds, calid, t_from, t_to):
    """Events of a calendar overlapping [t_from, t_to) as plain dicts
    (RFC3339 in). Fetches the whole calendar (GET) and filters locally —
    a universally supported operation; Nextcloud's calendar-query REPORT
    returns HTTP 500 in many versions."""
    calid = caldav_resolve(creds, calid)
    r = requests.get(f"{caldav_base(creds[0])}/{calid}/", auth=creds,
                     headers={"Accept": "text/calendar"}, timeout=60)
    _dbg(r, f"GET calendar {calid}")
    if r.status_code != 200:
        raise RuntimeError(f"events: HTTP {r.status_code}")
    text = re.sub(r"\r?\n[ \t]", "", r.text)  # unfold ICS continuation
    t_from, t_to = _rfc3339(t_from), _rfc3339(t_to)
    if t_from is None or t_to is None:
        raise RuntimeError(f"bad time range: {(t_from, t_to)}")
    evs = []
    for m in re.finditer(r"BEGIN:VEVENT(.*?)END:VEVENT", text, re.S):
        blk = m.group(1)

        def g(tag):
            mm = re.search(r"^" + tag + r"[;:](.*)$", blk, re.M | re.I)
            return mm.group(1).strip() if mm else ""

        start = _ics_dt(g("DTSTART"))
        if start is None:
            continue
        end = _ics_dt(g("DTEND"))
        if end is None or end <= start:  # no/one-point DTEND -> 1 hour
            end = start + timedelta(hours=1)
        # local wall clock (all-day or no TZ) vs UTC range: widen the day
        if re.fullmatch(r"\d{8}", g("DTSTART")) is not None:  # all-day
            start, end = start, start + timedelta(days=1)
        if not (start < t_to and end >= t_from):
            continue
        evs.append({"summary": g("SUMMARY"), "start": g("DTSTART"),
                    "end": g("DTEND"), "location": g("LOCATION"),
                    "description": g("DESCRIPTION")})
    return evs


def caldav_create(creds, calid, spec):
    """spec (JSON: title/start/end/location/description/attendees) -> ICS PUT.
    Returns the event UID."""
    calid = caldav_resolve(creds, calid)
    uid = uuid.uuid4().hex

    def dt(s):
        """datetime -> compact RFC5545 (UTC); a bare date stays a date
        (all-day). RFC 5545 forbids ISO-style values in ICS bodies."""
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
            return s.replace("-", "")
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if d.tzinfo is None:
            return d.strftime("%Y%m%dT%H%M%S")
        return d.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    st = spec["start"]
    ics = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//pigeon//EN",
           "BEGIN:VEVENT", f"UID:{uid}", f"DTSTAMP:{dt(st)}",
           "DTSTART:" + dt(st)]
    if spec.get("end"):
        ics.append("DTEND:" + dt(spec["end"]))
    ics.append(f"SUMMARY:{spec['title']}")
    if spec.get("location"):
        ics.append("LOCATION:" + spec["location"])
    if spec.get("description"):
        ics.append("DESCRIPTION:" + spec["description"])
    for a in (spec.get("attendees") or []):
        ics.append(f"ATTENDEE;CN={a}:mailto:{a}")
    ics += ["END:VEVENT", "END:VCALENDAR"]
    r = requests.put(f"{caldav_base(creds[0])}/{calid}/{uid}.ics",
                     auth=creds, data=("\r\n".join(ics)).encode(),
                     headers={"Content-Type": "text/calendar"}, timeout=30)
    if r.status_code not in (201, 204):
        raise RuntimeError(f"appointment: HTTP {r.status_code}")
    return uid
