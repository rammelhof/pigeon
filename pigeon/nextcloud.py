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


def caldav_calendars(creds):
    """[{name, id}] via PROPFIND with Depth 1."""
    base = caldav_base(creds[0])
    prop = ("<prop xmlns:d='DAV:' xmlns:c='urn:ietf:params:xml:ns:caldav'>"
            "<d:displayname/><c:calendar-color/></prop>")
    body = ("<?xml version='1.0'?>"
            "<d:propfind xmlns:d='DAV:'>" + prop + "</d:propfind>")
    r = requests.request("PROPFIND", base, auth=creds, data=body.encode(),
                         headers={"Depth": "1", "Content-Type": "text/xml"},
                         timeout=30)
    if r.status_code not in (200, 207):
        raise RuntimeError(f"calendars: HTTP {r.status_code}")
    out = []
    for m in re.finditer(
            r"<d:response>\s*<d:href>([^<]+)</d:href>.*?<d:displayname>"
            r"([^<]*)</d:displayname>.*?</d:response>",
            r.text, re.S):
        url = m.group(1)
        out.append({"name": m.group(2).strip(),
                    "id": url.rstrip("/").rsplit("/", 1)[-1] if "/" in url
                    else ""})
    return out


def caldav_events(creds, calid, t_from, t_to):
    """Events of a calendar in [t_from, t_to) as plain dicts (RFC3339 in)."""
    body = ("<?xml version='1.0'?>"
            "<c:calendar-query xmlns:c='urn:ietf:params:xml:ns:caldav'"
            " xmlns:d='DAV:'><d:prop><d:getetag/><c:calendar-data/>"
            "</d:prop><c:filter><c:comp-filter name='VCALENDAR'>"
            "<c:comp-filter name='VEVENT'><c:time-range"
            f" start='{t_from}' end='{t_to}'/></c:comp-filter>"
            "</c:comp-filter></c:filter></c:calendar-query>")
    r = requests.request("REPORT", f"{caldav_base(creds[0])}/{calid}/",
                         auth=creds, data=body.encode(),
                         headers={"Depth": "1",
                                  "Content-Type": "text/xml"}, timeout=30)
    if r.status_code not in (200, 207):
        raise RuntimeError(f"events: HTTP {r.status_code}")
    evs = []
    for m in re.finditer(r"BEGIN:VEVENT\s(.*?)END:VEVENT", r.text, re.S):
        blk = m.group(1)

        def g(tag):
            mm = re.search(r"^" + tag + r"[;:](.*)$", blk, re.M | re.I)
            return mm.group(1).strip() if mm else ""
        evs.append({"summary": g("SUMMARY"), "start": g("DTSTART"),
                    "end": g("DTEND"), "location": g("LOCATION"),
                    "description": g("DESCRIPTION")})
    return evs


def caldav_create(creds, calid, spec):
    """spec (JSON: title/start/end/location/description/attendees) -> ICS PUT.
    Returns the event UID."""
    uid = uuid.uuid4().hex

    def dt(s):
        # a bare "YYYY-MM-DD" becomes a local all-day date
        return s[10:].replace(":", "") + "Z" \
            if re.match(r"^\d{4}-\d{2}-\d{2}$", s) else s

    st = spec["start"].replace("Z", "Z")
    ics = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//pigeon//EN",
           "BEGIN:VEVENT", f"UID:{uid}", f"DTSTAMP:{st}", "DTSTART:" + dt(st)]
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
