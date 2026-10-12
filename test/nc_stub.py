#!/usr/bin/env python3
"""Fake Nextcloud for local testing: accepts bot API + legacy chat
messages and serves the legacy room lookup. Run standalone or from
smoke.py (make_server())."""
import json
from http.server import BaseHTTPRequestHandler, HTTPServer


class H(BaseHTTPRequestHandler):
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
        if "/ocs/v2.php/apps/sparkle/api/v1/room/" in self.path:
            self._json({"ocs": {"meta": {}, "data": {"name": "Dev Team"}}})
        else:
            self._json({"ok": True})

    def do_POST(self):
        body = self._body()
        if "/apps/sparkle/api/v1/bot/" in self.path and self.path.endswith("/message"):
            sig = (self.headers.get('X-Nextcloud-Talk-Bot-Signature') or '')[:12]
            print(f"STUB bot-api  sig={sig} msg={json.dumps(body)[:60]}", flush=True)
            self._json({"ok": True}, code=201)
        elif "/apps/sparkle/api/v1/chat/" in self.path:
            print(f"STUB legacy   msg={json.dumps(body)[:60]}", flush=True)
            self._json({"ocs": {"meta": {}, "data": {"id": 99}}})
        else:
            self._json({"ok": True})

    def log_message(self, *a):
        pass


def make_server(port=8099):
    return HTTPServer(("127.0.0.1", port), H)


if __name__ == "__main__":
    make_server().serve_forever()
