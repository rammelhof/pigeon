#!/usr/bin/env python3
"""Fake Nextcloud for local testing: accepts spreed bot API messages
and verifies outgoing bot API signatures. Run standalone or from smoke.py."""
import hashlib
import hmac
import json
from http.server import BaseHTTPRequestHandler, HTTPServer


class H(BaseHTTPRequestHandler):
    # every bot API message received (room token, message) — for tests
    received = []
    # every share download requested (path) — for tests
    downloads = []
    # share tokens that should 404 (broken links) — for tests
    broken_shares = {"missing-share"}
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

    def do_GET(self):
        if self.path.startswith("/s/"):
            H.downloads.append(self.path)
            token = self.path.split("/")[2].split("?")[0]
            if token in H.broken_shares:
                self._json({"error": "not found"}, code=404)
                return
            body = H.file_bytes
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self._json({"ok": True})

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
