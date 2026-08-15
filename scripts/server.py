#!/usr/bin/env python3
"""
server.py -- the local advisor, standard library only.

Serves one page and three JSON routes from one origin on localhost, so there is
nothing to install and no cross-origin setup. All student data stays on this
machine; the server never calls out.

    GET  /                     the advisor page
    GET  /api/students         picker list
    GET  /api/students/<sn>    one student: profile, standing, advice buckets
    POST /api/check            {sn, codes:[...]} -> CLEARED / REVIEW per module

Run from the scripts/ directory:
    python server.py
then open http://127.0.0.1:8000
"""
from __future__ import annotations
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from datasource import CsvSource

ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "web" / "advisor.html"
HOST, PORT = "127.0.0.1", 8000

SOURCE = CsvSource()   # swap for ItsSource() when the database is wired


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj).encode("utf-8"), "application/json")

    def do_GET(self) -> None:
        path = self.path.split("?")[0]
        if path == "/":
            self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
        elif path == "/api/students":
            self._json(SOURCE.list_students())
        elif path.startswith("/api/students/"):
            sn = path.rsplit("/", 1)[-1]
            data = SOURCE.get_student(sn)
            self._json(data) if data else self._json({"error": "not found"}, 404)
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:
        if self.path.split("?")[0] != "/api/check":
            self._json({"error": "not found"}, 404)
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            req = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._json({"error": "bad json"}, 400)
            return
        data = SOURCE.check(str(req.get("sn", "")), list(req.get("codes", [])))
        self._json(data) if data else self._json({"error": "not found"}, 404)

    def log_message(self, *args) -> None:   # quiet console
        pass


def main() -> None:
    print(f"Civil registration advisor -> http://{HOST}:{PORT}  (Ctrl-C to stop)")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
