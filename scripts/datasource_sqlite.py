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
from regadvisor_engine import code_level
import regadvisor_engine as R
from standing_codes import status_of, status_of_colour, EXCLUDE_CODES
from datasource import in_progress_now

# The registrar-code -> standing map lives in standing_codes, shared with the
# checker so the two never drift. status_of() resolves an unrecognised code to
# "review" -- never green -- so a readmit or suspended student is never cleared.
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
        self.results = self._load_results()
        self.bio = {r["student_number"]: r for r in store.students(programme)}
        self.history = self._load_history()
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

    def _load_results(self) -> dict[str, list[dict[str, Any]]]:
        """Store rows -> engine shape, mirroring data_loaders.load_results."""
        out: dict[str, list[dict[str, Any]]] = {}
        for sn, rows in self._raw.items():
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
        """Each student's standing as the registrar last stated it.

        The newest COLOUR period, not the newest code. A code is written only
        when something needs saying, so the newest one can be years old: reading
        it as the student's standing today leaves someone flagged at risk long
        after the registrar returned them to green. The colour block runs to the
        current period, so its last row is the right place to look; the code for
        that same period is used when there is one, since it says more than the
        colour does.
        """
        latest = self.store.latest_decisions(self.programme)
        colours = self.store.colour_codes(self.programme)
        codes = {(str(d["student_number"]), f"{d['calendar_year']}:{d['semester']}"):
                 (d.get("term_code") or "", d.get("term_text") or "")
                 for d in self.store.decisions(self.programme)}

        def period_key(p: str) -> tuple[str, int]:
            y, _, s = p.partition(":")
            return (y, int(s) if s.isdigit() else 0)

        out: dict[str, dict[str, Any]] = {}
        for sn, h in latest.items():                 # no colours: as before
            out[sn] = {"code": h["code"], "text": h["text"], "period": "",
                       "status": status_of(h["code"], self.ers_policy)}
        for sn, periods in colours.items():
            if not periods:
                continue
            last = max(periods, key=period_key)
            code, text = codes.get((sn, last), ("", ""))
            out[sn] = {"code": code, "text": text or periods[last]["text"],
                       "period": last,
                       "status": (status_of(code, self.ers_policy) if code
                                  else status_of_colour(periods[last]["colour"],
                                                        self.ers_policy))}
        return out

    def _engine_history(self, sn: str) -> dict[str, Any]:
        h = self.history.get(sn)
        if not h:
            return {"last_status": "none", "appeals_exhausted": False}
        return {"last_status": h["status"], "appeals_exhausted": h["code"] in EXCLUDE_CODES}

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
            official = self.history.get(sn, {}).get("status", "none")
            a = self._advise(sn)
            engine = a["ers"]["status"] if a else official
            out.append({"sn": sn, "name": name,
                        "year": b.get("year_of_study"),
                        "official": official, "engine": engine,
                        "agree": None if official == "none" else official == engine})
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
        h = self.history.get(sn, {})
        b = self.bio.get(sn, {"student_number": sn})
        bio = {"sn": sn, "surname": b.get("surname", ""), "name": b.get("name", ""),
               "year_of_study": b.get("year_of_study"), "plan_code": b.get("plan_code", "")}
        if a is None:
            # Rules not authored yet: show who they are and where they stand.
            return {"bio": bio, "advice_ready": False,
                    "official": {"code": h.get("code", ""), "text": h.get("text", ""),
                                 "status": h.get("status", "none")},
                    "engine": None, "agree": None, "cap": None,
                    "advice": {k: [] for k in ("can_register", "concession_possible",
                               "cannot_register", "needs_review", "passed")}}
        tx, m, ers, cap, adv = a["tx"], a["metrics"], a["ers"], a["cap"], a["advice"]
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
            "official": {"code": h.get("code", ""), "text": h.get("text", ""),
                         "status": h.get("status", "none")},
            "engine": {"status": ers["status"], "code": ers["code"], "label": ers["label"],
                       "cumulative_pct": round(m["cumulative"]["credit_pct_passed"] * 100),
                       "semester_pct": round(m["semester"]["credit_pct_passed"] * 100),
                       "period": m["semester"]["period"]},
            "agree": None if h.get("status", "none") == "none" else h["status"] == ers["status"],
            "cap": cap,
            "advice": {k: slim(adv[k]) for k in
                       ("can_register", "concession_possible", "cannot_register",
                        "needs_review", "passed")},
        }

    def check(self, sn: str, codes: list[str]) -> dict[str, Any] | None:
        if sn not in self.results or not self.advice_ready:
            return None
        return check_additions(self.cur, self.results[sn], codes, policy=None)
