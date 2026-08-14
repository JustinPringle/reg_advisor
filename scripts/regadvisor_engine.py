"""
regadvisor_engine.py -- prerequisite-advice engine.

A Python port of AutoScholar's regadvisor.engine.js: pure functions over
(curriculum, transcript) -> advice. No I/O, no DOM. Adapted to UKZN data and
extended so the prerequisite grammar can express the aggregate conditions real
handbooks use ("Must be in 3rd yr", ">= 62cr level 1"), not just course codes.

The transcript index carries enough context (passed set, marks, credits by
level, year of study, semesters registered) for every term type to resolve.
Unknown / unparseable terms return `met=True` so malformed data never
false-blocks a student -- but the curriculum loader marks genuinely opaque
conditions as `review`, which DOES route the module to a human.
"""
from __future__ import annotations
from typing import Any
import re

DEFAULT_CORE_LEN = 7


# --- Course-code helpers ----------------------------------------------------
def core_code(code: Any, length: int | None = None) -> str:
    """First `length` chars of a real SMS code -- collapses a supplementary
    sitting onto its base course. Synthetic slots (containing _ - or space) are
    left whole so two distinct electives never merge. Idempotent."""
    s = "" if code is None else str(code).strip()
    n = DEFAULT_CORE_LEN if not length or length <= 0 else length
    if len(s) <= n or not re.fullmatch(r"[A-Za-z0-9]+", s):
        return s
    return s[:n]


def code_level(code: str) -> int:
    """The NQF-ish level = first digit of the numeric part (ENCV2SA -> 2)."""
    m = re.search(r"[A-Za-z]+(\d)", str(code))
    return int(m.group(1)) if m else 0


# --- Transcript index -------------------------------------------------------
def index_transcript(results: list[dict[str, Any]],
                     pass_codes: set[str] | None = None,
                     pass_mark: float = 50,
                     core_len: int = DEFAULT_CORE_LEN) -> dict[str, Any]:
    """Build the index the engine reads: best result + attempts per course,
    passed set, gpa (mean of passed marks), credits passed overall and by level,
    plus year_of_study / semesters_registered carried from the rows.

    A row passes if result_code in pass_codes, or (no code) mark >= pass_mark.
    Everything keys on the core code so re-sits collapse (best wins)."""
    pass_codes = pass_codes or {"P", "PM"}
    best: dict[str, dict[str, Any]] = {}
    attempts: dict[str, int] = {}
    for r in results or []:
        code = core_code(r.get("course_code") or r.get("code"), core_len)
        if not code:
            continue
        rc = str(r.get("result_code") or "").upper().strip()
        mark_raw = r.get("mark", r.get("finalMark"))
        try:
            mark = float(mark_raw)
        except (TypeError, ValueError):
            mark = None
        # A not-yet-graded registration (no code, no mark) is not an attempt.
        if not rc and mark is None:
            continue
        passed = (rc in pass_codes) or (not rc and mark is not None and mark >= pass_mark)
        attempts[code] = attempts.get(code, 0) + 1
        score = (1000 + (mark if mark is not None else pass_mark)) if passed else (mark or 0)
        cur = best.get(code)
        cur_score = ((1000 + (cur["mark"] or pass_mark)) if cur and cur["passed"]
                     else (cur["mark"] or 0)) if cur else -1
        if score > cur_score:
            best[code] = {"code": code, "passed": passed, "mark": mark,
                          "credits": float(r.get("credits") or 0)}
    passed_marks = [b["mark"] for b in best.values() if b["passed"] and b["mark"] is not None]
    gpa = sum(passed_marks) / len(passed_marks) if passed_marks else 0.0
    passed_set = {c for c, b in best.items() if b["passed"]}
    credits_passed = sum(b["credits"] for b in best.values() if b["passed"])
    credits_by_level: dict[int, float] = {}
    for c, b in best.items():
        if b["passed"]:
            lv = code_level(c)
            credits_by_level[lv] = credits_by_level.get(lv, 0.0) + b["credits"]
    # Context carried straight from the rows (constant per student in this feed).
    yos = 0
    sems = set()
    for r in results or []:
        try:
            yos = max(yos, int(float(r.get("year_of_study") or 0)))
        except (TypeError, ValueError):
            pass
        blk = str(r.get("block") or "")
        if blk in ("1", "2"):
            sems.add((r.get("calendar_year"), blk))
    return {"best": best, "attempts": attempts, "passed_set": passed_set,
            "gpa": gpa, "credits_passed": credits_passed,
            "credits_by_level": credits_by_level, "year_of_study": yos,
            "semesters_registered": len(sems), "core_len": core_len}


def _has(tx: dict[str, Any], code: str) -> bool:
    return core_code(code, tx.get("core_len")) in tx.get("passed_set", set())


def _best(tx: dict[str, Any], code: str) -> dict[str, Any] | None:
    return tx.get("best", {}).get(core_code(code, tx.get("core_len")))


# --- Prerequisite term evaluation -------------------------------------------
def eval_term(term: Any, tx: dict[str, Any]) -> dict[str, Any]:
    """Evaluate ONE prerequisite term against the transcript index.

    Supported term shapes (nest arbitrarily):
      "CODE"                        course passed
      {"code","min_mark"}           passed AND best mark >= min_mark
      {"code","soft":True}          recommended only, never blocks
      {"all":[...]} / [...]         AND
      {"any":[...]}                 OR
      {"any_n","of":[...]}          at least any_n satisfied
      {"min_year":N}                student in year N of study or later
      {"min_credits":N,"level":L?}  N passed credits (optionally at level L)
      {"review": "text"}            NEVER auto-satisfied -> forces human review
    Unknown shapes are permissive so malformed data never false-blocks.
    Returns {met, soft, label, missing:[code]}.
    """
    if term is None:
        return {"met": True, "soft": False, "label": "", "missing": []}
    if isinstance(term, str):
        ok = _has(tx, term)
        return {"met": ok, "soft": False, "label": term, "missing": [] if ok else [term]}
    if isinstance(term, list):
        return eval_term({"all": term}, tx)

    if "all" in term:
        rs = [eval_term(t, tx) for t in term["all"]]
        hard = [r for r in rs if not r["soft"]]
        return {"met": all(r["met"] for r in hard), "soft": False,
                "label": " & ".join(r["label"] for r in rs),
                "missing": [m for r in hard if not r["met"] for m in r["missing"]]}
    if "any" in term:
        rs = [eval_term(t, tx) for t in term["any"]]
        met = any(r["met"] for r in rs)
        return {"met": met, "soft": False,
                "label": "(" + " | ".join(r["label"] for r in rs) + ")",
                "missing": [] if met else [m for r in rs for m in r["missing"]]}
    if "any_n" in term and "of" in term:
        rs = [eval_term(t, tx) for t in term["of"]]
        met = sum(1 for r in rs if r["met"]) >= term["any_n"]
        return {"met": met, "soft": False,
                "label": f"{term['any_n']}-of-(" + ", ".join(r["label"] for r in rs) + ")",
                "missing": [] if met else [m for r in rs if not r["met"] for m in r["missing"]]}
    if "review" in term:  # opaque handbook condition -> always route to a human
        return {"met": False, "soft": False, "label": f"[review: {term['review']}]",
                "missing": [f"review:{term['review']}"]}
    if "min_year" in term:
        ok = tx.get("year_of_study", 0) >= term["min_year"]
        return {"met": ok, "soft": False, "label": f"year>={term['min_year']}",
                "missing": [] if ok else [f"year>={term['min_year']}"]}
    if "min_sem" in term:
        ok = tx.get("semesters_registered", 0) >= term["min_sem"]
        return {"met": ok, "soft": False, "label": f"sem>={term['min_sem']}",
                "missing": [] if ok else [f"sem>={term['min_sem']}"]}
    if "min_credits" in term:
        lv = term.get("level")
        have = (tx.get("credits_by_level", {}).get(lv, 0.0) if lv is not None
                else tx.get("credits_passed", 0.0))
        ok = have >= term["min_credits"]
        lab = f">={term['min_credits']}cr" + (f" L{lv}" if lv is not None else "")
        return {"met": ok, "soft": False, "label": lab,
                "missing": [] if ok else [lab]}
    if "code" in term:
        if term.get("soft"):
            return {"met": True, "soft": True, "label": term["code"] + " (rec)",
                    "missing": [] if _has(tx, term["code"]) else [term["code"]]}
        b = _best(tx, term["code"])
        if term.get("min_mark") is not None:
            ok = bool(b and b["passed"] and (b["mark"] is None or b["mark"] >= term["min_mark"]))
            return {"met": ok, "soft": False, "label": f"{term['code']}>={term['min_mark']}",
                    "missing": [] if ok else [term["code"]]}
        ok = _has(tx, term["code"])
        return {"met": ok, "soft": False, "label": term["code"],
                "missing": [] if ok else [term["code"]]}
    return {"met": True, "soft": False, "label": "", "missing": []}


def check_prereqs(mod: dict[str, Any], tx: dict[str, Any]) -> dict[str, Any]:
    """AND over a module's prereq list. Soft terms never block.
    -> {met, missing:[code], unmet:[label], soft:[code]}."""
    pr = mod.get("prereqs") or []
    if not pr:
        return {"met": True, "missing": [], "unmet": [], "soft": []}
    rs = [eval_term(t, tx) for t in pr]
    hard = [r for r in rs if not r["soft"]]
    return {"met": all(r["met"] for r in hard),
            "missing": [m for r in hard if not r["met"] for m in r["missing"]],
            "unmet": [r["label"] for r in hard if not r["met"]],
            "soft": [m for r in rs if r["soft"] and r["missing"] for m in r["missing"]]}


# --- The four-bucket advice classifier --------------------------------------
def eval_advice(curriculum: dict[str, Any], tx: dict[str, Any],
                concession_gpa: float = 55, max_missing: int = 1) -> dict[str, Any]:
    """Classify every prescribed course into four buckets.

    can_register        : prereqs met, not yet passed
    concession_possible : near-miss -> route to a human (gpa high, <=1 missing)
    cannot_register     : blocked
    repeat_needed       : attempted before (overlay flag; a course can be both
                          repeat_needed and one of the three above)
    passed              : already done
    """
    out = {"can_register": [], "concession_possible": [], "cannot_register": [],
           "needs_review": [], "repeat_needed": [], "passed": []}
    for mod in curriculum.get("modules", []):
        if mod.get("type") in ("free_elective", "core_elective", "elective"):
            continue
        b = _best(tx, mod["code"])
        if b and b["passed"]:
            out["passed"].append(dict(mod))
            continue
        pc = check_prereqs(mod, tx)
        attempted = tx.get("attempts", {}).get(core_code(mod["code"], tx.get("core_len")), 0) > 0
        row = {**mod, "prereq_check": pc, "is_repeat": attempted}
        if attempted:
            out["repeat_needed"].append(row)
        # An opaque handbook condition can't be scored -- never call it a
        # near-miss. It goes to a human either way.
        has_review = any(str(m).startswith("review:") for m in pc["missing"])
        hard_missing = [m for m in pc["missing"] if not str(m).startswith("review:")]
        if pc["met"]:
            out["can_register"].append(row)
        elif has_review:
            out["needs_review"].append(row)
        elif tx.get("gpa", 0) >= concession_gpa and len(hard_missing) <= max_missing:
            out["concession_possible"].append(row)
        else:
            out["cannot_register"].append(row)
    return out


# --- Curricular-analytics extras --------------------------------------------
def prereq_codes(mod: dict[str, Any]) -> list[str]:
    """Flatten a module's prereq tree to the set of course codes it names."""
    out: list[str] = []

    def walk(t: Any) -> None:
        if t is None:
            return
        if isinstance(t, str):
            out.append(t)
        elif isinstance(t, list):
            for x in t:
                walk(x)
        elif isinstance(t, dict):
            for key in ("all", "any", "of"):
                if key in t:
                    for x in t[key]:
                        walk(x)
                    return
            if "code" in t:
                out.append(t["code"])
    for t in mod.get("prereqs") or []:
        walk(t)
    return list(dict.fromkeys(out))


def blocking_factor(curriculum: dict[str, Any]) -> dict[str, int]:
    """Heileman blocking factor: downstream courses that transitively require
    each course. High = failing it stalls many."""
    mods = curriculum.get("modules", [])
    dep_of: dict[str, list[str]] = {}
    for m in mods:
        for pc in prereq_codes(m):
            dep_of.setdefault(pc, []).append(m["code"])
    out: dict[str, int] = {}
    for m in mods:
        seen: set[str] = set()
        queue = [m["code"]]
        while queue:
            c = queue.pop(0)
            for d in dep_of.get(c, []):
                if d not in seen:
                    seen.add(d)
                    queue.append(d)
        out[m["code"]] = len(seen)
    return out


def concession_evidence(curriculum: dict[str, Any], tx: dict[str, Any],
                        course_code: str, **opts: Any) -> dict[str, Any]:
    """0-100 recommendation score for waiving a blocked course: GPA-led,
    rewarded by overall pass-rate, penalised by missing prereqs and re-sits.
    -> {gpa, pass_rate, credits_passed, attempts, missing, score, recommendation}."""
    mod = next((m for m in curriculum.get("modules", []) if m["code"] == course_code),
               {"code": course_code, "prereqs": []})
    missing = check_prereqs(mod, tx)["missing"]
    attempted = len(tx.get("attempts", {}))
    passed = len(tx.get("passed_set", set()))
    pass_rate = passed / attempted if attempted else 0.0
    attempts = tx.get("attempts", {}).get(core_code(course_code, tx.get("core_len")), 0)
    gpa = tx.get("gpa", 0.0)
    w_gpa = opts.get("w_gpa", 0.6)
    w_pass = opts.get("w_pass", 0.4)
    miss_pen = opts.get("miss_penalty", 12)
    att_pen = opts.get("attempt_penalty", 5)
    score = w_gpa * gpa + w_pass * (pass_rate * 100) - miss_pen * len(missing) - att_pen * max(0, attempts - 1)
    score = max(0, min(100, round(score)))
    rec = "strong" if score >= 70 else "weak" if score >= 50 else "against"
    return {"gpa": round(gpa, 1), "pass_rate": round(pass_rate * 100), "credits_passed": tx.get("credits_passed", 0),
            "attempts": attempts, "missing": missing, "score": score, "recommendation": rec}


# --- ERS standing -> credit cap (his 56/48/32 table, config-driven) ----------
DEFAULT_ERS_CREDIT_CAPS: dict[str, Any] = {
    "green": None, "orange": 48, "red": 32, "exclude": 0,
    "ERS-ORANGE-FIRSTSEM": 48, "ERS-ORANGE-CUMUL": 48, "ERS-ORANGE-SEM": 56,
    "ERS-RED-FIRST": 32, "ERS-RED-SECOND": 24, "ERS-EXCLUDE": 0,
}


def ers_credit_cap(ers_code: str | None, ers_status: str,
                   caps: dict[str, Any] | None = None) -> int | None:
    """Map an ERS classification to a term credit cap. None = no cap; 0 = excluded.
    Keyed by specific code first, then status. Data-driven -- no if/else."""
    caps = caps or DEFAULT_ERS_CREDIT_CAPS
    if ers_code is not None and ers_code in caps:
        return caps[ers_code]
    return caps.get(ers_status)
