#!/usr/bin/env python3
"""Fake Nextcloud for local testing: accepts spreed bot API messages
and verifies outgoing bot API signatures; serves WebDAV SEARCH
(oc:fileid -> href) + authenticated file GETs like a real NC34 instance.
Run standalone or from smoke.py."""
import base64
import hashlib
import hmac
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class H(BaseHTTPRequestHandler):
    # every bot API message received (room token, message) — for tests
    received = []
    # Basic auth for WebDAV: {user: password}
    share_auth = {}
    # every dav SEARCH seen: (authed_user or None, fileid)
    search_calls = []
    # WebDAV path of the searched file (what SEARCH resolves to)
    dav_href = "/remote.php/dav/files/ncuser/Talk/demo/file.webp"
    file_bytes = b"FAKE-IMAGE-BYTES-1234"
    # /nc proxy store: WebDAV files {path: bytes}, CalDAV calendars+events
    files = {"Temp/notes.txt": b"hello nextcloud"}  # capital T, like real NC
    calendars = {"family": "Family", "work": "Work", "privat": "Privat",
                 "Privat (2)": "Privat2"}
    events = {"family": [{"uid": "ev1", "summary": "Old thing",
                          "start": "2000-01-01T08:00:00+01:00"}],
               "work": [], "privat": [], "Privat (2)": []}
    # CardDAV: address books {id: display name}, contacts {book: {uri: vcf}}
    # (the z- book is a hidden system book — the app must skip it)
    addressbooks = {"personal": "Personal", "business": "Business",
                    "z-app-generated--recent": "Zuletzt kontaktiert"}
    contacts = {"personal": {
                    "jane@acme": ("BEGIN:VCARD\r\nVERSION:3.0\r\n"
                                  "UID:jane@acme\r\nFN:Jane Doe\r\n"
                                  "N:Doe;Jane;;;\r\nORG:ACME\r\n"
                                  "TEL;TYPE=CELL:+43 660 1234567\r\n"
                                  "EMAIL;TYPE=WORK:jane@acme.example\r\n"
                                  "END:VCARD\r\n")},
                 "business": {}}

    @staticmethod
    def _vcf_fn(vcf):
        m = re.search(r"^FN:([^\r\n]*)",
                      re.sub(r"\r?\n[ \t]", "", vcf), re.M)
        return m.group(1).strip() if m else ""

    @staticmethod
    def _ab_sub(path):
        """Path part after the addressbook home (<user>/ or the NC-34
        users/<user>/ form); '' on the home itself, None elsewhere."""
        m = re.match(r"^/remote\.php/dav/addressbooks/"
                     r"(?:users/)?[^/]+(?:/(.*))?$",
                     path.rstrip("/"))
        return (m.group(1) or "") if m else None

    @staticmethod
    def _unesc(v):
        return v.replace("\\n", "\n").replace("\\,", ",") \
            .replace("\\;", ";").replace("\\\\", "\\")

    @staticmethod
    def _esc(v):
        v = v or ""
        return v.replace("\\", "\\\\").replace(";", "\\;") \
            .replace(",", "\\,").replace("\n", "\\n")

    @staticmethod
    def _event_ics(e):
        """Full ICS for one stored event (everything it has)."""
        ics = ["BEGIN:VCALENDAR", "VERSION:2.0",
               "PRODID:-//fake-nc//EN", "BEGIN:VEVENT",
               "UID:" + e["uid"], "DTSTAMP:20260101T000000Z",
               "DTSTART:" + e.get("start", "20260101T000000Z")]
        if e.get("end"):
            ics.append("DTEND:" + e["end"])
        ics.append("SUMMARY:" + H._esc(e.get("summary", "")))
        for k in ("location", "description", "rrule", "status"):
            if e.get(k):
                ics.append(k.upper() + ":" + (H._esc(e[k])
                            if k in ("location", "description") else e[k]))
        if e.get("sequence") is not None:
            ics.append("SEQUENCE:" + str(e["sequence"]))
        if e.get("organizer"):
            ics.append("ORGANIZER:mailto:" + e["organizer"])
        for a in e.get("attendees") or []:
            ics.append("ATTENDEE;CN=%s:mailto:%s" % (a.get("cn", ""),
                                                     a["email"]))
        for al in e.get("alarms") or []:
            ics.append("BEGIN:VALARM")
            ics.append("TRIGGER:" + (("-" if al["minutes"] < 0 else "+")
                         + "PT%dM" % abs(al["minutes"])
                         if al.get("minutes") is not None
                         else al.get("trigger", "")))
            ics.append("ACTION:" + al.get("action", "DISPLAY"))
            if al.get("description"):
                ics.append("DESCRIPTION:" + H._esc(al["description"]))
            ics.append("END:VALARM")
        ics += ["END:VEVENT", "END:VCALENDAR"]
        return ("\r\n".join(ics) + "\r\n").encode()

    @staticmethod
    def _parse_event(data, path):
        """ICS body -> stored event dict (everything the client can send)."""
        uid = path.rstrip("/").rsplit("/", 1)[-1].replace(".ics", "")
        ev = {"uid": uid, "summary": "", "start": "", "end": "",
              "location": "", "description": "", "rrule": "", "status": "",
              "sequence": None, "organizer": "", "attendees": [],
              "alarms": []}
        alarms, cur = [], None
        for line in data.decode("utf-8", "replace").replace("\r\n", "\n").split("\n"):
            if line.startswith("BEGIN:VALARM"):
                cur = {"action": "DISPLAY", "minutes": None, "description": ""}
                continue
            if line.startswith("END:VALARM"):
                if cur:
                    alarms.append(cur)
                cur = None
                continue
            if cur is not None:
                if line.startswith("TRIGGER:"):
                    v = line[8:].strip()
                    if re.match(r"^[+-]P", v):
                        m2 = re.fullmatch(
                            r"[+-]P(?:(\d+)D)?"
                            r"(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", v)
                        sign = -1 if v.startswith("-") else 1
                        hours = int(m2.group(2) or 0)
                        mnt = int(m2.group(3) or 0)
                        sec = int(m2.group(4) or 0)
                        cur["minutes"] = sign * (hours * 60 + mnt
                                                 + sec // 60)
                    else:
                        cur["trigger"] = v.replace(":", "")
                elif line.startswith("ACTION:"):
                    cur["action"] = line[7:].strip().upper()
                elif line.startswith("DESCRIPTION:"):
                    cur["description"] = H._unesc(line[12:].strip())
                continue
            for key, tag in (("summary", "SUMMARY"), ("start", "DTSTART"),
                             ("end", "DTEND"), ("location", "LOCATION"),
                             ("description", "DESCRIPTION"),
                             ("rrule", "RRULE"), ("status", "STATUS"),
                             ("organizer", "ORGANIZER")):
                if line.startswith(tag + ":"):
                    v = H._unesc(line[len(tag) + 1:].strip())
                    ev[key] = v.split(":", 1)[-1] if key == "organizer" \
                        else v
                    break
            else:
                m2 = re.match(r"^SEQUENCE:(\d+)$", line)
                if m2:
                    ev["sequence"] = int(m2.group(1))
                    continue
                m2 = re.match(r"^ATTENDEE(;CN=([^;]*))?:mailto:(.+)$", line)
                if m2:
                    ev["attendees"].append({"cn": (m2.group(2) or "").strip(),
                                            "email": m2.group(3).strip()})
        ev["alarms"] = alarms
        ev["ics"] = data
        return ev

    put_calls = []  # (method, path) for the /nc proxy tests
    # /v1/audio/transcriptions (STT): calls recorded as
    # (authorization_header, model, filename); expected api key
    stt_calls = []
    stt_key = "Bearer TEST-KEY"
    stt_text = "hallo, das ist eine test-sprachnachricht"
    # bot API posts recorded with their silent flag: (room_token, silent)
    bot_silent = []
    # when non-empty, bot API messages containing this substring fail
    # with 500 (to test that an ack failure does not break the run)
    post_fail = ""

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _xml_error(self, message):
        # like real sabre: XML 404 NotFound, not JSON
        body = ('<?xml version="1.0" encoding="utf-8"?>'
                '<d:error xmlns:d="DAV:" '
                'xmlns:s="http://sabredav.org/ns">'
                '<s:exception>Sabre\\DAV\\Exception\\NotFound</s:exception>'
                f"<s:message>{message}</s:message></d:error>").encode()
        self.send_response(404)
        self.send_header("Content-Type", "application/xml; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(n).decode()

    def _basic_auth(self):
        raw = self.headers.get("Authorization", "")
        if not raw.startswith("Basic "):
            return None
        try:
            user, _, pw = base64.b64decode(raw[6:]).decode().partition(":")
            return user, pw
        except Exception:
            return None

    def do_GET(self):
        if self.path.startswith("/remote.php/webdav/"):
            auth = self._basic_auth()
            ok = auth is not None and H.share_auth.get(auth[0]) == auth[1]
            if not ok:
                self._json({"error": "unauthorized"}, code=401)
                return
            data = H.files.get(self.path[len("/remote.php/webdav/"):])
            if data is None:
                self._json({"error": "not found"}, code=404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if self.path.startswith("/remote.php/dav/files/"):
            auth = self._basic_auth()
            ok = auth is not None and H.share_auth.get(auth[0]) == auth[1]
            if not ok:
                self._json({"error": "unauthorized"}, code=401)
                return
            body = H.file_bytes
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/remote.php/dav/calendars/"):
            if not self._dav_ok():
                self._json({"error": "unauthorized"}, code=401)
                return
            H.put_calls.append(("GET", self.path))
            parts = self.path.rstrip("/").split("/")
            if len(parts) == 7:  # one event: <user>/<cal>/<uid>.ics
                cal = parts[-2]
                e = next((x for x in H.events.get(cal, [])
                          if x["uid"] == parts[-1].replace(".ics", "")),
                         None)
                if e is None:
                    self._xml_error(f'File with name {parts[-1]}'
                                    ' could not be located')
                    return
                raw = e.get("ics") or H._event_ics(e)
                self.send_response(200)
                self.send_header("Content-Type", "text/calendar")
                self.send_header("ETag", '"fake-' + e["uid"] + '"')
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return
            cal = parts[-1]
            inner = "".join(
                H._event_ics(e).decode()
                .replace("BEGIN:VCALENDAR\r\n", "")
                .replace("END:VCALENDAR\r\n", "")
                .replace("\r\n", "\r\n")
                for e in H.events.get(cal, []))
            ics = ("BEGIN:VCALENDAR\r\n" + inner + "END:VCALENDAR\r\n")
            raw = ics.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/calendar")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        if self.path.startswith("/remote.php/dav/addressbooks/"):
            if not self._dav_ok():
                self._json({"error": "unauthorized"}, code=401)
                return
            H.put_calls.append(("GET", self.path))
            sub = H._ab_sub(self.path)
            if sub and "/" in sub:  # one contact: <book>/<uri>.vcf
                book, file = sub.split("/", 1)
                uri = file.replace(".vcf", "")
                vcf = H.contacts.get(book, {}).get(uri)
                if vcf is None:
                    self._xml_error(f'File with name {file}'
                                    ' could not be located')
                    return
                raw = vcf.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/vcard")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return
            self._json({"error": "unexpected"}, code=400)
        self._json({"ok": True})

    def do_REPORT(self):
        body = self._body()
        if not self._dav_ok():
            self._json({"error": "unauthorized"}, code=401)
            return
        sub = H._ab_sub(self.path) if self.path.startswith(
            "/remote.php/dav/addressbooks/") else None
        qm = re.search(r"<c:text-match[^>]*>([^<]*)</c:text-match>", body)
        if sub and "/" not in sub and qm:
            # CardDAV SEARCH on FN — like the live server: the match
            # hrefs come back, but d:displayname is 404 (names must be
            # fetched from the cards themselves).
            book = sub
            q = qm.group(1).lower()
            hits = [u for u, v in H.contacts.get(book, {}).items()
                    if q in H._vcf_fn(v).lower()]
            resp = "".join(
                f"<d:response><d:href>/remote.php/dav/"
                f"addressbooks/u/{book}/{u}.vcf</d:href>"
                '<d:propstat><d:prop><d:getlastmodified>'
                'Tue, 01 Jan 2026 00:00:00 GMT</d:getlastmodified>'
                '</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat>'
                '<d:propstat><d:prop><d:displayname/></d:prop>'
                '<d:status>HTTP/1.1 404 Not Found</d:status></d:propstat>'
                '</d:response>' for u in hits)
            xml = ('<d:multistatus xmlns:d="DAV:" '
                   'xmlns:crd="urn:ietf:params:xml:ns:carddav">'
                   + resp + "</d:multistatus>")
            raw = xml.encode()
            self.send_response(207)
            self.send_header("Content-Type", "application/xml")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        self._xml_error("REPORT not supported")

    def do_SEARCH(self):
        body = self._body()
        m = re.search(r"<d:literal>(\d+)</d:literal>", body)
        file_id = int(m.group(1)) if m else None
        auth = self._basic_auth()
        ok = auth is not None and H.share_auth.get(auth[0]) == auth[1]
        H.search_calls.append((auth[0] if auth else None, file_id))
        if not ok:
            self._json({"error": "unauthorized"}, code=401)
            return
        xml = ('<?xml version="1.0"?>'
               '<d:multistatus xmlns:d="DAV:" '
               'xmlns:oc="http://owncloud.org/ns"><d:response>'
               f"<d:href>{H.dav_href}</d:href>"
               "<d:propstat><d:prop><d:displayname>file.webp</d:displayname>"
               f"<oc:fileid>{file_id}</oc:fileid></d:prop>"
               "<d:status>HTTP/1.1 200 OK</d:status></d:propstat>"
               "</d:response></d:multistatus>")
        raw = xml.encode()
        self.send_response(207)
        self.send_header("Content-Type", "application/xml")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _dav_ok(self):
        auth = self._basic_auth()
        return auth is not None and H.share_auth.get(auth[0]) == auth[1]

    def do_PROPFIND(self):
        if not self._dav_ok():
            self._json({"error": "unauthorized"}, code=401)
            return
        H.put_calls.append(("PROPFIND", self.path))
        if self.path.startswith("/remote.php/dav/calendars/"):
            # /remote.php/dav/calendars/<user>            -> principal
            # /remote.php/dav/calendars/<user>/<cal>[/]   -> one calendar
            parts = self.path.rstrip("/").split("/")
            if len(parts) == 5:
                # Depth-1 on the principal: list the calendars.
                # Nextcloud's answer contains the calendar-home
                # collection itself first (resourcetype
                # calendar-home-set, NOT a calendar — accessing it
                # 404s), then one response per real calendar
                # (resourcetype c:calendar). Note: the prefix here is
                # 'cal', deliberately different from the 'c' the client
                # requests — servers pick their own prefixes.
                xml = ('<d:multistatus xmlns:d="DAV:" '
                       'xmlns:cal="urn:ietf:params:xml:ns:caldav">'
                       '<d:response><d:href>/remote.php/dav/calendars/u/'
                       '</d:href><d:propstat><d:prop>'
                       '<d:displayname>u</d:displayname>'
                       '<d:resourcetype><d:collection/>'
                       '<cal:calendar-home-set/></d:resourcetype></d:prop>'
                       '<d:status>HTTP/1.1 200 OK</d:status>'
                       '</d:propstat></d:response>'
                       + "".join(
                           f"<d:response><d:href>/remote.php/dav/calendars/"
                           f"u/{c}/</d:href><d:propstat><d:prop>"
                           f"<d:displayname>{n}</d:displayname>"
                           f"<d:resourcetype><d:collection/>"
                           f"<cal:calendar/></d:resourcetype></d:prop>"
                           f"<d:status>HTTP/1.1 200 OK</d:status>"
                           f"</d:propstat></d:response>"
                           for c, n in H.calendars.items())
                       + "</d:multistatus>")
            else:
                cal = parts[-1]
                # Depth-1 on one calendar: one response per event,
                # ICS body as cal:calendar-data (XML escaped) — this is
                # what pigeon fetches with (plain GET answers a
                # placeholder HTML page, REPORTs are broken).
                from xml.sax.saxutils import escape
                xml = ('<d:multistatus xmlns:d="DAV:" '
                       'xmlns:cal="urn:ietf:params:xml:ns:caldav">'
                       + "".join(
                           '<d:response><d:href>/remote.php/dav/'
                           f'calendars/u/{cal}/{e["uid"]}.ics</d:href>'
                           '<d:propstat><d:prop>'
                           '<cal:calendar-data>'
                           + escape(
                               "BEGIN:VCALENDAR\r\nVERSION:2.0\r\n"
                               "BEGIN:VEVENT\r\n"
                               f'UID:{e["uid"]}\r\n'
                               f'SUMMARY:{e.get("summary", "")}\r\n'
                               f'DTSTART:{e.get("start", "20260101T000000Z")}\r\n'
                               f'DTEND:{e.get("end", "20260101T010000Z")}\r\n'
                               "END:VEVENT\r\nEND:VCALENDAR\r\n")
                           + '</cal:calendar-data></d:prop>'
                           '<d:status>HTTP/1.1 200 OK</d:status>'
                           '</d:propstat></d:response>'
                           for e in H.events.get(cal, []))
                       + "</d:multistatus>")
        elif self.path.startswith("/remote.php/dav/addressbooks/"):
            # Home forms: <user>/ or users/<user>/ (NC 34); books and
            # contacts sit under either. sub = the part after the home.
            sub = H._ab_sub(self.path)
            if not sub:
                # Depth-1 on the principal: the addressbook-home set
                # (resourcetype addressbook-home-set, NOT a book — the
                # client must filter it) + one response per book.
                xml = ('<d:multistatus xmlns:d="DAV:" '
                       'xmlns:crd="urn:ietf:params:xml:ns:carddav">'
                       '<d:response><d:href>/remote.php/dav/'
                       'addressbooks/u/</d:href><d:propstat><d:prop>'
                       '<d:displayname>u</d:displayname>'
                       '<d:resourcetype><d:collection/>'
                       '<crd:addressbook-home-set/></d:resourcetype>'
                       '</d:prop><d:status>HTTP/1.1 200 OK</d:status>'
                       '</d:propstat></d:response>'
                       + "".join(
                           f"<d:response><d:href>/remote.php/dav/"
                           f"addressbooks/users/u/{b}/</d:href>"
                           '<d:propstat><d:prop>'
                           f"<d:displayname>{n}</d:displayname>"
                           '<d:resourcetype><d:collection/>'
                           '<crd:addressbook/></d:resourcetype>'
                           '<d:status>HTTP/1.1 200 OK</d:status>'
                           '</d:propstat></d:response>'
                           for b, n in H.addressbooks.items())
                       + "</d:multistatus>")
            elif "/" not in sub:
                book = sub
                # Depth-1 on one book: the book itself (a collection —
                # the client filters it) + one response per contact
                # (displayname = FN, no resourcetype).
                resp = (f"<d:response><d:href>/remote.php/dav/"
                        f"addressbooks/users/u/{book}/</d:href>"
                        '<d:propstat><d:prop>'
                        f"<d:displayname>"
                        f"{H.addressbooks.get(book, '')}</d:displayname>"
                        '<d:resourcetype><d:collection/>'
                        '<crd:addressbook/></d:resourcetype>'
                        '<d:status>HTTP/1.1 200 OK</d:status>'
                        '</d:propstat></d:response>')
                for uri, vcf in H.contacts.get(book, {}).items():
                    resp += (f"<d:response><d:href>/remote.php/dav/"
                             f"addressbooks/users/u/{book}/{uri}.vcf"
                             f"</d:href>"
                             '<d:propstat><d:prop>'
                             f"<d:displayname>"
                             f"{H._vcf_fn(vcf)}</d:displayname>"
                             '<d:getlastmodified>'
                             'Tue, 01 Jan 2026 00:00:00 GMT'
                             '</d:getlastmodified>'
                             '<d:status>HTTP/1.1 200 OK</d:status>'
                             '</d:propstat></d:response>')
                xml = ('<d:multistatus xmlns:d="DAV:" '
                       'xmlns:crd="urn:ietf:params:xml:ns:carddav">'
                       + resp + "</d:multistatus>")
            else:
                self._json({"error": "unexpected"}, code=400)
                return
        else:  # webdav dir listing: direct children (files + folders)
            root = self.path[len("/remote.php/webdav/"):].rstrip("/")
            under = [p for p in sorted(H.files)
                     if not root or p.startswith(root + "/")]
            if root and not under:
                # real sabre: unknown dir -> 404 NotFound
                self._xml_error(f'File with name {root} could not be located')
                return
            dirs, files = set(), []
            for p in under:
                rest = p[len(root) + 1:] if root else p
                first, _, after = rest.partition("/")
                if after:
                    dirs.add(first)
                else:
                    files.append((first, p))
            resp = []
            for name in sorted(dirs):
                resp.append(
                        f"<d:response><d:href>/remote.php/webdav/{root + '/' if root else ''}{name}/"
                        f"</d:href><d:propstat><d:prop>"
                        f"<d:displayname>{name}</d:displayname>"
                        f"<d:resourcetype><d:collection/></d:resourcetype>"
                        f"</d:prop><d:status>HTTP/1.1 200 OK</d:status>"
                        f"</d:propstat></d:response>")
            for name, p in files:
                resp.append(
                        f"<d:response><d:href>/remote.php/webdav/{p}"
                        f"</d:href><d:propstat><d:prop>"
                        f"<d:displayname>{name}</d:displayname>"
                        f"<d:getcontentlength>{len(H.files[p])}"
                        f"</d:getcontentlength>"
                        f"<d:getlastmodified>Tue, 01 Jan 2026 00:00:00 GMT"
                        f"</d:getlastmodified>"
                        f"</d:prop><d:status>HTTP/1.1 200 OK</d:status>"
                        f"</d:propstat></d:response>")
            xml = ('<d:multistatus xmlns:d="DAV:">'
                   + "".join(resp) + "</d:multistatus>")
        raw = xml.encode()
        self.send_response(207)
        self.send_header("Content-Type", "application/xml")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_PUT(self):
        n = int(self.headers.get("Content-Length", 0))
        data = self.rfile.read(n)
        if not self._dav_ok():
            self._json({"error": "unauthorized"}, code=401)
            return
        H.put_calls.append(("PUT", self.path))
        if self.path.startswith("/remote.php/webdav/"):
            H.files[self.path[len("/remote.php/webdav/"):]] = data
            self.send_response(201)
            self.end_headers()
        elif self.path.startswith("/remote.php/dav/calendars/"):
            parts = self.path.rstrip("/").split("/")
            cal = parts[-2]
            ev = H._parse_event(data, self.path)
            lst = H.events.setdefault(cal, [])
            lst[:] = [x for x in lst if x["uid"] != ev["uid"]]
            lst.append(ev)
            self.send_response(201)
            self.end_headers()
        elif self.path.startswith("/remote.php/dav/addressbooks/"):
            sub = H._ab_sub(self.path)
            if sub and "/" in sub:
                book, file = sub.split("/", 1)
                uri = file.replace(".vcf", "")
                H.contacts.setdefault(book, {})[uri] = data.decode("utf-8")
            self.send_response(201)
            self.end_headers()
        else:
            self._json({"ok": True}, code=204)

    def do_DELETE(self):
        if not self._dav_ok():
            self._json({"error": "unauthorized"}, code=401)
            return
        H.put_calls.append(("DELETE", self.path))
        if self.path.startswith("/remote.php/webdav/"):
            H.files.pop(self.path[len("/remote.php/webdav/"):], None)
        elif self.path.startswith("/remote.php/dav/calendars/"):
            parts = self.path.rstrip("/").split("/")
            cal, uid = parts[-2], parts[-1].replace(".ics", "")
            H.events[cal] = [e for e in H.events.get(cal, [])
                             if e["uid"] != uid]
        elif self.path.startswith("/remote.php/dav/addressbooks/"):
            sub = H._ab_sub(self.path)
            if sub and "/" in sub:
                book, file = sub.split("/", 1)
                H.contacts.get(book, {}).pop(file.replace(".vcf", ""),
                                             None)
        self._json({"ok": True}, code=204)

    def do_POST(self):
        if self.path.rstrip("/") == "/v1/audio/transcriptions":
            n = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(n)  # binary multipart — keep as bytes
            auth = self.headers.get("Authorization", "")
            model = re.search(rb'name="model"\r\n\r\n([^\r]+)', raw)
            fname = re.search(rb'filename="([^"]+)"', raw)
            H.stt_calls.append(
                (auth,
                 model.group(1).decode() if model else None,
                 fname.group(1).decode() if fname else None))
            if auth != H.stt_key:
                self._json({"error": "unauthorized"}, code=401)
                return
            self._json({"text": H.stt_text})
            return
        body = self._body()
        if "/ocs/v2.php/apps/spreed/api/v1/bot/" in self.path \
                and self.path.endswith("/message"):
            if getattr(H, "secret", None):
                # verify signature: HMAC-SHA256(random + raw message, secret)
                try:
                    message = json.loads(body).get("message", "")
                except json.JSONDecodeError:
                    message = None
                random_str = self.headers.get("X-Nextcloud-Talk-Bot-Random", "")
                signature = self.headers.get("X-Nextcloud-Talk-Bot-Signature", "")
                expected = (message is not None and hmac.new(
                    H.secret.encode(), (random_str + message).encode(),
                    hashlib.sha256).hexdigest() == signature)
                if not expected:
                    print("STUB bot-api  BAD SIGNATURE", flush=True)
                    self._json({"error": "unauthenticated"}, code=401)
                    return
            if H.post_fail and H.post_fail in (message or ""):
                print("STUB bot-api  forced 500 (post_fail)", flush=True)
                self._json({"error": "unavailable"}, code=500)
                return
            print("STUB bot-api  sig-ok msg=" + json.dumps(body)[:60],
                  flush=True)
            H.received.append((self.path.split("/bot/")[1].split("/")[0],
                               message))
            H.bot_silent.append(
                (self.path.split("/bot/")[1].split("/")[0],
                 bool(json.loads(body).get("silent", False))))
            self._json({"ok": True}, code=201)
        else:
            self._json({"ok": True})

    def log_message(self, *a):
        pass


def make_server(port=8099, secret=None):
    H.secret = secret
    s = ThreadingHTTPServer(("127.0.0.1", port), H)
    s.daemon_threads = True
    return s


if __name__ == "__main__":
    make_server().serve_forever()
