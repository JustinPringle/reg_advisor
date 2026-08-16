#!/usr/bin/env python3
"""
server.py -- the local advisor, standard library only.

Serves one page and the JSON API from one origin on localhost. All student data
stays on this machine; the server never calls out.

    GET  /                     the advisor page
    GET  /api/students         picker list
    GET  /api/students/<sn>    one student: profile, standing, advice + grades
    POST /api/check            {sn, codes:[...]} -> CLEARED / REVIEW per module
    GET  /api/triage           the batched queue: auto-cleared + academic piles
    GET  /api/decisions        decisions recorded so far
    POST /api/decide           {sn, code, decision, note} -> record a decision

Run from scripts/:  python server.py    then open http://127.0.0.1:8000
"""
from __future__ import annotations
from typing import Any
import csv
import json
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from datasource import CsvSource
from triage import triage_queue, demo_apps, read_apps, CONCESSION_AUTOCLEAR

ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "web" / "advisor.html"
DECISIONS = ROOT / "data" / "decisions.csv"
ADMIN_FILE = ROOT / "data" / "admin_worklist.csv"
APPS = ROOT / "data" / "applications.csv"
HOST, PORT = "127.0.0.1", 8000

SOURCE = CsvSource()   # swap for ItsSource() when the database is wired
_QUEUE: dict[str, Any] | None = None


def load_decisions() -> dict[tuple[str, str], dict[str, str]]:
    d: dict[tuple[str, str], dict[str, str]] = {}
    if DECISIONS.exists():
        with open(DECISIONS, newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                d[(r["student_number"], r["module_code"])] = {
                    "decision": r["decision"], "date": r.get("date", ""),
                    "note": r.get("note", "")}
    return d


DEC = load_decisions()


def record_decision(sn: str, code: str, decision: str, note: str) -> None:
    new = not DECISIONS.exists()
    with open(DECISIONS, "a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(["student_number", "module_code", "decision", "date", "note"])
        w.writerow([sn, code, decision, date.today().isoformat(), note])
    DEC[(sn, code)] = {"decision": decision, "date": date.today().isoformat(), "note": note}


def build_queue() -> dict[str, Any]:
    global _QUEUE
    if _QUEUE is None:
        rule = (SOURCE.cur.get("rules") or {}).get("autoclear") or CONCESSION_AUTOCLEAR
        if APPS.exists():
            apps = read_apps(APPS)
            names = {sn: f"{b.get('surname','')}, {b.get('name','')}".strip(", ")
                     for sn, b in SOURCE.bio.items()}
        else:
            apps, names = demo_apps(SOURCE)
        _QUEUE = triage_queue(SOURCE.cur, SOURCE.results, apps, names, rule)
    return _QUEUE


def annotate(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for r in rows:
        d = DEC.get((r["student_number"], r["module_code"]))
        out.append({**r, "decision": d["decision"] if d else None})
    return out


def admin_rows() -> list[dict[str, Any]]:
    """The admin handoff: everything auto-cleared (registrations and auto
    concessions) plus every concession the academic approved in the browser."""
    q = build_queue()
    today = date.today().isoformat()
    rows = []
    for r in q["admin"]:
        rows.append({"student_number": r["student_number"], "name": r["name"],
                     "module_code": r["module_code"], "module_name": r["module_name"],
                     "credits": r["credits"], "lane": r["lane"],
                     "basis": r["reason"], "note": "", "decided": today})
    acad = {(r["student_number"], r["module_code"]): r for r in q["academic"]}
    for (sn, code), d in DEC.items():
        if d["decision"] != "approved":
            continue
        ar = acad.get((sn, code))
        if not ar:
            continue
        rows.append({"student_number": sn, "name": ar["name"], "module_code": code,
                     "module_name": ar["module_name"], "credits": ar["credits"],
                     "lane": "concession-approved", "basis": "approved by academic",
                     "note": d.get("note", ""), "decided": d.get("date", today)})
    order = {"concession-approved": 0, "concession-auto": 1, "register": 2}
    rows.sort(key=lambda r: (order.get(r["lane"], 3), r["name"]))
    return rows


def write_admin(rows: list[dict[str, Any]]) -> None:
    fields = ["student_number", "name", "module_code", "module_name", "credits",
              "lane", "basis", "note", "decided"]
    with open(ADMIN_FILE, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


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
        elif path == "/api/triage":
            q = build_queue()
            auto = [r for r in q["admin"] if r["lane"] == "concession-auto"]
            self._json({"academic": annotate(q["academic"]),
                        "auto": annotate(auto), "summary": q["summary"]})
        elif path == "/api/decisions":
            self._json([{"sn": k[0], "code": k[1], **v} for k, v in DEC.items()])
        elif path == "/api/admin":
            rows = admin_rows()
            self._json({"rows": rows, "count": len(rows)})
        elif path.startswith("/api/students/"):
            sn = path.rsplit("/", 1)[-1]
            data = SOURCE.get_student(sn)
            self._json(data) if data else self._json({"error": "not found"}, 404)
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:
        path = self.path.split("?")[0]
        length = int(self.headers.get("Content-Length") or 0)
        try:
            req = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._json({"error": "bad json"}, 400)
            return
        if path == "/api/check":
            data = SOURCE.check(str(req.get("sn", "")), list(req.get("codes", [])))
            self._json(data) if data else self._json({"error": "not found"}, 404)
        elif path == "/api/decide":
            sn, code = str(req.get("sn", "")), str(req.get("code", ""))
            decision = str(req.get("decision", ""))
            if not (sn and code and decision):
                self._json({"error": "sn, code, decision required"}, 400)
                return
            record_decision(sn, code, decision, str(req.get("note", "")))
            self._json({"ok": True, "decided": len(DEC)})
        elif path == "/api/admin/export":
            rows = admin_rows()
            write_admin(rows)
            self._json({"ok": True, "path": str(ADMIN_FILE.relative_to(ROOT)),
                        "count": len(rows)})
        else:
            self._json({"error": "not found"}, 404)

    def log_message(self, *args) -> None:
        pass


def main() -> None:
    print(f"Civil registration advisor -> http://{HOST}:{PORT}  (Ctrl-C to stop)")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
