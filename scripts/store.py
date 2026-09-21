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
    db.colour_codes("ENG-CIVIL")    -> {student_number: {"2026:1": {colour, text}}}
"""
from __future__ import annotations
from typing import Any, Iterable
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from standing_codes import SEM_OF_BLOCK

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
    as_of           TEXT DEFAULT '',   -- see _as_of
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
    as_of           TEXT DEFAULT '',
    PRIMARY KEY (programme, student_number, calendar_year, block, module_code, attempt)
);
CREATE TABLE IF NOT EXISTS term_decisions (
    programme       TEXT,
    student_number  TEXT,
    calendar_year   TEXT,
    semester        INTEGER,
    term_code       TEXT,
    term_text       TEXT,
    as_of           TEXT DEFAULT '',
    PRIMARY KEY (programme, student_number, calendar_year, semester)
);
-- The registrar's colour for one period: green, orange or red. Kept apart from
-- term_decisions on purpose. A decision code is written only when something
-- needs saying, so a period in good standing has none; the colour block states
-- a standing for every period. Two records, two tables, and each checks the
-- other -- 2023:2 orange here should sit beside 2023:2 RISK there.
CREATE TABLE IF NOT EXISTS colour_codes (
    programme       TEXT,
    student_number  TEXT,
    calendar_year   TEXT,
    semester        INTEGER,
    colour          TEXT,          -- 'green' | 'orange' | 'red'
    colour_text     TEXT,          -- the registrar's wording, e.g. 'At Risk'
    as_of           TEXT DEFAULT '',
    PRIMARY KEY (programme, student_number, calendar_year, semester)
);
CREATE TABLE IF NOT EXISTS ingests (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    programme   TEXT,
    source      TEXT,
    n_students  INTEGER,
    n_results   INTEGER,
    n_decisions INTEGER,
    as_of       TEXT DEFAULT '',
    at          TEXT
);
CREATE TABLE IF NOT EXISTS documents (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    programme   TEXT,
    kind        TEXT,          -- 'initial' (pre-check ERS run) or 'final' (captured)
    filename    TEXT,
    stored_path TEXT,
    is_current  INTEGER,       -- 1 = the current document of its kind for the programme
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
        for table in ("students", "results", "term_decisions", "colour_codes", "ingests"):
            cols = {r[1] for r in self.db.execute(f"PRAGMA table_info({table})")}
            if "as_of" not in cols:      # a store made before as_of existed
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN as_of TEXT DEFAULT ''")
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

    def remove_programme(self, code: str, keep_registration: bool = False) -> dict[str, Any]:
        """Delete one programme's data from every table, leaving others untouched.

        Clears students, results, term decisions, the ingest log, and the document
        records for the programme, then drops its registration so it leaves the
        picker. Pass keep_registration=True to clear the data but keep the code,
        name, and yaml on file -- handy before a clean re-ingest. Returns the row
        count removed per table and the document file paths, which stay on disk for
        the caller to unlink or keep.
        """
        data_tables = ("students", "results", "term_decisions", "colour_codes",
                       "ingests", "documents")
        with self._lock:
            files = [r["stored_path"] for r in self.db.execute(
                "SELECT stored_path FROM documents WHERE programme=?", (code,)).fetchall()]
            removed: dict[str, int] = {}
            for table in data_tables:
                cur = self.db.execute(f"DELETE FROM {table} WHERE programme=?", (code,))
                removed[table] = cur.rowcount
            if not keep_registration:
                cur = self.db.execute("DELETE FROM programmes WHERE code=?", (code,))
                removed["programmes"] = cur.rowcount
            self.db.commit()
        return {"removed": removed, "document_files": [f for f in files if f]}

    def ingest(self, parsed: dict[str, list[dict]], source: str = "") -> dict[str, int]:
        """Upsert one parsed ERS into the store. Idempotent per semester."""
        now = _now()
        prog = _programme_of(parsed)
        # An older parse has no colours key; treat it as none rather than fail.
        colours = parsed.get("colours") or []
        counts = {"n_students": len(parsed["students"]),
                  "n_results": len(parsed["results"]),
                  "n_decisions": len(parsed["decisions"]),
                  "n_colours": len(colours)}
        as_of = _as_of(parsed["results"])
        with self._lock:
            self._upsert_students(parsed["students"], now, as_of)
            self._upsert_results(parsed["results"], as_of)
            self._upsert_decisions(parsed["decisions"], as_of)
            self._upsert_colours(colours, as_of)
            self.db.execute(
                "INSERT INTO ingests(programme, source, n_students, n_results, n_decisions, as_of, at)"
                " VALUES(?,?,?,?,?,?,?)",
                (prog, source, counts["n_students"], counts["n_results"], counts["n_decisions"],
                 as_of, now))
            self.db.commit()
        return counts

    # Students, decisions and colours are snapshots: each export restates them,
    # and an older export restates them as they were then (a pre-appeal FPRR, a
    # stale year of study). A row is replaced only by an export at least as
    # recent as the one that wrote it, so ingest order does not matter.
    def _upsert_students(self, rows: Iterable[dict], now: str, as_of: str) -> None:
        self.db.executemany(
            "INSERT INTO students(programme, student_number, surname, name, plan_code,"
            " year_of_study, total_credits, updated, as_of) VALUES(?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(programme, student_number) DO UPDATE SET surname=excluded.surname,"
            " name=excluded.name, plan_code=excluded.plan_code,"
            " year_of_study=excluded.year_of_study, total_credits=excluded.total_credits,"
            " updated=excluded.updated, as_of=excluded.as_of"
            " WHERE excluded.as_of >= students.as_of",
            [(r["programme"], str(r["student_number"]), r.get("surname"), r.get("name"),
              r.get("plan_code"), _num(r.get("year_of_study")), _num(r.get("total_credits")), now,
              as_of)
             for r in rows])

    def _upsert_results(self, rows: Iterable[dict], as_of: str) -> None:
        # An ERS export is cumulative, so the same period+module can list more
        # than one record (an in-progress row and its later result, or a supp
        # beside the first sitting). Number them within their period so none is
        # lost. The ordering is stable across exports, so re-ingest stays idempotent.
        #
        # A blank row (no result code, no mark) means "registered, nothing
        # posted as of this export". It never overwrites a posted result. A
        # posted result is replaced only by a posted one from an export at least
        # as recent: exports can disagree (a remark, a condoned pass), and the
        # newer one is right. Ingest order no longer matters for results.
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
                _num(r.get("grade")), r.get("result_code"), r.get("result_text"), as_of))
        self.db.executemany(
            "INSERT INTO results(programme, student_number, calendar_year, block, plan_code,"
            " study_period, module_code, attempt, module_name, credits, grade, result_code, result_text,"
            " as_of) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(programme, student_number, calendar_year, block, module_code, attempt) DO UPDATE SET"
            " module_name=excluded.module_name, credits=excluded.credits, grade=excluded.grade,"
            " result_code=excluded.result_code, result_text=excluded.result_text,"
            " plan_code=excluded.plan_code, study_period=excluded.study_period, as_of=excluded.as_of"
            " WHERE (NULLIF(results.result_code, '') IS NULL AND results.grade IS NULL)"
            " OR ((NULLIF(excluded.result_code, '') IS NOT NULL OR excluded.grade IS NOT NULL)"
            "     AND excluded.as_of >= results.as_of)",
            params)

    def _upsert_decisions(self, rows: Iterable[dict], as_of: str) -> None:
        self.db.executemany(
            "INSERT INTO term_decisions(programme, student_number, calendar_year, semester,"
            " term_code, term_text, as_of) VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(programme, student_number, calendar_year, semester) DO UPDATE SET"
            " term_code=excluded.term_code, term_text=excluded.term_text, as_of=excluded.as_of"
            " WHERE excluded.as_of >= term_decisions.as_of",
            [(r["programme"], str(r["student_number"]), str(r.get("calendar_year") or ""),
              _int(r.get("semester")), r.get("term_code"), r.get("term_text"), as_of)
             for r in rows])

    def _upsert_colours(self, rows: Iterable[dict], as_of: str) -> None:
        self.db.executemany(
            "INSERT INTO colour_codes(programme, student_number, calendar_year, semester,"
            " colour, colour_text, as_of) VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(programme, student_number, calendar_year, semester) DO UPDATE SET"
            " colour=excluded.colour, colour_text=excluded.colour_text, as_of=excluded.as_of"
            " WHERE excluded.as_of >= colour_codes.as_of",
            [(r["programme"], str(r["student_number"]), str(r.get("calendar_year") or ""),
              _int(r.get("semester")), (r.get("colour") or "").lower(), r.get("colour_text"),
              as_of)
             for r in rows])

    # -- read ----------------------------------------------------------------
    def years(self, programme: str) -> list[str]:
        """Distinct calendar years present in a programme's results, newest first."""
        rows = self.db.execute(
            "SELECT DISTINCT calendar_year FROM results WHERE programme=?"
            " AND calendar_year<>'' ORDER BY calendar_year DESC", (programme,)).fetchall()
        return [r["calendar_year"] for r in rows]
    
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

    def decisions(self, programme: str) -> list[dict[str, Any]]:
        """Every term-decision row for a programme (all periods, all students)."""
        rows = self.db.execute(
            "SELECT student_number, calendar_year, semester, term_code, term_text"
            " FROM term_decisions WHERE programme=?"
            " ORDER BY student_number, calendar_year, semester", (programme,)).fetchall()
        return [dict(r) for r in rows]

    def colour_rows(self, programme: str) -> list[dict[str, Any]]:
        """Every colour row for a programme, in the parser's shape. The list
        form of colour_codes(), so the captured data can be handed to the
        checker exactly as a fresh parse would be."""
        rows = self.db.execute(
            "SELECT student_number, calendar_year, semester, colour, colour_text"
            " FROM colour_codes WHERE programme=?"
            " ORDER BY student_number, calendar_year, semester", (programme,)).fetchall()
        return [dict(r, programme=programme) for r in rows]

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

    def colour_codes(self, programme: str) -> dict[str, dict[str, dict[str, Any]]]:
        """Every colour on file, per student, keyed "year:semester".

            {"222009802": {"2026:1": {"colour": "green", "text": "Good Academic
             Standing"}, ...}}

        This is the registrar saying a period was fine. Read it before treating a
        student with no decision code as unknown.
        """
        rows = self.db.execute(
            "SELECT student_number, calendar_year, semester, colour, colour_text"
            " FROM colour_codes WHERE programme=?", (programme,)).fetchall()
        out: dict[str, dict[str, dict[str, Any]]] = {}
        for r in rows:
            period = f"{r['calendar_year']}:{_int(r['semester']) or 0}"
            out.setdefault(r["student_number"], {})[period] = {
                "colour": (r["colour"] or "").lower(), "text": r["colour_text"] or ""}
        return out

    # -- documents (initial vs final ERS PDFs) -------------------------------
    def add_document(self, programme: str, kind: str, filename: str,
                     stored_path: str) -> int:
        """Record an uploaded ERS PDF and make it the current one of its kind.

        kind is 'initial' (the raw ERS run, kept only to check) or 'final' (the
        captured record). Only one document of each kind is current per
        programme; older ones stay on file but lose the current flag, so the
        history is auditable while `current_document` always returns the latest.
        """
        kind = (kind or "final").strip().lower()
        with self._lock:
            self.db.execute(
                "UPDATE documents SET is_current=0 WHERE programme=? AND kind=?",
                (programme, kind))
            cur = self.db.execute(
                "INSERT INTO documents(programme, kind, filename, stored_path, is_current, at)"
                " VALUES(?,?,?,?,1,?)",
                (programme, kind, filename, stored_path, _now()))
            self.db.commit()
            return int(cur.lastrowid)

    def documents(self, programme: str) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT id, kind, filename, stored_path, is_current, at FROM documents"
            " WHERE programme=? ORDER BY at DESC", (programme,)).fetchall()
        return [dict(r) for r in rows]

    def current_document(self, programme: str, kind: str) -> dict[str, Any] | None:
        r = self.db.execute(
            "SELECT id, kind, filename, stored_path, at FROM documents"
            " WHERE programme=? AND kind=? AND is_current=1", (programme, kind)).fetchone()
        return dict(r) if r else None


def _as_of(results: Iterable[dict]) -> str:
    """How recent an export is: its latest period with a posted result, "YYYY:S".

    Read from the export itself, so neither the filename nor the upload order
    matters. An export with nothing posted is "" -- older than any other, so it
    fills gaps but never replaces a snapshot. Text order is time order.

    Only an assessed result counts: a mark and a result code. Codes without a
    mark -- DE (deregistered), F/ (no DP), a vacation-work P -- are written
    mid-semester, and counting them dated the July 2025 augmented export as
    2025:2, level with January 2026.
    """
    posted = [(str(r.get("calendar_year") or ""),
               SEM_OF_BLOCK.get(str(r.get("block") or "").strip().upper(), 0))
              for r in results
              if (r.get("result_code") or "").strip() and r.get("grade") is not None]
    posted = [p for p in posted if p[0] and p[1]]
    return "%s:%d" % max(posted) if posted else ""


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
    for key in ("students", "results", "decisions", "colours"):
        if parsed.get(key):
            return parsed[key][0].get("programme", "")
    return ""
