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

    def period_load(p: str) -> tuple[float, float]:
        total = passed = 0.0
        for r in by_period[p]:
            c = float(r.get("credits") or 0)
            total += c
            if r.get("passed"):
                passed += c
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

    cum_pct = (cum_passed / cum_total) if cum_total else 0.0
    sem_pct = (sem_passed / sem_total) if sem_total else 0.0

    return {
        "cumulative": {"credit_pct_passed": cum_pct,
                       "credits_expected_to_date": cum_total,
                       "credits_passed_to_date": cum_passed},
        "semester": {"credit_pct_passed": sem_pct, "credits_total": sem_total,
                     "credits_passed": sem_passed, "period": current or ""},
        "history": {"below_minimum": cum_pct < min_pct,
                    "semesters_registered": len(main_periods),
                    "last_status": history.get("last_status", "none"),
                    "appeals_exhausted": bool(history.get("appeals_exhausted", False))},
        "periods": order,
    }
