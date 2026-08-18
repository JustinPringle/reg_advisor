"""
datasource_sqlite.py -- a programme-scoped source backed by the SQLite store.

It answers the same three questions the CSV source does, so the server and page
never change:

    list_students()   -> picker rows for the selected programme
    get_student(sn)   -> profile + ERS standing + advice buckets
    check(sn, codes)  -> CLEARED / REVIEW for a candidate plan

Two things it adds over the CSV source. First, it is built for one programme and
reads only that programme's rows, so Civil and Augmented Civil stay separate.
Second, if a programme's rule file is not authored yet, the source still lists
students and shows the registrar's standing; only the prerequisite advice waits
for the YAML. Nothing mis-clears in the meantime -- advice is simply empty.

    from store import Store
    src = SqliteSource(Store("data/advisor.db"), "ENG-CIVIL")
"""
from __future__ import annotations
from typing import Any
from pathlib import Path

from programme_loader import load_programme
from advise import advise_student, check_additions
from regadvisor_engine import code_level
import regadvisor_engine as R

# Registrar term code -> engine standing. Unknown / absent -> good standing
# (fail-safe: an unrecognised code never invents probation).
STATUS_OF_CODE: dict[str, str] = {
    "CO": "green", "GREEN": "green", "BLUE": "green",
    "RISK": "orange", "RSK2": "orange",
    "PROB": "red", "FPRR": "red", "FPRD": "red", "FPMA": "red", "FPDS": "red",
    "XNFA": "exclude", "XACA": "exclude", "XAC": "exclude",
}
_EXCLUDE = {"XNFA", "XACA", "XAC"}
PASS_CODES = {"P", "PM"}


def list_programmes(store: Any) -> list[dict[str, Any]]:
    """Programmes on offer, for the picker."""
    return store.programmes()


class SqliteSource:
    def __init__(self, store: Any, programme: str) -> None:
        self.store = store
        self.programme = programme
        meta = store.programme(programme) or {}
        self.name = meta.get("name") or programme
        self.cur = self._load_rules(meta.get("yaml_path"))
        self.advice_ready = self.cur is not None
        self.results = self._load_results()
        self.bio = {r["student_number"]: r for r in store.students(programme)}
        self.history = self._load_history()
        self._cache: dict[str, dict[str, Any]] = {}
        self.active_years = {sn: {str(r["calendar_year"]) for r in rows if r.get("calendar_year")}
                             for sn, rows in self.results.items()}
        self.years = sorted({y for ys in self.active_years.values() for y in ys}, reverse=True)
        self.current_year = self.years[0] if self.years else None

    # -- loaders -------------------------------------------------------------
    def _load_rules(self, yaml_path: str | None) -> dict[str, Any] | None:
        if not yaml_path:
            return None
        p = Path(yaml_path)
        if not p.exists():
            print(f"  note: rule file {yaml_path} not found -- advice held for {self.programme}")
            return None
        return load_programme(str(p))

    def _load_results(self) -> dict[str, list[dict[str, Any]]]:
        """Store rows -> engine shape, mirroring data_loaders.load_results."""
        out: dict[str, list[dict[str, Any]]] = {}
        for sn, rows in self.store.results(self.programme).items():
            shaped = []
            for r in rows:
                code = (r.get("module_code") or "").strip()
                rc = (r.get("result_code") or "").strip().upper()
                mark = r.get("grade")
                passed = (rc in PASS_CODES) or (not rc and mark is not None and mark >= 50)
                shaped.append({
                    "student_number": sn,
                    "period": f"{r.get('calendar_year','')}:{r.get('block','')}",
                    "calendar_year": r.get("calendar_year", ""),
                    "block": (r.get("block") or "").strip(),
                    "course_code": code, "result_code": rc,
                    "credits": r.get("credits") or 0, "mark": mark, "passed": passed,
                    "year_of_study": r.get("year_of_study") or 0,
                    "level": code_level(code)})
            out[sn] = shaped
        return out

    def _load_history(self) -> dict[str, dict[str, Any]]:
        latest = self.store.latest_decisions(self.programme)
        return {sn: {"code": h["code"], "text": h["text"],
                     "status": STATUS_OF_CODE.get(h["code"], "green")}
                for sn, h in latest.items()}

    def _engine_history(self, sn: str) -> dict[str, Any]:
        h = self.history.get(sn)
        if not h:
            return {"last_status": "none", "appeals_exhausted": False}
        return {"last_status": h["status"], "appeals_exhausted": h["code"] in _EXCLUDE}

    # -- interface -----------------------------------------------------------
    def _advise(self, sn: str) -> dict[str, Any] | None:
        if not self.advice_ready:
            return None
        if sn not in self._cache:
            self._cache[sn] = advise_student(
                self.cur, self.results[sn], history=self._engine_history(sn))
        return self._cache[sn]
    
    def current_students(self) -> set[str]:
        """Students active in the current (latest) year. The registration
        workflow acts only on these; older cohorts stay in the record and in
        metrics but never enter triage. If no year is known, fall back to all."""
        if not self.current_year:
            return set(self.results)
        return {sn for sn, ys in self.active_years.items() if self.current_year in ys}
    
    def list_students(self, year: str | None = None) -> list[dict[str, Any]]:
        out = []
        for sn in self.results:
            if year and year not in self.active_years.get(sn, set()):
                continue
            b = self.bio.get(sn, {})
            name = f"{b.get('surname','')}, {b.get('name','')}".strip(", ")
            official = self.history.get(sn, {}).get("status", "green")
            a = self._advise(sn)
            engine = a["ers"]["status"] if a else official
            out.append({"sn": sn, "name": name,
                        "year": b.get("year_of_study"),
                        "official": official, "engine": engine,
                        "agree": official == engine})
        out.sort(key=lambda x: x["name"].lower())
        return out

    def get_student(self, sn: str) -> dict[str, Any] | None:
        if sn not in self.results:
            return None
        a = self._advise(sn)
        h = self.history.get(sn, {})
        b = self.bio.get(sn, {"student_number": sn})
        bio = {"sn": sn, "surname": b.get("surname", ""), "name": b.get("name", ""),
               "year_of_study": b.get("year_of_study"), "plan_code": b.get("plan_code", "")}
        if a is None:
            # Rules not authored yet: show who they are and where they stand.
            return {"bio": bio, "advice_ready": False,
                    "official": {"code": h.get("code", ""), "text": h.get("text", ""),
                                 "status": h.get("status", "green")},
                    "engine": None, "agree": None, "cap": None,
                    "advice": {k: [] for k in ("can_register", "concession_possible",
                               "cannot_register", "needs_review", "passed")}}
        tx, m, ers, cap, adv = a["tx"], a["metrics"], a["ers"], a["cap"], a["advice"]
        in_progress = sorted({r["course_code"] for r in self.results[sn]
                              if not r["result_code"] and r["mark"] is None and r["course_code"]})

        def slim(bucket: list[dict[str, Any]]) -> list[dict[str, Any]]:
            out = []
            for x in bucket:
                best = R._best(tx, x["code"])
                carry = []
                for p in x.get("prereq_check", {}).get("missing", []):
                    ps = str(p)
                    if ps.startswith("review:") or ">=" in ps:
                        continue
                    pb = R._best(tx, ps)
                    carry.append({"code": ps, "mark": pb["mark"] if pb else None})
                out.append({"code": x["code"], "name": x.get("name", ""),
                            "credits": x.get("credits", 0),
                            "unmet": x.get("prereq_check", {}).get("unmet", []),
                            "mark": best["mark"] if best else None,
                            "prereq_marks": carry})
            return out

        return {
            "bio": {**bio, "gpa": round(tx["gpa"]),
                    "credits_passed": round(tx["credits_passed"]),
                    "passed_count": len(tx["passed_set"]),
                    "semesters": tx["semesters_registered"], "in_progress": in_progress},
            "advice_ready": True,
            "official": {"code": h.get("code", ""), "text": h.get("text", ""),
                         "status": h.get("status", "green")},
            "engine": {"status": ers["status"], "code": ers["code"], "label": ers["label"],
                       "cumulative_pct": round(m["cumulative"]["credit_pct_passed"] * 100),
                       "semester_pct": round(m["semester"]["credit_pct_passed"] * 100),
                       "period": m["semester"]["period"]},
            "agree": h.get("status", "green") == ers["status"],
            "cap": cap,
            "advice": {k: slim(adv[k]) for k in
                       ("can_register", "concession_possible", "cannot_register",
                        "needs_review", "passed")},
        }

    def check(self, sn: str, codes: list[str]) -> dict[str, Any] | None:
        if sn not in self.results or not self.advice_ready:
            return None
        return check_additions(self.cur, self.results[sn], codes, policy=None)
