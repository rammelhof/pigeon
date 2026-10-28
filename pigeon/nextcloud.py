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
from xml.sax.saxutils import unescape

import requests

from .config import MAX_FILE_SIZE_MB, NEXTCLOUD_URL, TALK_BOT_SECRET, log
from .room import room_creds
from .security import sign_request


def post_message(room, message, silent=False):
    """Post a chat message via the signed Talk bot API
    (https://nextcloud-talk.readthedocs.io/en/latest/bots/).
    room must be the conversation token. silent=True suppresses chat
    notifications (the bot API's own 'silent' flag, Talk 17.1+)."""
    url = (f"{NEXTCLOUD_URL}/ocs/v2.php/apps/spreed/api/v1"
           f"/bot/{room}/message")
    headers = sign_request(TALK_BOT_SECRET, message)  # sign raw message
    body = {"message": message}
    if silent:
        body["silent"] = True
    r = requests.post(url, data=json.dumps(body).encode(),
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
    share link so the user can still open the file."""
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
                "sending: !pw <nextcloud-user> <app-password>")
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


class EventNotFound(RuntimeError):
    """The calendar has no event with the given UID."""


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


def _ics_escape(text):
    """Escape a text property value (RFC 5545 3.3.11): backslash, semicolon,
    comma, line breaks."""
    text = (text or "").replace("\\", "\\\\")
    return text.replace(";", "\\;").replace(",", "\\,") \
        .replace("\r\n", "\\n").replace("\n", "\\n") \
        .replace("\r", "\\n")


def _ics_unescape(value):
    """Reverse _ics_escape on a parsed text property value."""
    return (value or "").replace("\\n", "\n").replace("\\,", ",") \
        .replace("\\;", ";").replace("\\\\", "\\")


def _vevents(ics):
    """All VEVENTs of an ICS text as dicts — everything readable: the
    basic properties, recurrence, organizer/attendees and the VALARMs
    (alerts/notifications). The start/end keys hold the raw ICS values
    (what the client should show); _start/_end are the parsed naive
    datetimes used for the range filter."""
    text = re.sub(r"\r?\n[ \t]", "", ics)  # unfold ICS continuation

    def _alarm(blk):
        """VALARM block -> {action, minutes?, trigger?, description?}."""
        def g(tag):
            mm = re.search(r"^" + tag + r"(?::|;[^:]*)?(.*)$", blk,
                           re.M | re.I)
            return _ics_unescape(mm.group(1).strip()) if mm else ""

        m = re.search(r"^TRIGGER(;[^:]*)?:(.*)$", blk, re.M | re.I)
        val = m.group(2).strip() if m else ""
        out = {"action": (g("ACTION") or "DISPLAY").upper()}
        if re.match(r"^\d{4}-\d{2}-\d{2}", val):  # absolute trigger
            dt = _ics_dt(val)
            if dt:
                out["trigger"] = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        elif val and val[0] in "+-":  # relative (PT.. duration)
            m2 = re.fullmatch(r"([+-])P(?:(\d+)D)?"
                              r"(?:T(?:(\d+)H)?(?:(\d+)M)?"
                              r"(?:(\d+)S)?)?", val)
            if m2:
                hours = int(m2.group(3) or 0)
                minutes = int(m2.group(4) or 0)
                out["minutes"] = (-hours * 60 - minutes
                                  if m2.group(1) == "-"
                                  else hours * 60 + minutes)
        desc = g("DESCRIPTION")
        if desc:
            out["description"] = desc
        return out

    def _attendee(val):
        """ATTENDEE value -> {email, cn?}; ignores non-mailto values."""
        email = val.split(":", 1)[-1].strip()
        if not email.startswith("mailto:"):
            return None
        email = email[len("mailto:"):].strip()
        out = {"email": email}
        cn = re.search(r"CN=([^;:]*)", val)
        if cn:
            out["cn"] = cn.group(1).strip()
        return out

    out = []
    for m in re.finditer(r"BEGIN:VEVENT(.*?)END:VEVENT", text, re.S):
        blk = m.group(1)

        def g(tag, _blk=blk):
            mm = re.search(r"^" + tag + r"[;:](.*)$", _blk, re.M | re.I)
            return _ics_unescape(mm.group(1).strip()) if mm else ""

        start = _ics_dt(g("DTSTART"))
        if start is None:
            continue
        end = _ics_dt(g("DTEND"))
        dur_all_day = False
        if end is None or end <= start:  # no/one-point DTEND
            if re.fullmatch(r"\d{8}", g("DTSTART")) is not None:  # all-day
                end = start + timedelta(days=1)
                dur_all_day = True
            else:
                end = start + timedelta(hours=1)
        ev = {"uid": g("UID"), "summary": g("SUMMARY"),
              "location": g("LOCATION"), "description": g("DESCRIPTION"),
              "start": g("DTSTART"), "end": g("DTEND"),
              "all_day": re.fullmatch(r"\d{8}", g("DTSTART")) is not None,
              "rrule": g("RRULE"), "status": g("STATUS"),
              "organizer": "",
              "attendees": [a for a in
                            (_attendee(mm.group(1))
                             for mm in re.finditer(
                                 r"^ATTENDEE[;:](.*)$", blk, re.M | re.I))
                            if a],
              "alarms": [_alarm(a) for a in re.findall(
                  r"BEGIN:VALARM(.*?)END:VALARM", blk, re.S)],
              "_start": start, "_end": end, "_durday": dur_all_day}
        om = re.search(r"^ORGANIZER[;:](.*)$", blk, re.M | re.I)
        if om:
            ev["organizer"] = _ics_unescape(
                om.group(1).split(":", 1)[-1].strip())
        seq = g("SEQUENCE")
        if seq.isdigit():
            ev["sequence"] = int(seq)
        out.append(ev)
    return out


def caldav_events(creds, calid, t_from, t_to):
    """Events of a calendar overlapping [t_from, t_to) as plain dicts.
    One PROPFIND (Depth 1) asks for c:calendar-data of every event —
    universally supported. It works where the alternatives do not:
    calendar-query REPORT is broken on many Nextcloud versions
    (HTTP 500/400) and a plain GET on the calendar path answers a
    placeholder HTML page instead of ICS."""
    calid = caldav_resolve(creds, calid)
    t_from, t_to = _rfc3339(t_from), _rfc3339(t_to)
    if t_from is None or t_to is None:
        raise RuntimeError(f"bad time range: {(t_from, t_to)}")
    body = ("<?xml version='1.0'?>"
            "<d:propfind xmlns:d='DAV:' "
            "xmlns:c='urn:ietf:params:xml:ns:caldav'>"
            "<d:prop><c:calendar-data/></d:prop></d:propfind>")
    r = requests.request("PROPFIND", f"{caldav_base(creds[0])}/{calid}/",
                         auth=creds, data=body.encode(),
                         headers={"Depth": "1", "Content-Type": "text/xml"},
                         timeout=120)
    _dbg(r, f"PROPFIND events of {calid}")
    if r.status_code not in (200, 207):
        raise RuntimeError(f"events: HTTP {r.status_code}")
    evs = []
    for blk in re.findall(r"<d:response>(.*?)</d:response>", r.text, re.S):
        dm = re.search(r"<\w+:calendar-data>(.*?)</\w+:calendar-data>",
                       blk, re.S)
        if not dm:
            continue
        for e in _vevents(unescape(dm.group(1))):
            if e["_start"] < t_to and e["_end"] >= t_from:
                evs.append({k: v for k, v in e.items()
                            if not k.startswith("_")})
    return evs


def _alarm_lines(al):
    """One alarm dict ({action, minutes? or trigger?, description?})
    -> its ICS VALARM lines (BEGIN..END included)."""
    out = ["BEGIN:VALARM"]
    if al.get("minutes") is not None:
        out.append("TRIGGER:" + ("-" if al["minutes"] < 0 else "+")
                   + "PT%dM" % abs(al["minutes"]))
    elif al.get("trigger"):
        out.append("TRIGGER:"
                   + _ics_dt(al["trigger"]).strftime("%Y%m%dT%H%M%SZ"))
    out.append("ACTION:" + (al.get("action") or "DISPLAY").upper())
    if al.get("description"):
        out.append("DESCRIPTION:" + _ics_escape(al["description"]))
    out.append("END:VALARM")
    return out


def caldav_create(creds, calid, spec):
    """spec (JSON: title/start/end/location/description/attendees/alarms)
    -> ICS PUT. Returns the event UID."""
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
    ics.append(f"SUMMARY:{_ics_escape(spec['title'])}")
    if spec.get("location"):
        ics.append("LOCATION:" + _ics_escape(spec["location"]))
    if spec.get("description"):
        ics.append("DESCRIPTION:" + _ics_escape(spec["description"]))
    for a in (spec.get("attendees") or []):
        ics.append(f"ATTENDEE;CN={a}:mailto:{a}")
    for al in (spec.get("alarms") or []):
        ics += _alarm_lines(al)
    ics += ["END:VEVENT", "END:VCALENDAR"]
    r = requests.put(f"{caldav_base(creds[0])}/{calid}/{uid}.ics",
                     auth=creds, data=("\r\n".join(ics)).encode(),
                     headers={"Content-Type": "text/calendar"}, timeout=30)
    if r.status_code not in (201, 204):
        raise RuntimeError(f"appointment: HTTP {r.status_code}")
    return uid


def _build_event_lines(e):
    """One event dict (from _vevents) -> the ICS lines of its VEVENT
    (without BEGIN/END:VEVENT). All datetimes are written as UTC (Z);
    an all-day event stays a bare date."""
    dt = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = [f"UID:{e.get('uid', '')}", f"DTSTAMP:{dt}"]

    def d(v, date_only):
        d2 = _ics_dt(v)
        if d2 is None:
            return v
        if date_only:
            return d2.strftime("%Y%m%d")
        if d2.tzinfo is None:
            return d2.strftime("%Y%m%dT%H%M%SZ")
        return d2.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    all_day = bool(e.get("all_day"))
    out.append("DTSTART:" + d(e.get("start", ""), all_day))
    end = e.get("end", "")
    if end or e.get("_durday"):
        out.append("DTEND:" + d(end, all_day)
                   if end else (e["_start"] + timedelta(days=1))
                   .strftime("%Y%m%d"))
    out.append("SUMMARY:" + _ics_escape(e.get("summary", "")))
    for key, tag in (("description", "DESCRIPTION"), ("location", "LOCATION")):
        if e.get(key):
            out.append(f"{tag}:{_ics_escape(e[key])}")
    for key, tag in (("rrule", "RRULE"), ("status", "STATUS")):
        if e.get(key):
            out.append(f"{tag}:{e[key]}")
    if e.get("sequence") is not None:
        out.append(f"SEQUENCE:{e['sequence']}")
    if e.get("organizer"):
        out.append("ORGANIZER:mailto:" + e["organizer"])
    for a in e.get("attendees") or []:
        out.append(("ATTENDEE;CN=%s:mailto:%s" % (a.get("cn", ""),
                                                  a["email"]))
                   if a.get("cn")
                   else "ATTENDEE:mailto:" + a["email"])
    for al in e.get("alarms") or []:
        out += _alarm_lines(al)
    return out


def caldav_get_event(creds, calid, uid):
    """One event of a calendar (plain GET, ETag) -> its full dict
    (all fields _vevents parses). Raises RuntimeError when missing."""
    calid = caldav_resolve(creds, calid)
    r = requests.get(f"{caldav_base(creds[0])}/{calid}/{uid}.ics",
                     auth=creds, timeout=30)
    _dbg(r, f"GET event {calid}/{uid}")
    if r.status_code == 404:
        raise EventNotFound(
            f"event {uid!r} not found in calendar {calid!r}")
    if r.status_code >= 400:
        raise RuntimeError(f"event: HTTP {r.status_code}")
    m = re.search(r"BEGIN:VEVENT(.*?)END:VEVENT", r.text, re.S)
    if not m:
        raise RuntimeError("event has no VEVENT")
    ev = _vevents("BEGIN:VEVENT" + m.group(1) + "END:VEVENT")
    if not ev:
        raise RuntimeError("event could not be parsed")
    out = {k: v for k, v in ev[0].items() if not k.startswith("_")}
    out["etag"] = r.headers.get("ETag", "")
    return out


def caldav_update(creds, calid, uid, spec):
    """Change an event in place. spec: {title?, start?, end?, location?,
    description?, all_day?, rrule?, status?, sequence?, organizer?,
    attendees?, alarms?, calendar?} — every given field replaces the
    current value, every omitted one keeps it (a field set to null is
    cleared). calendar=<name or id> moves the event to another
    calendar (PUT to the new one, DELETE from the old). Bumps SEQUENCE
    when not given. Returns (updated_event, calendar_id)."""
    calid = caldav_resolve(creds, calid)
    ev = dict(caldav_get_event(creds, calid, uid))
    ev.pop("etag", None)
    if "title" in spec:
        ev["summary"] = spec["title"]
    for k in ("start", "end", "location", "description", "rrule",
              "status", "organizer", "attendees", "alarms"):
        if k in spec:
            ev[k] = spec[k]
    if "all_day" in spec:
        ev["all_day"] = bool(spec["all_day"])
    if "sequence" not in spec:
        ev["sequence"] = (ev.get("sequence") or 0) + 1
    else:
        ev["sequence"] = int(spec["sequence"]) if spec["sequence"] is not None \
            else 0
    body = ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\n"
            "PRODID:-//pigeon//EN\r\nBEGIN:VEVENT\r\n"
            + "\r\n".join(_build_event_lines(ev))
            + "\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")
    target = caldav_resolve(creds, spec["calendar"]) \
        if spec.get("calendar") else calid
    if target != calid:
        r = requests.put(f"{caldav_base(creds[0])}/{target}/{uid}.ics",
                         auth=creds, data=body.encode(),
                         headers={"Content-Type": "text/calendar"},
                         timeout=30)
        _dbg(r, f"MOVE event {uid} -> {target}")
        if r.status_code not in (200, 201, 204):
            raise RuntimeError(f"move: HTTP {r.status_code}")
        r = requests.delete(f"{caldav_base(creds[0])}/{calid}/{uid}.ics",
                            auth=creds, timeout=30)
        _dbg(r, f"DELETE old event {calid}/{uid}")
        if r.status_code not in (200, 204):
            raise RuntimeError(f"delete old copy: HTTP {r.status_code}")
    else:
        r = requests.put(f"{caldav_base(creds[0])}/{calid}/{uid}.ics",
                         auth=creds, data=body.encode(),
                         headers={"Content-Type": "text/calendar"},
                         timeout=30)
        _dbg(r, f"PUT event {calid}/{uid}")
        if r.status_code not in (200, 201, 204):
            raise RuntimeError(f"update: HTTP {r.status_code}")
    return {k: v for k, v in ev.items() if not k.startswith("_")}, target


# ------------------------------------------------------------ contacts ----
# CardDAV (vCard 3.0), same pattern as the CalDAV section above.

def carddav_base(user):
    return (NEXTCLOUD_URL.rstrip("/") + "/remote.php/dav/addressbooks/"
            + str(user).split("@", 1)[0])


def carddav_books(creds):
    """[{name, id}] of the real address books via PROPFIND Depth 1 on
    the addressbook-home. Only entries whose resourcetype is a CardDAV
    address book count (the home set itself is not); match the
    namespace prefix independently (<anything:addressbook>)."""
    base = carddav_base(creds[0])
    prop = ("<prop xmlns:d='DAV:'>"
            "<d:displayname/><d:resourcetype/></prop>")
    body = ("<?xml version='1.0'?"
            "<d:propfind xmlns:d='DAV:'>" + prop + "</d:propfind>")
    r = requests.request("PROPFIND", base, auth=creds, data=body.encode(),
                         headers={"Depth": "1", "Content-Type": "text/xml"},
                         timeout=30)
    _dbg(r, f"PROPFIND {base}")
    if r.status_code not in (200, 207):
        raise RuntimeError(f"addressbooks: HTTP {r.status_code}")
    out = []
    for m in re.finditer(
            r"<d:response>\s*<d:href>([^<]+)</d:href>(.*?)</d:response>",
            r.text, re.S):
        url, blk = m.group(1), m.group(2)
        if not re.search(r"<\w+:addressbook[\s/>]", blk):
            continue  # the addressbook-home set itself, not a book
        dm = re.search(r"<d:displayname>([^<]*)</d:displayname>", blk)
        out.append({"name": unescape(dm.group(1)).strip() if dm else "",
                    "id": unquote(url.rstrip("/").rsplit("/", 1)[-1])
                    if "/" in url else ""})
    return out


def carddav_resolve(creds, abid):
    """abid (id or name, case-insensitive) -> the real book id. Raises
    UnknownBook (with the full list) on no match."""
    books = carddav_books(creds)
    low = abid.lower()
    for b in books:
        if low in (b["id"].lower(), b["name"].lower()):
            return b["id"]
    raise UnknownBook(abid, books)


class UnknownBook(RuntimeError):
    """abid matches no address book; .books is the full [{name, id}]."""

    def __init__(self, abid, books):
        self.abid = abid
        self.books = books
        names = ", ".join(f"{b['name']} ({b['id']})" for b in books)
        super().__init__(f"unknown address book {abid!r}; "
                         f"available: {names}")


class ContactNotFound(RuntimeError):
    """The address book has no contact with the given uri."""


def _vcf_unescape(v):
    return _ics_unescape(v)  # same escape set (RFC 6350 3.2.10)


def _vcf_escape(v):
    return _ics_escape(v)


def _vcf_unfold(text):
    """Undo RFC 6350 line folding: a line starting with space/tab
    continues the previous one."""
    return re.sub(r"\r?\n[ \t]", "", text)


def _vcf_parse(text):
    """vCard text -> ordered list of {name, params, value}; params like
    TYPE=a,b are split into lists. Unmodeled properties (PHOTO, X-*)
    are kept, so updates do not destroy them."""
    out = []
    for line in _vcf_unfold(text).splitlines():
        line = line.strip()
        if not line or line in ("BEGIN:VCARD", "END:VCARD"):
            continue
        head, _, value = line.partition(":")
        name, _, extra = head.partition(";")
        params = {}
        for part in extra.split(";"):
            k, _, v = part.partition("=")
            k = k.strip().upper()
            vals = [x.strip() for x in v.split(",") if x.strip()]
            if k in params:
                params[k] = (params[k] if isinstance(params[k], list)
                             else [params[k]]) + vals
            else:
                params[k] = vals
        out.append({"name": name.upper(), "params": params,
                    "value": value.strip()})
    return out


def _vcf_lines(props):
    """props (as from _vcf_parse) -> vCard 3.0 text."""
    lines = ["BEGIN:VCARD", "VERSION:3.0"]
    for p in props:
        if p["name"] == "VERSION":
            continue
        params = ""
        for k in sorted(p["params"]):
            v = p["params"][k]
            params += ";" + k + "=" + (",".join(v) if isinstance(v, list)
                                       else str(v))
        lines.append(p["name"] + params + ":"
                     + (_vcf_escape(p["value"])
                        if p["name"] not in ("VERSION", "UID")
                        else p["value"]))
    return "\r\n".join(lines + ["END:VCARD"]) + "\r\n"


def _vcf_view(props):
    """props -> the JSON contact (only fields that have a value)."""
    def single(name):
        for p in props:
            if p["name"] == name and p["value"].strip():
                return _vcf_unescape(p["value"].strip())
        return None

    def multi(name):
        return [{"value": _vcf_unescape(p["value"].strip()),
                 "type": p["params"].get("TYPE")}
                for p in props if p["name"] == name and p["value"].strip()]

    out = {"uri": None}
    for key, name in (("fn", "FN"), ("org", "ORG"), ("title", "TITLE"),
                      ("note", "NOTE"), ("bday", "BDAY"), ("uid", "UID")):
        v = single(name)
        if v is not None:
            out[key] = v
    for p in props:  # N:family;given;... -> "given family"
        if p["name"] == "N":
            parts = [_vcf_unescape(x).strip() for x in p["value"].split(";")]
            given = parts[1] if len(parts) > 1 else ""
            family = parts[0] if parts else ""
            if given or family:
                out["name"] = (given + " " + family).strip()
            break
    for key, name in (("email", "EMAIL"), ("tel", "TEL"), ("url", "URL")):
        v = multi(name)
        if v:
            out[key] = v
    cats = single("CATEGORIES")
    if cats:
        out["categories"] = [c.strip() for c in cats.split(",") if c.strip()]
    return out


def _as_list(v):
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def _vcf_drop(props, name):
    props[:] = [p for p in props if p["name"] != name]


def _spec_to_props(spec):
    """create spec -> (vCard 3.0 props, uid); UID is generated."""
    uid = uuid.uuid4().hex
    props = [{"name": "UID", "params": {}, "value": uid},
             {"name": "FN", "params": {},
              "value": str(spec.get("fn") or "").strip()}]
    for key, name in (("org", "ORG"), ("title", "TITLE"), ("note", "NOTE"),
                      ("bday", "BDAY")):
        if spec.get(key):
            props.append({"name": name, "params": {},
                          "value": str(spec[key]).strip()})
    for key, name in (("email", "EMAIL"), ("tel", "TEL"), ("url", "URL")):
        for v in _as_list(spec.get(key)):
            v = str(v).strip()
            if v:
                props.append({"name": name, "params": {}, "value": v})
    return props, uid


def carddav_list(creds, abid, query=None):
    """[{uri, fn, modified}] via PROPFIND Depth 1 (the answer includes
    the book itself — filtered as a collection). query: case-
    insensitive substring on the contact name, client-side."""
    abid = carddav_resolve(creds, abid)
    base = carddav_base(creds[0]) + "/" + abid
    prop = ("<prop xmlns:d='DAV:'><d:displayname/><d:resourcetype/>"
            "<d:getlastmodified/></prop>")
    body = ("<?xml version='1.0'?"
            "<d:propfind xmlns:d='DAV:'>" + prop + "</d:propfind>")
    r = requests.request("PROPFIND", base, auth=creds, data=body.encode(),
                         headers={"Depth": "1", "Content-Type": "text/xml"},
                         timeout=30)
    _dbg(r, f"PROPFIND {base}")
    if r.status_code not in (200, 207):
        raise RuntimeError(f"list contacts: HTTP {r.status_code}")
    out = []
    q = (query or "").lower()
    for m in re.finditer(
            r"<d:response>\s*<d:href>([^<]+)</d:href>(.*?)</d:response>",
            r.text, re.S):
        url, blk = m.group(1), m.group(2)
        if re.search(r"<\w+:collection[\s/>]", blk):
            continue  # the book itself (Depth 1 includes it)
        dm = re.search(r"<d:displayname>([^<]*)</d:displayname>", blk)
        fn = unescape(dm.group(1)).strip() if dm else ""
        uri = unquote(url.rstrip("/").rsplit("/", 1)[-1])
        if uri.lower().endswith(".vcf"):
            uri = uri[:-4]
        if not uri or (q and q not in fn.lower()):
            continue
        gm = re.search(r"<d:getlastmodified>([^<]*)</d:getlastmodified>",
                       blk)
        out.append({"uri": uri, "fn": fn,
                    "modified": gm.group(1) if gm else None})
    return out


def carddav_get(creds, abid, uri):
    """One contact, fully parsed (fn, name, org, title, email[], tel[],
    url[], bday, note, categories, uid, uri)."""
    abid = carddav_resolve(creds, abid)
    r = requests.get(f"{carddav_base(creds[0])}/{abid}/{uri}.vcf",
                     auth=creds, timeout=30)
    _dbg(r, f"GET contact {abid}/{uri}")
    if r.status_code == 404:
        raise ContactNotFound(uri)
    if r.status_code >= 400:
        raise RuntimeError(f"get contact: HTTP {r.status_code}")
    out = _vcf_view(_vcf_parse(r.text))
    out["uri"] = uri
    return out


def carddav_create(creds, abid, spec):
    """spec (JSON: fn required; org/title/note/bday; email/tel/url as
    string or list) -> new vCard PUT. Returns the contact uri."""
    abid = carddav_resolve(creds, abid)
    if not str(spec.get("fn") or "").strip():
        raise ValueError("fn (display name) is required")
    props, uid = _spec_to_props(spec)
    r = requests.put(f"{carddav_base(creds[0])}/{abid}/{uid}.vcf",
                     data=_vcf_lines(props).encode(), auth=creds,
                     headers={"Content-Type": "text/vcard"}, timeout=30)
    _dbg(r, f"PUT contact {abid}/{uid}")
    if r.status_code not in (200, 201, 204):
        raise RuntimeError(f"create contact: HTTP {r.status_code}")
    return uid


def carddav_update(creds, abid, uri, spec):
    """Change a contact: {fn?, org?, title?, note?, bday?, email?,
    tel?, url?} — every given field replaces the current one (null
    clears it), every omitted one keeps it. Unmodeled properties
    (photo etc.) survive. Returns the updated contact."""
    abid = carddav_resolve(creds, abid)
    r = requests.get(f"{carddav_base(creds[0])}/{abid}/{uri}.vcf",
                     auth=creds, timeout=30)
    if r.status_code == 404:
        raise ContactNotFound(uri)
    if r.status_code >= 400:
        raise RuntimeError(f"get contact: HTTP {r.status_code}")
    props = _vcf_parse(r.text)
    for key, name in (("fn", "FN"), ("org", "ORG"), ("title", "TITLE"),
                      ("note", "NOTE"), ("bday", "BDAY")):
        if key in spec:
            _vcf_drop(props, name)
            if spec[key]:
                props.append({"name": name, "params": {},
                              "value": str(spec[key]).strip()})
    for key, name in (("email", "EMAIL"), ("tel", "TEL"), ("url", "URL")):
        if key in spec:
            _vcf_drop(props, name)
            for v in _as_list(spec[key]):
                v = str(v).strip()
                if v:
                    props.append({"name": name, "params": {}, "value": v})
    _vcf_drop(props, "VERSION")  # rewritten by _vcf_lines
    r = requests.put(f"{carddav_base(creds[0])}/{abid}/{uri}.vcf",
                     data=_vcf_lines(props).encode(), auth=creds,
                     headers={"Content-Type": "text/vcard"}, timeout=30)
    _dbg(r, f"PUT contact {abid}/{uri}")
    if r.status_code not in (200, 201, 204):
        raise RuntimeError(f"update contact: HTTP {r.status_code}")
    return carddav_get(creds, abid, uri)


def carddav_delete(creds, abid, uri):
    abid = carddav_resolve(creds, abid)
    r = requests.delete(f"{carddav_base(creds[0])}/{abid}/{uri}.vcf",
                        auth=creds, timeout=30)
    _dbg(r, f"DELETE contact {abid}/{uri}")
    if r.status_code not in (200, 204):
        raise RuntimeError(f"delete contact: HTTP {r.status_code}")
    return uri
