#!/usr/bin/env python3
"""
server.py -- the local advisor, programme-aware, standard library only.

One page and one JSON API on localhost. Every student endpoint is scoped to a
programme, so a coordinator picks a programme, sees only its students, and gets
advice against its rules. Uploading a semester's ERS ingests it into the local
database and refreshes the picker. No student data leaves the machine.

    GET  /                        the advisor page
    GET  /api/programmes          programmes on offer (+ the current one)
    GET  /api/students?programme= picker list for a programme
    GET  /api/students/<sn>?programme=   one student: profile, standing, advice
    POST /api/check               {programme, sn, codes} -> CLEARED / REVIEW
    POST /api/ingest              multipart: file, programme, name, yaml -> ingest
    GET  /api/health?programme=   programme-health analytics (intake, throughput, modules)
    GET  /api/triage?programme=   the batched concession queue
    GET  /api/decisions           decisions recorded so far
    POST /api/decide              {sn, code, decision, note, by} -> record a decision
    GET  /api/admin?programme=     the admin hand-off worklist
    POST /api/admin/export?programme=   write that worklist to CSV

Run from scripts/:  python server.py    then open http://127.0.0.1:8000
"""
from __future__ import annotations
from typing import Any
from urllib.parse import urlparse, parse_qs
import csv
import json
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from store import Store
from datasource_sqlite import SqliteSource, list_programmes
from ingest_ers import ingest_path
from ers_ingest import parse_file
from multipart import parse_multipart
from triage import triage_queue, demo_apps, CONCESSION_AUTOCLEAR
from programme_catalogue import catalogue, resolve
import checks_service as CHK

ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "web" / "advisor.html"
DB = ROOT / "data" / "student_data.db"
UPLOADS = ROOT / "data" / "uploads"
DECISIONS = ROOT / "data" / "decisions.csv"
ADMIN_DIR = ROOT / "data"
PROGRAMMES_DIR = ROOT / "programmes"     # the folder the picker reads
HOST, PORT = "127.0.0.1", 8000

STORE = Store(str(DB))
_SOURCES: dict[str, SqliteSource] = {}
_QUEUES: dict[tuple, dict[str, Any]] = {}     # keyed by (programme, year, sem)


def source(programme: str) -> SqliteSource | None:
    if not programme:
        return None
    if programme not in _SOURCES:
        if STORE.programme(programme) is None:
            return None
        _SOURCES[programme] = SqliteSource(STORE, programme)
    return _SOURCES[programme]


def refresh(programme: str) -> None:
    _SOURCES.pop(programme, None)
    for k in [k for k in _QUEUES if k[0] == programme]:
        _QUEUES.pop(k, None)


# --- decisions --------------------------------------------------------------
# Append-only log. A row is (student, module, decision, date, note, by). Two
# live decisions -- "approved" and "declined" -- plus a "removed" tombstone
# that undoes whichever decision preceded it. The file replays in order, so the
# last row for a pair wins and a tombstone clears it. A legacy five-column file
# is migrated once, at boot, to gain the "by" column; old rows still load.
DEC_FIELDS = ["student_number", "module_code", "decision", "date", "note", "by"]


def _migrate_decisions() -> None:
    if not DECISIONS.exists():
        return
    with open(DECISIONS, newline="", encoding="utf-8") as fh:
        rows = list(csv.reader(fh))
    if rows and "by" not in rows[0]:
        with open(DECISIONS, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(DEC_FIELDS)
            for r in rows[1:]:
                w.writerow((r + [""] * len(DEC_FIELDS))[:len(DEC_FIELDS)])


def load_decisions() -> dict[tuple[str, str], dict[str, str]]:
    d: dict[tuple[str, str], dict[str, str]] = {}
    if DECISIONS.exists():
        with open(DECISIONS, newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                k = (r["student_number"], r["module_code"])
                if r["decision"] == "removed":
                    d.pop(k, None)
                else:
                    d[k] = {"decision": r["decision"], "date": r.get("date", ""),
                            "note": r.get("note", ""), "by": r.get("by", "")}
    return d


_migrate_decisions()
DEC = load_decisions()


def record_decision(sn: str, code: str, decision: str, note: str, by: str = "") -> None:
    new = not DECISIONS.exists()
    with open(DECISIONS, "a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(DEC_FIELDS)
        w.writerow([sn, code, decision, date.today().isoformat(), note, by])
    if decision == "removed":
        DEC.pop((sn, code), None)
    else:
        DEC[(sn, code)] = {"decision": decision, "date": date.today().isoformat(),
                           "note": note, "by": by}


# --- concession queue (per programme) ---------------------------------------
def build_queue(programme: str, year: str = "", sem: str = "") -> dict[str, Any] | None:
    src = source(programme)
    if src is None or not src.advice_ready:
        return None
    key = (programme, year, sem)
    if key not in _QUEUES:
        rule = (src.cur.get("rules") or {}).get("autoclear") or CONCESSION_AUTOCLEAR
        only = src.cohort(year, int(sem)) if (year and sem) else src.current_students()
        apps, names = demo_apps(src, only=only)
        _QUEUES[key] = triage_queue(src.cur, src.results, apps, names, rule)
    return _QUEUES[key]


def annotate(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{**r, "decision": (DEC.get((r["student_number"], r["module_code"])) or {}).get("decision")}
            for r in rows]


def admin_rows(programme: str, year: str = "", sem: str = "") -> list[dict[str, Any]]:
    q = build_queue(programme, year, sem)
    if q is None:
        return []
    today = date.today().isoformat()
    rows = []
    for r in q["admin"]:
        rows.append({"student_number": r["student_number"], "name": r["name"],
                     "module_code": r["module_code"], "module_name": r["module_name"],
                     "credits": r["credits"], "lane": r["lane"],
                     "basis": r["reason"], "note": "", "by": "", "decided": today})
    acad = {(r["student_number"], r["module_code"]): r for r in q["academic"]}
    for (sn, code), d in DEC.items():
        if d["decision"] != "approved":       # declined and removed never reach admin
            continue
        ar = acad.get((sn, code))
        if not ar:
            continue
        rows.append({"student_number": sn, "name": ar["name"], "module_code": code,
                     "module_name": ar["module_name"], "credits": ar["credits"],
                     "lane": "concession-approved", "basis": "approved by academic",
                     "note": d.get("note", ""), "by": d.get("by", ""),
                     "decided": d.get("date", today)})
    order = {"concession-approved": 0, "concession-auto": 1, "register": 2}
    rows.sort(key=lambda r: (order.get(r["lane"], 3), r["name"]))
    return rows


def write_admin(programme: str, rows: list[dict[str, Any]]) -> Path:
    fields = ["student_number", "name", "module_code", "module_name", "credits",
              "lane", "basis", "note", "by", "decided"]
    path = ADMIN_DIR / f"admin_worklist_{programme}.csv"
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    return path


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj).encode("utf-8"), "application/json")

    def _q(self, name: str, default: str = "") -> str:
        return (parse_qs(urlparse(self.path).query).get(name) or [default])[0]

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/":
            self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
        elif path == "/api/programmes":
            progs = list_programmes(STORE)
            self._json({"programmes": progs, "current": progs[0]["code"] if progs else None})
        elif path == "/api/programme_files":
            # The programmes on offer in the folder -- what the upload picker shows.
            self._json({"programmes": catalogue(PROGRAMMES_DIR)})
        elif path == "/api/documents":
            self._json({"documents": STORE.documents(self._q("programme"))})
        elif path == "/api/completion":
            self._json(CHK.completion(STORE, self._q("programme"),
                                      self._q("year"), self._q("sem")))
        elif path == "/api/health":
            self._json(CHK.health(STORE, self._q("programme")))
        elif path == "/api/erscheck":
            src = source(self._q("programme"))
            year, sem = self._q("year"), self._q("sem")
            only = src.cohort(year, int(sem)) if (src and year and sem) else None
            self._json(CHK.ers_check(STORE, self._q("programme"),
                                     self._q("source", "final"), only=only))
        elif path == "/api/erscheck/student":                       # <-- add
            self._json(CHK.student_detail(STORE, self._q("programme"),
                                          self._q("sn"), self._q("source", "final")))
        elif path == "/api/cycles":
            src = source(self._q("programme"))
            if src is None:
                self._json({"cycles": [], "current": None})
            else:
                cur = ({"year": src.current_cycle[0], "semester": src.current_cycle[1]}
                       if src.current_cycle else None)
                self._json({"cycles": src.cohorts(), "current": cur})
        elif path == "/api/students":
            src = source(self._q("programme"))
            sem = self._q("sem")
            self._json(src.list_students(self._q("year") or None,
                                         int(sem) if sem else None)
                       if src else {"error": "unknown programme"},
                       200 if src else 404)
        elif path == "/api/triage":
            q = build_queue(self._q("programme"), self._q("year"), self._q("sem"))
            if q is None:
                self._json({"academic": [], "auto": [], "summary": {}, "ready": False})
                return
            auto = [r for r in q["admin"] if r["lane"] == "concession-auto"]
            self._json({"academic": annotate(q["academic"]), "auto": annotate(auto),
                        "summary": q["summary"], "ready": True})
        elif path == "/api/decisions":
            self._json([{"sn": k[0], "code": k[1], **v} for k, v in DEC.items()])
        elif path == "/api/admin":
            rows = admin_rows(self._q("programme"), self._q("year"), self._q("sem"))
            self._json({"rows": rows, "count": len(rows)})
        elif path.startswith("/api/students/"):
            sn = path.rsplit("/", 1)[-1]
            src = source(self._q("programme"))
            data = src.get_student(sn) if src else None
            self._json(data) if data else self._json({"error": "not found"}, 404)
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""

        if path == "/api/ingest":
            self._ingest(raw)
            return

        try:
            req = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            self._json({"error": "bad json"}, 400)
            return

        if path == "/api/check":
            src = source(str(req.get("programme", "")))
            data = src.check(str(req.get("sn", "")), list(req.get("codes", []))) if src else None
            self._json(data) if data else self._json({"error": "not found"}, 404)
        elif path == "/api/decide":
            sn, code = str(req.get("sn", "")), str(req.get("code", ""))
            decision = str(req.get("decision", ""))
            if not (sn and code and decision):
                self._json({"error": "sn, code, decision required"}, 400)
                return
            record_decision(sn, code, decision, str(req.get("note", "")),
                            str(req.get("by", "")))
            self._json({"ok": True, "decided": len(DEC)})
        elif path == "/api/admin/export":
            programme = str(req.get("programme", "")) or self._q("programme")
            year, sem = str(req.get("year", "")), str(req.get("sem", ""))
            rows = admin_rows(programme, year, sem)
            out = write_admin(programme, rows)
            self._json({"ok": True, "path": str(out.relative_to(ROOT)), "count": len(rows)})
        elif path == "/api/completion/export":
            programme = str(req.get("programme", "")) or self._q("programme")
            year, sem = str(req.get("year", "")), str(req.get("sem", ""))
            comp = CHK.completion(STORE, programme, year, sem)
            from completion import export_completion
            dg = export_completion(comp["DC"], str(ADMIN_DIR / f"degree_complete_{programme}.csv"))
            dgor = export_completion(comp["DGOR"], str(ADMIN_DIR / f"vac_only_{programme}.csv"), dgor=True)
            self._json({"ok": True, "DG": {"path": str(dg.relative_to(ROOT)), "count": len(comp["DC"])},
                        "DGOR": {"path": str(dgor.relative_to(ROOT)), "count": len(comp["DGOR"])}})
        elif path == "/api/erscheck/export":
            programme = str(req.get("programme", "")) or self._q("programme")
            ers_source = str(req.get("source", "")) or self._q("source", "final")
            year, sem = str(req.get("year", "")), str(req.get("sem", ""))
            src = source(programme)
            only = src.cohort(year, int(sem)) if (src and year and sem) else None
            rep = CHK.ers_check(STORE, programme, ers_source, only=only)
            from ers_check import export_mismatches
            out = export_mismatches(rep, str(ADMIN_DIR / f"ers_mismatches_{programme}_{ers_source}.csv"))
            self._json({"ok": True, "source": ers_source, "path": str(out.relative_to(ROOT)),
                        "count": rep.get("summary", {}).get("mismatch", 0)})
        else:
            self._json({"error": "not found"}, 404)

    def _ingest(self, raw: bytes) -> None:
        fields, files = parse_multipart(self.headers.get("Content-Type", ""), raw)
        programme = fields.get("programme", "").strip()
        if not programme or "file" not in files:
            self._json({"error": "programme and file required"}, 400)
            return
        # The picker sends a programme code; the catalogue supplies its name and
        # rule file, so nothing but the code and the PDF is typed at upload time.
        # An explicit name/yaml still overrides, for a programme not yet foldered.
        entry = resolve(programme, PROGRAMMES_DIR) or {}
        name = fields.get("name", "").strip() or entry.get("name", "")
        yaml = fields.get("yaml", "").strip() or entry.get("yaml_path", "")
        kind = (fields.get("kind", "final").strip().lower() or "final")
        if kind not in ("initial", "final"):
            kind = "final"

        filename, data = files["file"]
        UPLOADS.mkdir(parents=True, exist_ok=True)
        dest = UPLOADS / f"{programme}__{kind}__{filename}"
        dest.write_bytes(data)
        STORE.add_document(programme, kind, filename, str(dest))

        try:
            if kind == "final":
                # The captured record: upsert into the main tables.
                counts = ingest_path(STORE, str(dest), programme, name, yaml)
            else:
                # An initial run is kept only to check -- registered as a
                # programme so its rules bind, parsed for a row count, but NOT
                # written to the results/decisions tables. Only the final is stored.
                STORE.register_programme(programme, name, yaml)
                suffix = Path(dest).suffix.lower()
                parsed = ({"students": [], "results": [], "decisions": []}
                          if suffix == ".csv" else parse_file(str(dest), programme))
                counts = {"n_students": len(parsed["students"]),
                          "n_results": len(parsed["results"]),
                          "n_decisions": len(parsed["decisions"])}
        except Exception as exc:                       # never crash the server on a bad upload
            self._json({"error": f"ingest failed: {exc}"}, 400)
            return
        refresh(programme)
        self._json({"ok": True, "programme": programme, "kind": kind,
                    "stored": kind == "final", **counts})

    def log_message(self, *args) -> None:
        pass


def main() -> None:
    progs = list_programmes(STORE)
    print(f"Registration advisor -> http://{HOST}:{PORT}  (Ctrl-C to stop)")
    print("  " + (", ".join(p["code"] for p in progs) or "no programmes yet -- upload an ERS"))
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
