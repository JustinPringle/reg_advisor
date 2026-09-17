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
                     core_len: int = DEFAULT_CORE_LEN,
                     equivalences: list[tuple[str, str]] | None = None) -> dict[str, Any]:
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
    # Credit-weighted average mark: each module's best-attempt mark weighted by
    # its credits. Modules without a numeric mark (e.g. an ungraded pass or an
    # in-progress registration) and 0-credit DP modules carry no weight.
    graded = [b for b in best.values() if b["mark"] is not None and b["credits"]]
    weighted = sum(b["mark"] * b["credits"] for b in graded)
    total_cr = sum(b["credits"] for b in graded)
    gpa = weighted / total_cr if total_cr else 0.0
    # Passed-mark WAM: the same credit-weighted mean over PASSED best-attempts
    # only. DECISION (Justin Pringle, 2026-08-26): the concession gate and the
    # profile "average mark" read this, not the all-attempts gpa above. "WAM >=
    # 55" is meant to say a sound student who slipped on one thing, so a failed
    # sitting should not drag the gate down. Computed here, before the twin
    # aliasing below, so an equivalent pair is never weighted twice.
    passed_graded = [b for b in graded if b["passed"]]
    gpa_passed = (sum(b["mark"] * b["credits"] for b in passed_graded)
                  / sum(b["credits"] for b in passed_graded)) if passed_graded else 0.0
    passed_set = {c for c, b in best.items() if b["passed"]}
    credits_passed = sum(b["credits"] for b in best.values() if b["passed"])
    credits_by_level: dict[int, float] = {}
    for c, b in best.items():
        if b["passed"]:
            lv = code_level(c)
            credits_by_level[lv] = credits_by_level.get(lv, 0.0) + b["credits"]
    # Twin equivalences (programme policy): passing one module of an equivalent
    # pair credits the other. Applied AFTER the gpa and credit totals so a single
    # sitting is never counted twice -- this only widens the lookups (passed_set,
    # best) so completion and prerequisites recognise an articulation student's
    # twin. Symmetric: whichever twin was sat satisfies the requirement.
    for a, b in (equivalences or []):
        a, b = core_code(a, core_len), core_code(b, core_len)
        if a in passed_set and b not in passed_set:
            passed_set.add(b); best.setdefault(b, {**best[a], "code": b})
        elif b in passed_set and a not in passed_set:
            passed_set.add(a); best.setdefault(a, {**best[b], "code": a})
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
            "gpa": gpa, "gpa_passed": gpa_passed, "credits_passed": credits_passed,
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
            mm = term["min_mark"]
            # min_mark is a CARRY threshold: scoring at or above it satisfies the
            # prerequisite even without a full pass (the 40-49 carry the handbook
            # allows). A recorded pass with no mark also counts. A bare code term
            # -- one with no min_mark -- still requires a full pass; author a
            # min_mark to permit a carry.
            if b is None:
                ok = False
            elif b["mark"] is not None:
                ok = b["mark"] >= mm
            else:
                ok = b["passed"]
            return {"met": ok, "soft": False, "label": f"{term['code']}>={mm}",
                    "missing": [] if ok else [term["code"]]}
        ok = _has(tx, term["code"])
        return {"met": ok, "soft": False, "label": term["code"],
                "missing": [] if ok else [term["code"]]}
    return {"met": True, "soft": False, "label": "", "missing": []}


def unmet_count(term: Any, tx: dict[str, Any]) -> int:
    """How many more distinct requirements a prereq term still needs.

    A satisfied term counts zero. An OR counts as one -- any single option
    closes it. An AND sums its unmet parts. This is the honest 'distance to
    eligible', unlike the raw leaf count (which over-counts an OR) or the
    top-level term count (which under-counts an AND-wrapped list)."""
    if eval_term(term, tx)["met"]:
        return 0
    if isinstance(term, list):
        term = {"all": term}
    if isinstance(term, dict):
        if "all" in term:
            return sum(unmet_count(t, tx) for t in term["all"])
        if "any" in term:
            return 1
        if "any_n" in term and "of" in term:
            met = sum(1 for t in term["of"] if eval_term(t, tx)["met"])
            return max(1, term["any_n"] - met)
    return 1


def carry_ok(term: Any, tx: dict[str, Any], floor: float = 45) -> bool:
    """True if an unmet prereq term is a near-miss the student may carry: they
    sat the module and scored above `floor`. A prereq never attempted, or failed
    at or below the floor, is not carry-eligible. An OR is closed by any one
    near-miss option; an AND needs every part to qualify; structural terms
    (year, credits, review) are never a mark carry."""
    if eval_term(term, tx)["met"]:
        return True
    if isinstance(term, list):
        term = {"all": term}
    code = None
    if isinstance(term, dict):
        if "all" in term:
            return all(carry_ok(t, tx, floor) for t in term["all"])
        if "any" in term:
            return any(carry_ok(t, tx, floor) for t in term["any"])
        if "any_n" in term and "of" in term:
            return sum(carry_ok(t, tx, floor) for t in term["of"]) >= term["any_n"]
        code = term.get("code")
    elif isinstance(term, str):
        code = term
    if not code:
        return False
    b = _best(tx, code)
    return bool(b and b.get("mark") is not None and b["mark"] > floor)


def check_prereqs(mod: dict[str, Any], tx: dict[str, Any]) -> dict[str, Any]:
    """AND over a module's prereq list. Soft terms never block.
    -> {met, missing:[code], unmet:[label], soft:[code], n_unmet}."""
    pr = mod.get("prereqs") or []
    if not pr:
        return {"met": True, "missing": [], "unmet": [], "soft": [], "n_unmet": 0}
    paired = [(t, eval_term(t, tx)) for t in pr]
    hard = [(t, r) for t, r in paired if not r["soft"]]
    return {"met": all(r["met"] for _, r in hard),
            "missing": [m for _, r in hard if not r["met"] for m in r["missing"]],
            "unmet": [r["label"] for _, r in hard if not r["met"]],
            "soft": [m for _, r in paired if r["soft"] and r["missing"] for m in r["missing"]],
            "n_unmet": sum(unmet_count(t, tx) for t, r in hard
                           if not str(r["label"]).startswith("[review"))}


# --- Finalist route: can this student complete the degree this year? ---------
# Distinct from a concession. A concession asks "did you nearly pass the
# prerequisite"; the finalist route asks "if you register the capstones now,
# does the degree finish this year". There is no ceiling on how many modules
# remain -- only whether each one can still be cleared, so this is a pure
# feasibility test with no merit floor. FIN-v1.
DEFAULT_FINALIST: dict[str, Any] = {
    "enabled": True,
    "applies_to": [],                 # capstone codes; empty disables the route
    "coregister_sem": 2,              # outstanding modules in this sem run alongside
    "special_exam": {
        "applies_to_sem": 1,          # only sem-1 modules get the later sitting
        "band": [40, 49],             # final mark; below the band is not eligible
        "requires_attempted": True,   # never sat -> cannot be cleared this year
    },
    "rule_id": "FIN-v1",
}

_ELECTIVE_TYPES = ("free_elective", "core_elective", "elective")


def is_practical_requirement(mod: dict[str, Any]) -> bool:
    """A prescribed requirement carrying no credits, captured outside the exam
    record: vacation work, and the workshop and practice courses that sit
    beside it (ENCV1EP, ENCV2MW, ENCV3CW).

    These were read as vacation work only -- the test was the word "vacation"
    in the name -- which left a student whose sole outstanding item was a
    workshop course looking academically incomplete. The registrar does not
    read them that way: it codes such a student DGOR, the same as one waiting
    on vac work. The shape is what matters, not the name: a prescribed,
    zero-credit DP requirement. A module may still declare `vac_work: true`
    explicitly.

    One definition, read by the completion lists and the finalist plan, so the
    two cannot disagree about what blocks a degree.
    """
    if mod.get("vac_work"):
        return True
    return bool(mod.get("is_dp")) and not float(mod.get("credits") or 0)


def completion_plan(curriculum: dict[str, Any], tx: dict[str, Any],
                    registering: set[str] | None = None,
                    rule: dict[str, Any] | None = None) -> dict[str, Any]:
    """Can this student finish the degree this year? The primitive behind both
    the finalist route and the probation load check.

    `registering` is the basket being taken now. Pass None to ask the
    hypothetical question -- "if they registered everything available to them,
    would the degree finish" -- which is what the advice buckets need. Pass an
    explicit set to ask about a real registration, which is what the probation
    check needs: a student who leaves an outstanding module off the basket does
    not complete, however eligible they were to take it.

    An outstanding module clears one of two ways: it is registered now (or
    could be, in the hypothetical), or it is a sem-1 module already attempted
    with a final mark inside the band, cleared at a later sitting. The later
    sitting is NOT part of the registered load -- it is written after the
    semester being assessed.
    -> {completes, registered:[code], later_sitting:[{code, mark}], blocked:[code]}
    """
    r = {**DEFAULT_FINALIST, **(rule or {})}
    se = {**DEFAULT_FINALIST["special_exam"], **(r.get("special_exam") or {})}
    lo, hi = se.get("band", [40, 49])
    taken: list[str] = []
    later: list[dict[str, Any]] = []
    blocked: list[str] = []
    for m in curriculum.get("modules", []):
        if m.get("type") in _ELECTIVE_TYPES or is_practical_requirement(m):
            continue
        b = _best(tx, m["code"])
        if b and b["passed"]:
            continue
        if registering is not None:
            if m["code"] in registering:
                taken.append(m["code"])
                continue
        elif m.get("sem") == r.get("coregister_sem"):
            taken.append(m["code"])
            continue
        if m.get("sem") == se.get("applies_to_sem"):
            mark = (b or {}).get("mark")
            if b is not None and mark is not None and lo <= mark <= hi:
                later.append({"code": m["code"], "mark": mark})
                continue
        blocked.append(m["code"])
    return {"completes": not blocked, "registered": taken,
            "later_sitting": later, "blocked": blocked}


def finalist_route(curriculum: dict[str, Any], tx: dict[str, Any],
                   mod: dict[str, Any],
                   rule: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Whether `mod` (a capstone) may be registered on the finalist route:
    the hypothetical completion question, restricted to the capstone codes."""
    r = {**DEFAULT_FINALIST, **(rule or {})}
    if not r.get("enabled"):
        return None
    targets = set(r.get("applies_to") or [])
    if mod["code"] not in targets:
        return None
    sub = {**curriculum, "modules": [m for m in curriculum.get("modules", [])
                                     if m["code"] not in targets]}
    plan = completion_plan(sub, tx, None, rule)
    if not plan["completes"]:
        return None
    return {"rule_id": r.get("rule_id", "FIN-v1"),
            "coregister": plan["registered"], "later_sitting": plan["later_sitting"]}


# --- The four-bucket advice classifier --------------------------------------
def eval_advice(curriculum: dict[str, Any], tx: dict[str, Any],
                concession_gpa: float | None = None, max_missing: int | None = None,
                concession_floor: float | None = None) -> dict[str, Any]:
    """Classify every prescribed course into four buckets.

    Concession thresholds come from the programme's rules block
    (curriculum["rules"]["concession"]); an explicit argument overrides it, and a
    hard default backs both so the function works on a bare curriculum.

    can_register        : prereqs met, not yet passed
    concession_possible : near-miss -> route to a human (gpa high, <=1 short,
                          failed prereq scored above the floor)
    cannot_register     : blocked
    repeat_needed       : attempted before (overlay flag; a course can be both
                          repeat_needed and one of the three above)
    passed              : already done
    """
    c = (curriculum.get("rules") or {}).get("concession") or {}
    if concession_gpa is None:
        concession_gpa = c.get("min_gpa", 55)
    if max_missing is None:
        max_missing = c.get("max_missing", 1)
    if concession_floor is None:
        concession_floor = c.get("prereq_floor", 45)
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
        # A concession needs two things: one requirement short (n_unmet), AND the
        # failed prereq nearly passed -- scored above the floor. A prereq never
        # attempted or failed well below is not carry-eligible, so it blocks.
        carryable = all(carry_ok(t, tx, concession_floor)
                        for t in (mod.get("prereqs") or [])
                        if not eval_term(t, tx)["soft"])
        if pc["met"]:
            out["can_register"].append(row)
        elif has_review:
            out["needs_review"].append(row)
        elif (fin := finalist_route(curriculum, tx, mod,
                                    (curriculum.get("rules") or {}).get("finalist"))):
            out["concession_possible"].append({**row, "finalist": fin})
        elif (tx.get("gpa_passed", tx.get("gpa", 0)) >= concession_gpa
              and pc["n_unmet"] <= max_missing and carryable):
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
    gpa = tx.get("gpa_passed", tx.get("gpa", 0.0))   # WAM: passed modules only
    w_gpa = opts.get("w_gpa", 0.6)
    w_pass = opts.get("w_pass", 0.4)
    miss_pen = opts.get("miss_penalty", 12)
    att_pen = opts.get("attempt_penalty", 5)
    score = w_gpa * gpa + w_pass * (pass_rate * 100) - miss_pen * len(missing) - att_pen * max(0, attempts - 1)
    score = max(0, min(100, round(score)))
    rec = "strong" if score >= 70 else "weak" if score >= 50 else "against"
    return {"gpa": round(gpa, 1), "pass_rate": round(pass_rate * 100), "credits_passed": tx.get("credits_passed", 0),
            "attempts": attempts, "missing": missing, "score": score, "recommendation": rec}


# --- Probation load: a MINIMUM, not a cap -----------------------------------
# A probation student must REGISTER at least 56 credits. This is the opposite
# instrument to a ceiling, and the two were previously tangled in one table
# (robot_system_logic.md §7 flags the same confusion in last year's code). No
# standing carries a maximum: green, orange and red are all uncapped.
#
# One exception, confirmed by the Programme Coordinator: a student who cannot
# reach 56 but whose registration COMPLETES THE DEGREE -- counting a later
# sitting, which is written after the semester and so is not part of the
# registered load -- has the requirement reduced, on the coordinator's
# signature. A student under 56 who cannot complete is not blocked from
# registering: they register, and the shortfall surfaces as a negative term
# decision at the end of the semester.
DEFAULT_LOAD: dict[str, Any] = {
    "probation_min": 56,
    "probation_statuses": ["red"],
    "completion_reduces": True,
}


def probation_load_check(curriculum: dict[str, Any], tx: dict[str, Any],
                         registering: set[str] | None, ers_status: str,
                         registered_credits: float,
                         rule: dict[str, Any] | None = None) -> dict[str, Any]:
    """Assess a registration basket against the probation minimum.
    -> {applies, verdict, required, registered, reason, plan}

    verdict is one of: "n/a" (not on probation), "meets", "reduced" (short of
    the minimum but the degree completes -- needs the coordinator's signature),
    "short" (does not meet probation; registration proceeds and the term
    decision falls out at the end of the semester).
    """
    r = {**DEFAULT_LOAD, **((curriculum.get("rules") or {}).get("load") or {}), **(rule or {})}
    need = r.get("probation_min", 56)
    if str(ers_status).lower() not in {str(x).lower() for x in r.get("probation_statuses") or []}:
        return {"applies": False, "verdict": "n/a", "required": None,
                "registered": registered_credits, "reason": "not on probation", "plan": None}
    if registered_credits >= need:
        return {"applies": True, "verdict": "meets", "required": need,
                "registered": registered_credits,
                "reason": f"registered {registered_credits:.0f}cr, meets the {need}cr probation minimum",
                "plan": None}
    plan = completion_plan(curriculum, tx, registering,
                           (curriculum.get("rules") or {}).get("finalist"))
    if r.get("completion_reduces") and plan["completes"]:
        later = ", ".join(f"{x['code']} ({x['mark']:.0f})" for x in plan["later_sitting"])
        return {"applies": True, "verdict": "reduced", "required": need,
                "registered": registered_credits, "plan": plan,
                "reason": (f"registered {registered_credits:.0f}cr, under the {need}cr minimum, "
                           f"but this completes the degree"
                           + (f" (later sitting: {later})" if later else "")
                           + " - needs coordinator sign-off")}
    return {"applies": True, "verdict": "short", "required": need,
            "registered": registered_credits, "plan": plan,
            "reason": (f"registered {registered_credits:.0f}cr, under the {need}cr probation "
                       f"minimum and the degree does not complete - registration proceeds, "
                       f"expect a negative term decision")}


# --- ERS standing -> credit cap (DEPRECATED: no standing is capped; see
# DEFAULT_LOAD above. Retained only until every caller moves across.) --------
DEFAULT_ERS_CREDIT_CAPS: dict[str, Any] = {
    "green": None, "orange": None, "red": None, "exclude": 0,
    "ERS-ORANGE-FIRSTSEM": None, "ERS-ORANGE-CUMUL": None, "ERS-ORANGE-SEM": None,
    "ERS-RED-FIRST": None, "ERS-RED-SECOND": None, "ERS-EXCLUDE": 0,
}


def ers_credit_cap(ers_code: str | None, ers_status: str,
                   caps: dict[str, Any] | None = None) -> int | None:
    """Map an ERS classification to a term credit cap. None = no cap; 0 = excluded.
    Keyed by specific code first, then status. Data-driven -- no if/else."""
    caps = caps or DEFAULT_ERS_CREDIT_CAPS
    if ers_code is not None and ers_code in caps:
        return caps[ers_code]
    return caps.get(ers_status)
