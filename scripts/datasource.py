"""
datasource.py -- the seam between stored data and the advisor.

Everything above this line (engines, server, page) speaks one small interface:

    list_students()   -> [{sn, name, year, official, engine, agree}]
    get_student(sn)   -> full profile + ERS standing + advice buckets
    check(sn, codes)  -> per-module CLEARED / REVIEW for a candidate plan

Today CsvSource reads the two files the parser writes plus the authored
curriculum. Tomorrow ItsSource reads the university system read-only, returns the
same shapes, and nothing else changes. Swap the class in server.py; the page and
the engines never know.

The registrar's own term-decision code (from ers_history.csv) is the authoritative
standing. The engine is run beside it as a cross-check, so a disagreement between
the two is visible rather than hidden.
"""
from __future__ import annotations
from typing import Any
import csv
from pathlib import Path

from programme_loader import load_programme
from data_loaders import load_results
from advise import advise_student, check_additions
import regadvisor_engine as R

ROOT = Path(__file__).resolve().parents[1]
YAML = ROOT / "programmes" / "civil.yaml"
RESULTS = ROOT / "data" / "ers_data.csv"
HISTORY = ROOT / "data" / "ers_history.csv"

# Registrar term code -> engine standing. Absent / empty / unknown -> good
# standing (fail-safe: an unknown code never invents probation).
STATUS_OF_CODE: dict[str, str] = {
    "CO": "green", "GREEN": "green", "BLUE": "green",
    "RISK": "orange", "RSK2": "orange",
    "PROB": "red", "FPRR": "red", "FPRD": "red", "FPMA": "red", "FPDS": "red",
    "XNFA": "exclude", "XACA": "exclude", "XAC": "exclude",
}
_EXCLUDE = {"XNFA", "XACA", "XAC"}


class CsvSource:
    """Read the parser's output and the authored curriculum."""

    def __init__(self) -> None:
        self.cur = load_programme(str(YAML))
        self.results = load_results(str(RESULTS))
        self.bio = self._load_bio()
        self.history = self._load_history()
        self._cache: dict[str, dict[str, Any]] = {}

    # -- file loaders --------------------------------------------------------
    def _load_bio(self) -> dict[str, dict[str, Any]]:
        bio: dict[str, dict[str, Any]] = {}
        with open(RESULTS, newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                sn = (r.get("student_number") or "").strip()
                if not sn:
                    continue
                bio.setdefault(sn, {
                    "sn": sn,
                    "surname": (r.get("surname") or "").strip(),
                    "name": (r.get("name") or "").strip(),
                    "year_of_study": _num(r.get("year_of_study")),
                    "total_credits": _num(r.get("total_credits")),
                })
        return bio

    def _load_history(self) -> dict[str, dict[str, Any]]:
        """Latest term-decision row per student -> {code, text, status}."""
        latest: dict[str, dict[str, Any]] = {}
        if not HISTORY.exists():
            return latest
        with open(HISTORY, newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                sn = (r.get("student_number") or "").strip()
                code = (r.get("term_code") or "").strip().upper()
                if not sn or not code:
                    continue
                key = (r.get("calendar_year"), r.get("semester"))
                cur = latest.get(sn)
                if cur is None or key > cur["_key"]:
                    latest[sn] = {"_key": key, "code": code,
                                  "text": (r.get("term_text") or "").strip(),
                                  "status": STATUS_OF_CODE.get(code, "green")}
        return latest

    def _engine_history(self, sn: str) -> dict[str, Any]:
        """The prior-term status the ERS engine reads, drawn from the PDF code."""
        h = self.history.get(sn)
        if not h:
            return {"last_status": "none", "appeals_exhausted": False}
        return {"last_status": h["status"],
                "appeals_exhausted": h["code"] in _EXCLUDE}

    # -- interface -----------------------------------------------------------
    def _advise(self, sn: str) -> dict[str, Any]:
        if sn not in self._cache:
            self._cache[sn] = advise_student(
                self.cur, self.results[sn], history=self._engine_history(sn))
        return self._cache[sn]

    def list_students(self) -> list[dict[str, Any]]:
        out = []
        for sn in self.results:
            b = self.bio.get(sn, {"name": "", "surname": "", "year_of_study": None})
            official = self.history.get(sn, {}).get("status", "green")
            engine = self._advise(sn)["ers"]["status"]
            out.append({"sn": sn,
                        "name": f"{b['surname']}, {b['name']}".strip(", "),
                        "year": b.get("year_of_study"),
                        "official": official, "engine": engine,
                        "agree": official == engine})
        out.sort(key=lambda x: x["name"].lower())
        return out

    def get_student(self, sn: str) -> dict[str, Any] | None:
        if sn not in self.results:
            return None
        a = self._advise(sn)
        tx, m, ers, cap, adv = a["tx"], a["metrics"], a["ers"], a["cap"], a["advice"]
        h = self.history.get(sn, {})
        in_progress = sorted({r["course_code"] for r in self.results[sn]
                              if not r["result_code"] and r["mark"] is None
                              and r["course_code"]})

        def slim(bucket: list[dict[str, Any]]) -> list[dict[str, Any]]:
            out = []
            for x in bucket:
                b = R._best(tx, x["code"])
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
                            "mark": b["mark"] if b else None,
                            "prereq_marks": carry})
            return out

        return {
            "bio": {**self.bio.get(sn, {"sn": sn}),
                    "gpa": round(tx["gpa"]), "credits_passed": round(tx["credits_passed"]),
                    "passed_count": len(tx["passed_set"]),
                    "semesters": tx["semesters_registered"],
                    "in_progress": in_progress},
            "official": {"code": h.get("code", ""), "text": h.get("text", ""),
                         "status": h.get("status", "green")},
            "engine": {"status": ers["status"], "code": ers["code"],
                       "label": ers["label"],
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
        if sn not in self.results:
            return None
        return check_additions(self.cur, self.results[sn], codes,
                               policy=None)


def _num(v: Any) -> float | None:
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


class ItsSource:
    """Placeholder for read-only ITS Integrator access. Implement the same three
    methods returning the same shapes, and server.py swaps to it unchanged."""
    def __init__(self) -> None:
        raise NotImplementedError("ITS read-only source not yet wired")
