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

from programme_loader import load_programme, fill_missing_credits
from advise import advise_student, check_additions
import regadvisor_engine as R
import ers_check as X
from checks_service import _complete_set
from datasource import in_progress_now

PASS_CODES = {"P", "PM"}

# A cohort is a cycle: a calendar year and a semester. The ERS block names the
# period; a supplementary block belongs to the semester it supplements, so S1 is
# semester 1 and S2 is semester 2. Block 0 (annual foundation) and the rare S3/S4
# are not cohort keys -- every such student also has a semester row, so none is
# lost. DECISION (Justin, 2026-08-18): S1->1, S2->2; 0/S3/S4 not cohort-scoped.
SEM_OF_BLOCK = {"1": 1, "S1": 1, "2": 2, "S2": 2}


def semester_of_block(block: str) -> int | None:
    return SEM_OF_BLOCK.get((block or "").strip().upper())


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
        self.ers_policy = ((self.cur or {}).get("rules") or {}).get("ers")
        # Raw rows, kept for transcript + names. Blank ERS credits are filled from
        # the rule file, the same fill the ERS check applies.
        self._raw = {sn: fill_missing_credits(rows, self.cur)
                     for sn, rows in store.results(programme).items()}
        self.results = {sn: X._shape_rows(rows) for sn, rows in self._raw.items()}
        self.bio = {r["student_number"]: r for r in store.students(programme)}
        # The registrar's statements, per student, for the standing below.
        self._decs: dict[str, list[dict[str, Any]]] = {}
        for d in store.decisions(programme):
            self._decs.setdefault(str(d["student_number"]), []).append(d)
        self._cols: dict[str, list[dict[str, Any]]] = {}
        for c in store.colour_rows(programme):
            self._cols.setdefault(str(c["student_number"]), []).append(c)
        self._complete = _complete_set(store, programme, self.cur)
        self._standings: dict[str, dict[str, Any]] = {}
        self._cache: dict[str, dict[str, Any]] = {}
        self.active_years = {sn: {str(r["calendar_year"]) for r in rows if r.get("calendar_year")}
                             for sn, rows in self.results.items()}
        self.years = sorted({y for ys in self.active_years.values() for y in ys}, reverse=True)
        self.current_year = self.years[0] if self.years else None
        # Each student's set of (year, semester) cohorts, from their result rows.
        self.active_cycles: dict[str, set[tuple[str, int]]] = {}
        for sn, rows in self.results.items():
            cs = set()
            for r in rows:
                sem = semester_of_block(r.get("block"))
                yr = str(r.get("calendar_year") or "")
                if sem and yr:
                    cs.add((yr, sem))
            self.active_cycles[sn] = cs
        self.cycles = sorted({c for cs in self.active_cycles.values() for c in cs},
                             reverse=True)
        self.current_cycle = self.cycles[0] if self.cycles else None

    # -- loaders -------------------------------------------------------------
    def _load_rules(self, yaml_path: str | None) -> dict[str, Any] | None:
        if not yaml_path:
            return None
        p = Path(yaml_path)
        if not p.exists():
            print(f"  note: rule file {yaml_path} not found -- advice held for {self.programme}")
            return None
        return load_programme(str(p))

    def standing(self, sn: str) -> dict[str, Any]:
        """The standing this student registers under, with the ERS check's own
        row for them as `check` -- one computation (ers_check.standing), so the
        standing here is the one the check reported."""
        if sn not in self._standings:
            self._standings[sn] = X.standing(
                self._raw.get(sn, []), self._decs.get(sn, []), self._cols.get(sn, []),
                self.ers_policy, degree_complete=sn in self._complete)
        return self._standings[sn]

    def _official(self, sn: str) -> dict[str, Any]:
        """What the registrar stated for the period that governs, or none."""
        st = self.standing(sn)
        if st["source"] != "registrar":
            return {"code": "", "text": "", "status": "none", "period": ""}
        return {k: st[k] for k in ("code", "text", "status", "period")}

    def _agree(self, sn: str) -> bool | None:
        """The ERS check's verdict for this student: same period, same inputs."""
        return {"match": True, "mismatch": False}.get(self.standing(sn)["check"]["verdict"])

    # -- interface -----------------------------------------------------------
    def _advise(self, sn: str) -> dict[str, Any] | None:
        if not self.advice_ready:
            return None
        if sn not in self._cache:
            self._cache[sn] = advise_student(
                self.cur, self.results[sn], standing=self.standing(sn))
        return self._cache[sn]
    
    def current_students(self) -> set[str]:
        """Students active in the current (latest) year. The registration
        workflow acts only on these; older cohorts stay in the record and in
        metrics but never enter triage. If no year is known, fall back to all."""
        if not self.current_year:
            return set(self.results)
        return {sn for sn, ys in self.active_years.items() if self.current_year in ys}
    
    def cohort(self, year: str, semester: int) -> set[str]:
        """The students active in one cycle (year + semester)."""
        key = (str(year), int(semester))
        return {sn for sn, cs in self.active_cycles.items() if key in cs}

    def cohorts(self) -> list[dict[str, Any]]:
        """Every cycle on offer, newest first, with a headcount -- for the picker."""
        return [{"year": y, "semester": s, "label": f"{y} \u00b7 Sem {s}",
                 "n": len(self.cohort(y, s))} for (y, s) in self.cycles]

    def list_students(self, year: str | None = None,
                      semester: int | None = None) -> list[dict[str, Any]]:
        members = self.cohort(year, semester) if (year and semester) else None
        out = []
        for sn in self.results:
            if members is not None and sn not in members:
                continue
            if members is None and year and year not in self.active_years.get(sn, set()):
                continue
            b = self.bio.get(sn, {})
            name = f"{b.get('surname','')}, {b.get('name','')}".strip(", ")
            st = self.standing(sn)
            out.append({"sn": sn, "name": name,
                        "year": b.get("year_of_study"),
                        "official": self._official(sn)["status"],
                        "engine": st["check"]["engine_status"],
                        "standing": st["status"], "agree": self._agree(sn)})
        out.sort(key=lambda x: x["name"].lower())
        return out

    # Supp blocks fall just after the semester they supplement, foundation first.
    _BLOCK_ORDER = {"0": 0.0, "1": 1.0, "S1": 1.5, "2": 2.0, "S2": 2.5}
    _BLOCK_LABEL = {"0": "Annual", "1": "Sem 1", "S1": "Supp 1", "2": "Sem 2", "S2": "Supp 2"}

    def _transcript(self, sn: str) -> list[dict[str, Any]]:
        """Every result the student has, grouped by period, oldest first.
        The complete history a coordinator needs to weigh a concession."""
        periods: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for r in self._raw.get(sn, []):
            code = (r.get("module_code") or "").strip()
            if not code:
                continue
            rc = (r.get("result_code") or "").strip().upper()
            mark = r.get("grade")
            passed = (rc in PASS_CODES) or (not rc and mark is not None and mark >= 50)
            yr, blk = str(r.get("calendar_year") or ""), (r.get("block") or "").strip()
            periods.setdefault((yr, blk), []).append(
                {"code": code, "name": r.get("module_name") or "",
                 "credits": r.get("credits") or 0, "mark": mark,
                 "result_code": rc, "result_text": r.get("result_text") or "",
                 "passed": passed})
        out = []
        for (yr, blk) in sorted(periods, key=lambda k: (k[0], self._BLOCK_ORDER.get(k[1], 9))):
            mods = sorted(periods[(yr, blk)], key=lambda m: m["code"])
            label = f"{yr} {self._BLOCK_LABEL.get(blk, blk)}"
            out.append({"period": label, "modules": mods})
        return out

    def get_student(self, sn: str) -> dict[str, Any] | None:
        if sn not in self.results:
            return None
        a = self._advise(sn)
        official = self._official(sn)
        b = self.bio.get(sn, {"student_number": sn})
        bio = {"sn": sn, "surname": b.get("surname", ""), "name": b.get("name", ""),
               "year_of_study": b.get("year_of_study"), "plan_code": b.get("plan_code", "")}
        if a is None:
            # Rules not authored yet: show who they are and where they stand.
            return {"bio": bio, "advice_ready": False, "official": official,
                    "engine": None, "agree": None, "cap": None,
                    "advice": {k: [] for k in ("can_register", "concession_possible",
                               "cannot_register", "needs_review", "passed")}}
        tx, cap, adv = a["tx"], a["cap"], a["advice"]
        chk = self.standing(sn)["check"]
        in_progress = in_progress_now(self.results[sn], self.current_year)

        twins = (self.cur or {}).get("twins") or {}
        attempts = tx.get("attempts", {})
        cl = tx.get("core_len")
        names = {r.get("module_code"): r.get("module_name")
                 for r in self._raw.get(sn, []) if r.get("module_code")}

        def reroute(code: str) -> tuple[str, dict[str, Any] | None, str | None]:
            """A failed augmented L1 module is repeated in its mainstream twin --
            the augmented section is not re-offered. Show the mainstream code,
            carrying the best mark across the pair so the near-miss stays visible.

            DECISION (Justin Pringle, 2026-09-15): the handbook rule is that
            failing an augmented module OBLIGES the student to take the
            mainstream twin. Routing therefore fires on the failure itself, not
            on evidence that the student has already sat the twin. This
            supersedes the 2026-08-26 twin-attempt rule, which left a failed
            student still showing an augmented code they cannot re-register.

            Two cases deliberately do NOT route, to stay fail-safe:
              - the augmented module has no recorded outcome yet (in progress,
                or supplementary granted and not yet sat) -- not a fail;
              - either twin has been passed -- nothing to repeat."""
            main = twins.get(code)
            if not main:
                return code, R._best(tx, code), None
            aug_b, main_b = R._best(tx, code), R._best(tx, main)
            if (aug_b and aug_b["passed"]) or (main_b and main_b["passed"]):
                return code, R._best(tx, code), None
            sat_twin = attempts.get(R.core_code(main, cl), 0) > 0
            failed_aug = bool(aug_b) and aug_b.get("mark") is not None
            if not (failed_aug or sat_twin):
                # augmented module never sat, or sat with no result yet
                return code, R._best(tx, code), None
            cands = [b for b in (aug_b, main_b) if b and b["mark"] is not None]
            best = max(cands, key=lambda b: b["mark"]) if cands else None
            return main, best, code

        def slim(bucket: list[dict[str, Any]]) -> list[dict[str, Any]]:
            out = []
            for x in bucket:
                code, best, twin_of = reroute(x["code"])
                carry = []
                for p in x.get("prereq_check", {}).get("missing", []):
                    ps = str(p)
                    if ps.startswith("review:") or ">=" in ps:
                        continue
                    pb = R._best(tx, ps)
                    carry.append({"code": ps, "mark": pb["mark"] if pb else None})
                row = {"code": code, "name": names.get(code) or x.get("name", ""),
                       "credits": x.get("credits", 0),
                       "unmet": x.get("prereq_check", {}).get("unmet", []),
                       "mark": best["mark"] if best else None,
                       "prereq_marks": carry}
                if twin_of:
                    row["twin_of"] = twin_of
                out.append(row)
            return out

        return {
            "bio": {**bio, "gpa": round(tx.get("gpa_passed", tx["gpa"])),
                    "credits_passed": round(tx["credits_passed"]),
                    "passed_count": len(tx["passed_set"]),
                    "semesters": tx["semesters_registered"], "in_progress": in_progress},
            "transcript": self._transcript(sn),
            "advice_ready": True,
            "official": official,
            # The ERS check's own row for this student -- the cross-check.
            "engine": {"status": chk["engine_status"], "code": chk["engine_code"],
                       "label": chk["engine_label"],
                       "cumulative_pct": chk["cumulative_pct"],
                       "semester_pct": chk["semester_pct"], "period": chk["period"]},
            # What the cap and probation load are read from: the registrar's
            # standing where stated, the engine's where not.
            "standing": {k: a["ers"][k] for k in ("status", "code", "label", "source")},
            "agree": self._agree(sn),
            "cap": cap,
            "advice": {k: slim(adv[k]) for k in
                       ("can_register", "concession_possible", "cannot_register",
                        "needs_review", "passed")},
        }

    def check(self, sn: str, codes: list[str]) -> dict[str, Any] | None:
        if sn not in self.results or not self.advice_ready:
            return None
        return check_additions(self.cur, self.results[sn], codes,
                               standing=self.standing(sn))
