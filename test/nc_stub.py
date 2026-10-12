"""Tiny Nextcloud stub for local testing: serves OCS room lookup and
accepts chat messages. Run: python test/nc_stub.py  (listens on 8099)"""
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

    def do_GET(self):
        if "/ocs/v2.php/apps/sparkle/api/v1/room/" in self.path:
            self._json({"ocs": {"meta": {}, "data": {"name": "Dev Team"}}})
        elif self.path.startswith("/ocs/"):
            self._json({"ocs": {"meta": {}, "data": {}}})
        else:
            self._json({"ok": True})

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(n).decode()
        print("STUB chat message:", body, flush=True)
        self._json({"ocs": {"meta": {}, "data": {"id": 99}}})

    def log_message(self, *a):
        pass


def make_server(port=8099):
    return HTTPServer(("127.0.0.1", port), H)


if __name__ == "__main__":
    make_server().serve_forever()
