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
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer


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
    put_calls = []  # (method, path) for the /nc proxy tests
    # /v1/audio/transcriptions (STT): calls recorded as
    # (authorization_header, model, filename); expected api key
    stt_calls = []
    stt_key = "Bearer TEST-KEY"
    stt_text = "hallo, das ist eine test-sprachnachricht"
    # Talk chat web API (live thinking bubble): room-by-token lookup
    # returns this numeric id; sends/edits are recorded
    chat_room_id = 5757
    chat_lookups = []   # (room_token, authed_user)
    chat_sent = []      # (room_id, message, silent)
    chat_edits = []     # (room_id, message_id, message)
    _chat_next_id = 9000
    _chat_counter_lock = None

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
        m = re.fullmatch(
            r"/ocs/v2\.php/apps/spreed/api/v4/room/([^/?]+)", self.path)
        if m:
            auth = self._basic_auth()
            ok = auth is not None and H.share_auth.get(auth[0]) == auth[1]
            H.chat_lookups.append((m.group(1), auth[0] if auth else None))
            if not ok:
                self._json({"error": "unauthorized"}, code=401)
                return
            self._json({"ocs": {"meta": {"status": "ok"},
                                "data": {"id": H.chat_room_id,
                                         "token": m.group(1)}}})
            return
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
            cal = self.path.rstrip("/").rsplit("/", 1)[-1]
            ics = ("BEGIN:VCALENDAR\r\n" + "".join(
                "BEGIN:VEVENT\r\nUID:%s\r\nSUMMARY:%s\r\n"
                "DTSTART:%s\r\nEND:VEVENT\r\n" % (e["uid"], e["summary"],
                                                  e["start"])
                for e in H.events.get(cal, [])) + "END:VCALENDAR\r\n")
            raw = ics.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/calendar")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        self._json({"ok": True})

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
        m = re.fullmatch(
            r"/ocs/v2\.php/apps/spreed/api/v4/chat/(\d+)/(\d+)", self.path)
        if m:
            if not self._dav_ok():
                self._json({"error": "unauthorized"}, code=401)
                return
            text = urllib.parse.parse_qs(self._body()).get("message", [""])[0]
            H.chat_edits.append((int(m.group(1)), int(m.group(2)), text))
            self._json({"ocs": {"meta": {"status": "ok"},
                                "data": {"id": int(m.group(2)),
                                         "message": text}}})
            return
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
            uid = parts[-1].replace(".ics", "")
            # parse SUMMARY/DTSTART out of the ICS body so GET can serve them
            summary, start = "", ""
            for line in data.decode("utf-8", "replace").replace("\r\n", "\n").split("\n"):
                if line.startswith("SUMMARY:"):
                    summary = line[len("SUMMARY:"):].strip()
                elif line.startswith("DTSTART:"):
                    start = line[len("DTSTART:"):].strip()
            H.events.setdefault(cal, []).append(
                {"uid": uid, "summary": summary, "start": start})
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
        self._json({"ok": True}, code=204)

    def do_POST(self):
        m = re.fullmatch(r"/ocs/v2\.php/apps/spreed/api/v4/chat/(\d+)",
                         self.path)
        if m:
            if not self._dav_ok():
                self._json({"error": "unauthorized"}, code=401)
                return
            qs = urllib.parse.parse_qs(self._body())
            text = qs.get("message", [""])[0]
            silent = qs.get("silent", ["0"])[0] == "1"
            H._chat_next_id += 1
            H.chat_sent.append((int(m.group(1)), text, silent))
            self._json({"ocs": {"meta": {"status": "ok"},
                                "data": {"id": H._chat_next_id,
                                         "message": text}}})
            return
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
            print("STUB bot-api  sig-ok msg=" + json.dumps(body)[:60],
                  flush=True)
            H.received.append((self.path.split("/bot/")[1].split("/")[0],
                               message))
            self._json({"ok": True}, code=201)
        else:
            self._json({"ok": True})

    def log_message(self, *a):
        pass


def make_server(port=8099, secret=None):
    H.secret = secret
    return HTTPServer(("127.0.0.1", port), H)


if __name__ == "__main__":
    make_server().serve_forever()
