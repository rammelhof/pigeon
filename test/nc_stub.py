#!/usr/bin/env python3
"""Fake Nextcloud for local testing: accepts spreed bot API messages
and verifies outgoing bot API signatures; serves the OCS dav direct API
(file id -> short-lived download URL) like a real NC34 instance.
Run standalone or from smoke.py."""
import base64
import hashlib
import hmac
import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer


class H(BaseHTTPRequestHandler):
    # every bot API message received (room token, message) — for tests
    received = []
    # Basic auth for the dav direct API: {user: password}
    share_auth = {}
    # issued direct download tokens -> body bytes
    direct_tokens = {}
    # every dav direct request seen: (authed_user or None, fileId)
    direct_calls = []
    file_bytes = b"FAKE-IMAGE-BYTES-1234"

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
        if self.path.startswith("/direct/"):
            token = self.path.rsplit("/", 1)[-1]
            body = H.direct_tokens.get(token)
            if body is None:
                self._json({"error": "not found"}, code=404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self._json({"ok": True})

    def do_POST(self):
        body = self._body()
        if "/ocs/v2.php/apps/dav/api/v1/direct" in self.path:
            auth = self._basic_auth()
            try:
                file_id = json.loads(body).get("fileId")
            except json.JSONDecodeError:
                file_id = None
            ok = auth is not None and H.share_auth.get(auth[0]) == auth[1]
            H.direct_calls.append((auth[0] if auth else None, file_id))
            if not ok:
                self._json({"ocs": {"meta": {"status": "failure",
                                             "statuscode": 401,
                                             "message": "Unauthorized"}}},
                           code=401)
                return
            token = os.urandom(16).hex()
            H.direct_tokens[token] = H.file_bytes
            self._json({"ocs": {"meta": {"status": "ok", "statuscode": 200,
                                         "message": "OK"},
                                "data": {"url":
                                         "http://127.0.0.1:8099/direct/"
                                         + token}}})
            return
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
