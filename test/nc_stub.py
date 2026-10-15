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
    files = {"temp/notes.txt": b"hello nextcloud"}
    calendars = {"family": "Family", "work": "Work", "privat": "Privat",
                 "Privat (2)": "Privat2"}
    events = {"family": [{"uid": "ev1", "summary": "Old thing",
                          "start": "2000-01-01T08:00:00+01:00"}],
               "work": [], "privat": [], "Privat (2)": []}
    put_calls = []  # (method, path) for the /nc proxy tests

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
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
            cal = self.path.rstrip("/").rsplit("/", 1)[-1]
            # Nextcloud's Depth-1 answer contains the calendar-home
            # collection itself first (resourcetype calendar-home-set, NOT
            # a calendar — accessing it 404s), then one response per
            # real calendar (resourcetype c:calendar). Note: the prefix
            # here is 'cal', deliberately different from the 'c' the
            # client requests — servers pick their own prefixes.
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
                       f"<d:resourcetype><d:collection/><cal:calendar/>"
                       f"</d:resourcetype></d:prop>"
                       f"<d:status>HTTP/1.1 200 OK</d:status>"
                       f"</d:propstat></d:response>"
                       for c, n in H.calendars.items())
                   + "</d:multistatus>")
        else:  # webdav dir listing
            root = self.path[len("/remote.php/webdav/"):] .rstrip("/")
            resp = []
            for p in sorted(H.files):
                if root and not p.startswith(root + "/"):
                    continue
                name = p.rsplit("/", 1)[-1]
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
