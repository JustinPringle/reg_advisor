"""
ers_engine.py -- academic-standing / progression classifier.

A Python port of AutoScholar's ers.engine.js, adapted to UKZN ERS data and
generalised so any programme drives it from a policy dict rather than
hard-coded rules.

The design is deliberately data-driven, following the source:
  * CRITERIA is an ordered list of rules; the first whose conditions all hold
    wins (most-stringent first).
  * classify() is a generic evaluator over CRITERIA -- no if/else ladder.
  * derive_metrics() maps raw result rows to the metrics the criteria read.

Nothing here is programme-specific: thresholds live in `policy`, so a second
programme (or institution) supplies its own cut points and passing codes.

Pure module: no I/O, no pandas. Feed it plain dicts; unit-test it directly.
"""
from __future__ import annotations
from typing import Any, Callable

# --- Default policy (UKZN Engineering). Override per programme. -------------
DEFAULT_POLICY: dict[str, Any] = {
    "pass_mark": 50,
    # A course counts as passed if its result code is in pass_codes, or (no
    # code) its mark >= pass_mark. UKZN: P and PM pass; F/FS/DE/FA do not.
    "pass_codes": {"P", "PM"},
    "cumulative_good": 0.75,   # >= this cumulative pass-rate is good standing
    "semester_good": 0.70,     # >= this current-semester pass-rate is good
    # Below this cumulative pass-rate the student is "below minimum" (the gate
    # that puts them on probation). UKZN Engineering = 48/72 of normal load.
    "min_progression_pct": 48 / 72,
}

def _below_minimum(cum_passed: float, cum_pct: float, semesters: int,
                   policy: dict[str, Any], min_pct: float) -> bool:
    """Below the progression floor. Uses the absolute min-progression credit for
    the student's semester when a table is authored; otherwise the ratio."""
    th = thresholds_for(semesters, policy)
    if th and th.get("min_prog") is not None:
        return float(cum_passed) < float(th["min_prog"])
    return cum_pct < min_pct


def thresholds_for(semesters: int, policy: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """The credit lines for a given count of completed semesters, or None when
    the programme authored no table. The lookup clamps into range, so a student
    past the last tabulated semester carries the final row's lines.

    The normal-load and 75% columns run out before the minimum-progression one,
    because cumulative expectation caps at the degree total while the minimum
    keeps climbing. A blank there means "no further expectation", not "no
    expectation": the last stated line is carried forward, so a student in a
    fifth year is measured against a full degree rather than against nothing."""
    table = ((policy or {}).get("progression")) or {}
    keys = sorted(int(k) for k in table)
    if not keys:
        return None
    s = min(max(int(semesters or 0), keys[0]), keys[-1])
    row = table.get(s, table.get(str(s)))
    mn, nm, p75 = (list(row) + [None, None, None])[:3]
    for k in reversed([k for k in keys if k <= s]):
        prev = (list(table.get(k, table.get(str(k)))) + [None, None, None])[:3]
        nm = nm if nm is not None else prev[1]
        p75 = p75 if p75 is not None else prev[2]
        if nm is not None and p75 is not None:
            break
    return {"sem": s, "min_prog": mn, "normal": nm, "p75": p75}

def normal_load_for(semesters: int, policy: dict[str, Any] | None = None) -> float | None:
    """The normal load for ONE semester: the step between two rows of the
    cumulative normal-load column.

    A full-time semester is the same size however many semesters a student has
    taken, so past the end of the table -- where the cumulative column stops,
    because cumulative expectation caps at the degree total -- the last step
    still applies. Falling back to it keeps a 5th-year student measured against
    a full load instead of against nothing. None only when the programme
    tabulates no normal load at all.
    """
    table = ((policy or {}).get("progression")) or {}
    rows = {}
    for k in table:
        row = list(table[k]) + [None, None, None]
        if row[1] is not None:
            rows[int(k)] = float(row[1])
    if not rows:
        return None
    keys = sorted(rows)
    s_i = min(max(int(semesters or 0), keys[0]), keys[-1])
    while s_i not in rows and s_i > keys[0]:
        s_i -= 1
    return rows[s_i] - rows.get(s_i - 1, 0.0)


def build_criteria(policy: dict[str, Any]) -> list[dict[str, Any]]:
    """The ERS decision list as data, thresholds injected from `policy`.

    Mirrors the ProcessFlow ERS-CLASSIFY tree one criterion at a time. Each
    criterion = ALL of its (path, op, value) rules. First match wins.
    """
    policy = {**DEFAULT_POLICY, **(policy or {})}
    cum = policy["cumulative_good"]
    sem = policy["semester_good"]
    return [
        {"code": "ERS-EXCLUDE", "status": "exclude",
         "label": "Exclude - appeals exhausted",
         "rules": [("history.appeals_exhausted", "eq", True)]},
        {"code": "ERS-RED-SECOND", "status": "red",
         "label": "Severely underperforming - 2nd time (appeal)",
         "rules": [("history.below_minimum", "eq", True),
                   ("history.last_status", "eq", "red"),
                   ("history.appeals_exhausted", "eq", False)]},
        {"code": "ERS-RED-FIRST", "status": "red",
         "label": "Severely underperforming - 1st time",
         "rules": [("history.below_minimum", "eq", True),
                   ("history.semesters_registered", "gte", 2),
                   ("history.last_status", "neq", "red")]},
        {"code": "ERS-ORANGE-FIRSTSEM", "status": "orange",
         "label": "At risk - first-semester probation",
         "rules": [("history.below_minimum", "eq", True),
                   ("history.semesters_registered", "lte", 1)]},
        {"code": "ERS-ORANGE-CUMUL", "status": "orange",
         "label": f"At risk - cumulative below {int(cum*100)}%",
         "rules": [("cumulative.credit_pct_passed", "lt", cum),
                   ("history.below_minimum", "eq", False)]},
        {"code": "ERS-ORANGE-SEM", "status": "orange",
         "label": f"At risk - current semester below {int(sem*100)}%",
         "rules": [("semester.credit_pct_passed", "lt", sem),
                   ("cumulative.credit_pct_passed", "gte", cum),
                   ("history.below_minimum", "eq", False)]},
        # Short on LOAD rather than on rate. A student who registers 40 credits
        # of a 72-credit semester and passes all 40 reads 100% on rate and is
        # still half a semester behind; the registrar calls that at risk.
        {"code": "ERS-ORANGE-LOAD", "status": "orange",
         "label": "At risk - passed less than a full semester's load",
         "rules": [("semester.below_load", "eq", True),
                   ("history.below_minimum", "eq", False)]},
        # Rehabilitation, not a fresh start. A student carrying an orange or red
        # standing does not return to green on one good semester: the spec's
        # trees B and C keep them there until the CUMULATIVE credit line is
        # cleared (75% of normal load to date). Tree C has no green leaf at all,
        # so a prior red resolves orange until a person moves it.
        {"code": "ERS-ORANGE-CARRY", "status": "orange",
         "label": "At risk - carried forward, cumulative not yet recovered",
         "rules": [("history.last_status", "eq", "orange"),
                   ("history.rehabilitated", "eq", False),
                   ("history.below_minimum", "eq", False)]},
        {"code": "ERS-ORANGE-FROMRED", "status": "orange",
         "label": "At risk - returning from probation",
         "rules": [("history.last_status", "eq", "red"),
                   ("history.below_minimum", "eq", False)]},
        {"code": "ERS-GREEN", "status": "green",
         "label": "Good academic standing",
         "rules": [("cumulative.credit_pct_passed", "gte", cum),
                   ("semester.credit_pct_passed", "gte", sem),
                   ("history.below_minimum", "eq", False)]},
    ]


# The decision tree as node/edge data -- the visual companion to the criteria,
# for a progression-map render (kept faithful to the source FLOW).
FLOW: dict[str, Any] = {
    "nodes": [
        {"id": "submit", "label": "Semester results captured", "kind": "action"},
        {"id": "classify", "label": "Classify student", "kind": "decision"},
        {"id": "green", "label": "Green - continue", "kind": "green"},
        {"id": "orange", "label": "Orange - academic probation", "kind": "orange"},
        {"id": "redStrict", "label": "Red - strict probation", "kind": "red"},
        {"id": "ceacom", "label": "CEACOM appeal", "kind": "review"},
        {"id": "finalProb", "label": "Final probation", "kind": "red"},
        {"id": "aeacom", "label": "AEACOM appeal", "kind": "review"},
        {"id": "excluded", "label": "Excluded", "kind": "exclude"},
    ],
    "edges": [
        {"from": "submit", "to": "classify", "label": "always"},
        {"from": "classify", "to": "excluded", "criterion": "ERS-EXCLUDE"},
        {"from": "classify", "to": "ceacom", "criterion": "ERS-RED-SECOND"},
        {"from": "classify", "to": "redStrict", "criterion": "ERS-RED-FIRST"},
        {"from": "classify", "to": "orange", "criterion": "ERS-ORANGE-FIRSTSEM"},
        {"from": "classify", "to": "orange", "criterion": "ERS-ORANGE-CUMUL"},
        {"from": "classify", "to": "orange", "criterion": "ERS-ORANGE-SEM"},
        {"from": "classify", "to": "orange", "criterion": "ERS-ORANGE-LOAD"},
        {"from": "classify", "to": "green", "criterion": "ERS-GREEN"},
        {"from": "ceacom", "to": "finalProb", "label": "approved"},
        {"from": "ceacom", "to": "aeacom", "label": "rejected"},
        {"from": "aeacom", "to": "finalProb", "label": "approved"},
        {"from": "aeacom", "to": "excluded", "label": "rejected"},
    ],
}


# --- Generic evaluator ------------------------------------------------------
def _get(obj: Any, path: str) -> Any:
    cur = obj
    for key in path.split("."):
        if cur is None:
            return None
        cur = cur.get(key) if isinstance(cur, dict) else getattr(cur, key, None)
    return cur


_OPS: dict[str, Callable[[Any, Any], bool]] = {
    "eq": lambda a, b: a == b,
    "neq": lambda a, b: a != b,
    "lt": lambda a, b: float(a) < float(b),
    "lte": lambda a, b: float(a) <= float(b),
    "gt": lambda a, b: float(a) > float(b),
    "gte": lambda a, b: float(a) >= float(b),
}


def classify(metrics: dict[str, Any],
             criteria: list[dict[str, Any]] | None = None,
             policy: dict[str, Any] | None = None) -> dict[str, Any]:
    """metrics -> {status, code, label, reasons[]}. First matching criterion wins."""
    if criteria is None:
        criteria = build_criteria(policy or DEFAULT_POLICY)
    for crit in criteria:
        reasons = []
        all_met = True
        for path, op, val in crit["rules"]:
            actual = _get(metrics, path)
            try:
                ok = _OPS[op](actual, val)
            except (TypeError, ValueError):
                ok = False
            reasons.append(f"{'PASS' if ok else 'FAIL'} {path} {op} {val} (={actual!r})")
            all_met = all_met and ok
        if all_met:
            return {"status": crit["status"], "code": crit["code"],
                    "label": crit["label"], "reasons": reasons}
    return {"status": "unknown", "code": None,
            "label": "Unclassified", "reasons": ["no criterion matched"]}


def explain(metrics: dict[str, Any],
            criteria: list[dict[str, Any]] | None = None,
            policy: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """The path through the rules, not just the verdict.

    Every criterion in order with each of its rules marked pass or fail, up to
    and including the one that fired. Same walk as classify(), reported rather
    than reduced, so a standing can be audited by reading it.
    """
    if criteria is None:
        criteria = build_criteria(policy or DEFAULT_POLICY)
    out = []
    for crit in criteria:
        rules = []
        for path, op, val in crit["rules"]:
            actual = _get(metrics, path)
            try:
                ok = _OPS[op](actual, val)
            except (TypeError, ValueError):
                ok = False
            rules.append({"path": path, "op": op, "value": val,
                          "actual": actual, "ok": ok})
        matched = all(r["ok"] for r in rules)
        out.append({"code": crit["code"], "status": crit["status"],
                    "label": crit["label"], "matched": matched, "rules": rules})
        if matched:
            break
    return out


# --- Institution mapper: raw result rows -> decision metrics ----------------
def derive_metrics(results: list[dict[str, Any]],
                   policy: dict[str, Any] | None = None,
                   history: dict[str, Any] | None = None) -> dict[str, Any]:
    """Roll up per-course result rows into the metrics `classify` reads.

    results: rows of {period, course_code, credits, mark, passed}. `period` is
             an ordered key (e.g. "2020:2"); `passed` is a bool the caller sets
             from the institution's pass rule.
    policy:  cut points + min_progression_pct.
    history: {last_status, appeals_exhausted} carried from the prior term
             (these live in the ERS PDF, not the results CSV -- see robot notes).

    Cumulative figures dedupe by course (best attempt wins), so a supplementary
    re-sit does not double-count credits -- a small, deliberate improvement on
    the source, which summed raw per-period. The current-semester figure uses
    the latest *main* period only.
    """
    # policy = policy or DEFAULT_POLICY
    policy = {**DEFAULT_POLICY, **(policy or {})}
    history = history or {}
    min_pct = policy["min_progression_pct"]

    # A row is "assessed" once it carries a result code or a mark. A registered
    # but not-yet-graded module (blank/blank) is neither passed nor failed, so
    # it must not drag the pass-rate down -- ERS runs on assessed results only.
    def assessed(r: dict[str, Any]) -> bool:
        return bool(str(r.get("result_code") or "").strip()) or r.get("mark") is not None

    rows = [r for r in (results or []) if assessed(r)]

    # Group assessed rows by period.
    by_period: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_period.setdefault(str(r.get("period", "")), []).append(r)
    order = sorted(by_period)

    def is_main(p: str) -> bool:  # main registration = block 1 or 2
        return p.split(":")[-1] in ("1", "2")

    main_periods = [p for p in order if is_main(p)]
    current = main_periods[-1] if main_periods else (order[-1] if order else None)

    def supp_of(p: str) -> list[str]:
        """The supplementary/deferred blocks that settle a main period.

        The ERS prints two totals for every period -- "Block 1" and "Block 1
        Main&Supp" -- and the decision is made on the second. A module failed in
        the main block and passed in the supp has been passed for that semester,
        so the supp blocks are read with their own semester: S1 with semester 1,
        S2/S3/S4 with semester 2.
        """
        year, _, sem = str(p).rpartition(":")
        blocks = ["S1"] if sem == "1" else ["S2", "S3", "S4"]
        return [f"{year}:{b}" for b in blocks if f"{year}:{b}" in by_period]

    def period_load(p: str) -> tuple[float, float]:
        """(registered, passed) credits for a period, supps included. Deduped by
        course so a module sat twice in one semester counts once."""
        attempts: dict[str, dict[str, Any]] = {}
        for q in [p] + supp_of(p):
            for r in by_period[q]:
                code = str(r.get("course_code") or "")
                prev = attempts.get(code)
                if prev is None or (r.get("passed") and not prev.get("passed")):
                    attempts[code] = r
        total = sum(float(r.get("credits") or 0) for r in attempts.values())
        passed = sum(float(r.get("credits") or 0) for r in attempts.values() if r.get("passed"))
        return total, passed

    sem_total, sem_passed = period_load(current) if current else (0.0, 0.0)

    # Cumulative: dedupe by course, best assessed attempt wins.
    best: dict[str, dict[str, Any]] = {}
    for r in rows:
        code = str(r.get("course_code") or "")
        if not code:
            continue
        prev = best.get(code)
        better = (prev is None
                  or (r.get("passed") and not prev.get("passed"))
                  or (r.get("passed") == prev.get("passed")
                      and float(r.get("mark") or 0) > float(prev.get("mark") or 0)))
        if better:
            best[code] = r
    cum_total = sum(float(r.get("credits") or 0) for r in best.values())
    cum_passed = sum(float(r.get("credits") or 0) for r in best.values() if r.get("passed"))

    # The 75%-of-normal cumulative line for this many completed semesters. The
    # table stops carrying a normal load past the standard degree length, and a
    # student that far out is behind by definition -- so a missing line means
    # NOT rehabilitated, never rehabilitated-by-default. Only the carry-forward
    # criteria read this, so it cannot hold a good-standing student back.
    _th = thresholds_for(len(main_periods), policy)
    _p75 = (_th or {}).get("p75")
    at_or_above_p75 = _p75 is not None and cum_passed >= float(_p75)

    # The other half of the rehabilitation test: credits passed this semester
    # against 70% of ONE semester's normal load. Not the same thing as the
    # semester pass RATE -- a student who registers 48 of 72 credits and passes
    # every one reads 100% on rate and is still short on load.
    _load = normal_load_for(len(main_periods), policy)
    _load_line = (policy["semester_good"] * _load) if _load is not None else None
    at_or_above_load = _load_line is not None and sem_passed >= _load_line
    # The same comparison read the other way round, and the difference matters
    # when the load is UNKNOWN (no normal-load row that far out). Rehabilitation
    # needs positive proof, so at_or_above_load is False when we cannot tell; a
    # clear pass must not be withheld on a missing row, so below_load is False
    # then too. Unknown is never held against the student in either direction.
    below_load = _load_line is not None and sem_passed < _load_line

    cum_pct = (cum_passed / cum_total) if cum_total else 0.0
    sem_pct = (sem_passed / sem_total) if sem_total else 0.0

    return {
        "cumulative": {"credit_pct_passed": cum_pct,
                       "credits_expected_to_date": cum_total,
                       "credits_passed_to_date": cum_passed,
                       "at_or_above_p75": at_or_above_p75},
        "semester": {"credit_pct_passed": sem_pct, "credits_total": sem_total,
                     "credits_passed": sem_passed, "period": current or "",
                     "at_or_above_load": at_or_above_load,
                     "below_load": below_load},
        "history": {"below_minimum": _below_minimum(cum_passed, cum_pct,
                                                    len(main_periods), policy, min_pct),
                    "semesters_registered": len(main_periods),
                    "semesters_completed": len(main_periods),
                    # Rehabilitated = both halves of the spec's tree-B test:
                    # cumulative back on the 75% line AND a full load passed this
                    # semester. Either half short and an orange standing stands.
                    "rehabilitated": at_or_above_p75 and at_or_above_load,
                    "last_status": history.get("last_status", "none"),
                    "appeals_exhausted": bool(history.get("appeals_exhausted", False))},
        "thresholds": thresholds_for(len(main_periods), policy),
        "periods": order,
    }
