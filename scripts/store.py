"""
store.py -- the local student database, one SQLite file, standard library only.

Every table is keyed by programme, so Civil and Augmented Civil live side by side
without touching. Ingesting a semester's ERS upserts its rows: a module attempt is
unique per (programme, student, year, block, module), so re-uploading the same
export changes nothing, and each new semester adds to the history rather than
replacing it. No data leaves the machine; the file is POPIA-contained.

    db = Store("data/advisor.db")
    db.register_programme("ENG-CIVIL", "Civil Engineering", "programmes/civil.yaml")
    db.ingest(parsed, source="ENG-CV_ERS_1_Jul_2026.pdf")   # parsed = ers_ingest output

Read side (used by the SQLite datasource):
    db.programmes()                 -> [{code, name, yaml_path, students}]
    db.students("ENG-CIVIL")        -> bio rows for the picker
    db.results("ENG-CIVIL")         -> {student_number: [result rows]}
    db.latest_decisions("ENG-CIVIL")-> {student_number: {code, text, year, semester}}
"""
from __future__ import annotations
from typing import Any, Iterable
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS programmes (
    code       TEXT PRIMARY KEY,
    name       TEXT,
    yaml_path  TEXT,
    created    TEXT
);
CREATE TABLE IF NOT EXISTS students (
    programme       TEXT,
    student_number  TEXT,
    surname         TEXT,
    name            TEXT,
    plan_code       TEXT,
    year_of_study   REAL,
    total_credits   REAL,
    updated         TEXT,
    PRIMARY KEY (programme, student_number)
);
CREATE TABLE IF NOT EXISTS results (
    programme       TEXT,
    student_number  TEXT,
    calendar_year   TEXT,
    block           TEXT,
    plan_code       TEXT,
    study_period    INTEGER,
    module_code     TEXT,
    attempt         INTEGER,
    module_name     TEXT,
    credits         REAL,
    grade           REAL,
    result_code     TEXT,
    result_text     TEXT,
    PRIMARY KEY (programme, student_number, calendar_year, block, module_code, attempt)
);
CREATE TABLE IF NOT EXISTS term_decisions (
    programme       TEXT,
    student_number  TEXT,
    calendar_year   TEXT,
    semester        INTEGER,
    term_code       TEXT,
    term_text       TEXT,
    PRIMARY KEY (programme, student_number, calendar_year, semester)
);
CREATE TABLE IF NOT EXISTS ingests (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    programme   TEXT,
    source      TEXT,
    n_students  INTEGER,
    n_results   INTEGER,
    n_decisions INTEGER,
    at          TEXT
);
"""


class Store:
    def __init__(self, path: str) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        # The threading server handles each request on its own thread, so the
        # connection must not be pinned to the thread that opened it. Writes are
        # serialised by _lock; reads are safe to run concurrently.
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self.db.executescript(SCHEMA)
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    # -- write ---------------------------------------------------------------
    def register_programme(self, code: str, name: str, yaml_path: str) -> None:
        # A blank name or path on a later ingest must not erase what is already
        # on record: COALESCE keeps the stored value when the new one is empty.
        with self._lock:
            self.db.execute(
                "INSERT INTO programmes(code, name, yaml_path, created) VALUES(?,?,?,?) "
                "ON CONFLICT(code) DO UPDATE SET"
                " name=COALESCE(NULLIF(excluded.name,''), programmes.name),"
                " yaml_path=COALESCE(NULLIF(excluded.yaml_path,''), programmes.yaml_path)",
                (code, name, yaml_path, _now()))
            # A programme never named falls back to its code, but a real name once
            # given is never overwritten by a later blank ingest.
            self.db.execute("UPDATE programmes SET name=code WHERE name IS NULL OR name=''")
            self.db.commit()

    def ingest(self, parsed: dict[str, list[dict]], source: str = "") -> dict[str, int]:
        """Upsert one parsed ERS into the store. Idempotent per semester."""
        now = _now()
        prog = _programme_of(parsed)
        counts = {"n_students": len(parsed["students"]),
                  "n_results": len(parsed["results"]),
                  "n_decisions": len(parsed["decisions"])}
        with self._lock:
            self._upsert_students(parsed["students"], now)
            self._upsert_results(parsed["results"])
            self._upsert_decisions(parsed["decisions"])
            self.db.execute(
                "INSERT INTO ingests(programme, source, n_students, n_results, n_decisions, at)"
                " VALUES(?,?,?,?,?,?)",
                (prog, source, counts["n_students"], counts["n_results"], counts["n_decisions"], now))
            self.db.commit()
        return counts

    def _upsert_students(self, rows: Iterable[dict], now: str) -> None:
        self.db.executemany(
            "INSERT INTO students(programme, student_number, surname, name, plan_code,"
            " year_of_study, total_credits, updated) VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(programme, student_number) DO UPDATE SET surname=excluded.surname,"
            " name=excluded.name, plan_code=excluded.plan_code,"
            " year_of_study=excluded.year_of_study, total_credits=excluded.total_credits,"
            " updated=excluded.updated",
            [(r["programme"], str(r["student_number"]), r.get("surname"), r.get("name"),
              r.get("plan_code"), _num(r.get("year_of_study")), _num(r.get("total_credits")), now)
             for r in rows])

    def _upsert_results(self, rows: Iterable[dict]) -> None:
        # An ERS export is cumulative, so the same period+module can list more
        # than one record (an in-progress row and its later result, or a supp
        # beside the first sitting). Number them within their period so none is
        # lost. The ordering is stable across exports, so re-ingest stays idempotent.
        seen: dict[tuple, int] = {}
        params = []
        for r in rows:
            key = (str(r["student_number"]), str(r.get("calendar_year") or ""),
                   str(r.get("block") or ""), r.get("module_code"))
            seen[key] = seen.get(key, 0) + 1
            params.append((
                r["programme"], str(r["student_number"]), str(r.get("calendar_year") or ""),
                str(r.get("block") or ""), r.get("plan_code"), _int(r.get("study_period")),
                r.get("module_code"), seen[key], r.get("module_name"), _num(r.get("credits")),
                _num(r.get("grade")), r.get("result_code"), r.get("result_text")))
        self.db.executemany(
            "INSERT INTO results(programme, student_number, calendar_year, block, plan_code,"
            " study_period, module_code, attempt, module_name, credits, grade, result_code, result_text)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(programme, student_number, calendar_year, block, module_code, attempt) DO UPDATE SET"
            " module_name=excluded.module_name, credits=excluded.credits, grade=excluded.grade,"
            " result_code=excluded.result_code, result_text=excluded.result_text,"
            " plan_code=excluded.plan_code, study_period=excluded.study_period",
            params)

    def _upsert_decisions(self, rows: Iterable[dict]) -> None:
        self.db.executemany(
            "INSERT INTO term_decisions(programme, student_number, calendar_year, semester,"
            " term_code, term_text) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(programme, student_number, calendar_year, semester) DO UPDATE SET"
            " term_code=excluded.term_code, term_text=excluded.term_text",
            [(r["programme"], str(r["student_number"]), str(r.get("calendar_year") or ""),
              _int(r.get("semester")), r.get("term_code"), r.get("term_text"))
             for r in rows])

    # -- read ----------------------------------------------------------------
    def programmes(self) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT p.code, p.name, p.yaml_path,"
            " (SELECT COUNT(*) FROM students s WHERE s.programme = p.code) AS students"
            " FROM programmes p ORDER BY p.name").fetchall()
        return [dict(r) for r in rows]

    def programme(self, code: str) -> dict[str, Any] | None:
        r = self.db.execute("SELECT code, name, yaml_path FROM programmes WHERE code=?",
                            (code,)).fetchone()
        return dict(r) if r else None

    def students(self, programme: str) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT student_number, surname, name, plan_code, year_of_study, total_credits"
            " FROM students WHERE programme=? ORDER BY surname, name", (programme,)).fetchall()
        return [dict(r) for r in rows]

    def results(self, programme: str) -> dict[str, list[dict[str, Any]]]:
        rows = self.db.execute(
            "SELECT * FROM results WHERE programme=?"
            " ORDER BY student_number, calendar_year, block", (programme,)).fetchall()
        out: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            out.setdefault(r["student_number"], []).append(dict(r))
        return out

    def latest_decisions(self, programme: str) -> dict[str, dict[str, Any]]:
        """Newest term-decision row per student, by (year, semester)."""
        rows = self.db.execute(
            "SELECT student_number, calendar_year, semester, term_code, term_text"
            " FROM term_decisions WHERE programme=?", (programme,)).fetchall()
        latest: dict[str, dict[str, Any]] = {}
        for r in rows:
            sn = r["student_number"]
            key = (str(r["calendar_year"]), _int(r["semester"]) or 0)
            cur = latest.get(sn)
            if cur is None or key > cur["_key"]:
                latest[sn] = {"_key": key, "code": (r["term_code"] or "").upper(),
                              "text": r["term_text"] or "",
                              "calendar_year": r["calendar_year"], "semester": r["semester"]}
        return latest


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _num(v: Any) -> float | None:
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


def _int(v: Any) -> int | None:
    n = _num(v)
    return int(n) if n is not None else None


def _programme_of(parsed: dict[str, list[dict]]) -> str:
    for key in ("students", "results", "decisions"):
        if parsed.get(key):
            return parsed[key][0].get("programme", "")
    return ""
