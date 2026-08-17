"""
completion.py -- who has finished, and who is one step short.

Two lists the coordinator hands to an administrator to capture:

  DG    Degree complete. Every prescribed module in the programme is passed
        (a pass code, or a mark >= 50). The student has met the curriculum.

  DGOR  Degree, only vacation work outstanding. Everything else is passed; the
        single remaining prescribed requirement is practical vacation work,
        done in the vacation and captured later. These students graduate the
        moment the vac-work pass lands, so admin watches them.

Both are pure read-outs of the transcript against the programme's own module
list -- no new thresholds, no judgement. A near-miss on any academic module
keeps a student off both lists.

Vac-work modules are identified from the programme file: a module may carry
`vac_work: true`, and as a safe default any 0-credit DP module whose name
mentions "vacation" is treated as vac work.
"""
from __future__ import annotations
from typing import Any
import csv
from pathlib import Path

import regadvisor_engine as R

_ELECTIVE_TYPES = {"elective", "free_elective", "core_elective"}

# Block -> a within-year order, so the completing semester can be read off the
# latest passing period. Main semesters are 1 and 2; a supplementary sitting
# (S1/S2) is ordered just after its main semester but attributed to it. Block
# "0" (year/annual modules) sorts first.
_BLOCK_ORDER = {"0": 0.0, "1": 1.0, "S1": 1.5, "2": 2.0, "S2": 2.5}


def _passed_row(r: dict[str, Any]) -> bool:
    """A single result row counts as a pass (mirrors the transcript index)."""
    rc = str(r.get("result_code") or "").strip().upper()
    if rc in ("P", "PM"):
        return True
    if rc:
        return False
    mark = r.get("grade", r.get("mark"))
    try:
        return float(mark) >= 50
    except (TypeError, ValueError):
        return False


def _period_key(year: Any, block: Any) -> tuple[int, float]:
    try:
        y = int(str(year).strip())
    except (TypeError, ValueError):
        y = 0
    return (y, _BLOCK_ORDER.get(str(block or "").strip(), 3.0))


def completion_period(rows: list[dict[str, Any]], prescribed_core: set[str],
                      exclude_core: set[str], rule: dict[str, Any],
                      core_len: int | None) -> tuple[int, int, bool] | None:
    """When the degree was completed: (year, semester, via_supplementary).

    A student is complete once every prescribed module is passed AND the
    elective rule is satisfied. Each requirement is dated by the FIRST period it
    was met; completion is the LATEST of those -- so a student whose last act was
    an elective (topping up in a later year) is dated by that elective, not by
    their last prescribed pass. The elective side accrues passes in period order
    and stops at the first period the whole rule holds. Vac-work (or any excluded
    code) is left out, so a DGOR student's academic completion period reports.
    """
    presc_first: dict[str, tuple[int, float]] = {}
    elec_first: dict[str, tuple[tuple[int, float], dict[str, Any]]] = {}
    for r in rows:
        if not _passed_row(r):
            continue
        code = R.core_code(r.get("module_code") or r.get("course_code") or "", core_len)
        if not code:
            continue
        k = _period_key(r.get("calendar_year"), r.get("block"))
        if code in prescribed_core:
            if code in exclude_core:
                continue
            if code not in presc_first or k < presc_first[code]:
                presc_first[code] = k
        else:  # a non-prescribed pass is an elective
            info = _elective_info(code, r.get("credits"))
            if code not in elec_first or k < elec_first[code][0]:
                elec_first[code] = (k, info)

    keys: list[tuple[int, float]] = list(presc_first.values())
    accum: list[dict[str, Any]] = []
    for k, info in sorted(elec_first.values(), key=lambda x: x[0]):
        accum.append(info)
        if electives_status(accum, rule)[0]:
            keys.append(k)
            break
    if not keys:
        return None
    year, order = max(keys)
    sem = 1 if order < 2 else 2
    return (year, sem, order in (1.5, 2.5))


def is_vac_work(mod: dict[str, Any]) -> bool:
    """A module counted as practical vacation work."""
    if mod.get("vac_work"):
        return True
    name = str(mod.get("name") or "").lower()
    credits = mod.get("credits")
    zero = isinstance(credits, (int, float)) and float(credits) == 0
    return bool(mod.get("is_dp") and zero and "vacation" in name)


def prescribed_modules(cur: dict[str, Any]) -> list[dict[str, Any]]:
    """The modules a degree actually requires -- prescribed, not elective slots."""
    return [m for m in cur.get("modules", [])
            if m.get("type", "prescribed") not in _ELECTIVE_TYPES]


# --- Elective rule (data-driven) --------------------------------------------
# The elective requirement, as data so a programme may override it in its
# rules.electives block. Two independent conditions, both required:
#   required          : specific electives that must appear -- here, one Level-4
#                       8-credit School-of-Engineering elective. Each reserves a
#                       matching pass so it cannot also count toward the credits.
#   min_other_credits : further elective credits, any level, any college, ON TOP
#                       of the reserved ones (24). A "school" prefix (EN) marks
#                       the School of Engineering.
DEFAULT_ELECTIVE_RULE: dict[str, Any] = {
    "min_other_credits": 24,
    "required": [
        {"level": 4, "credits": 8, "school": "EN", "count": 1,
         "label": "Level-4 8-credit School of Engineering elective"},
    ],
}


def elective_rule(cur: dict[str, Any]) -> dict[str, Any]:
    r = ((cur.get("rules") or {}).get("electives")) or {}
    return {"min_other_credits": r.get("min_other_credits",
                                       DEFAULT_ELECTIVE_RULE["min_other_credits"]),
            "required": r.get("required", DEFAULT_ELECTIVE_RULE["required"])}


def _elective_info(code: str, credits: Any) -> dict[str, Any]:
    return {"code": code, "level": R.code_level(code), "credits": float(credits or 0)}


def _matches(e: dict[str, Any], req: dict[str, Any]) -> bool:
    if "level" in req and e["level"] != req["level"]:
        return False
    if "credits" in req and e["credits"] != float(req["credits"]):
        return False
    school = req.get("school")
    if school and not str(e["code"]).upper().startswith(str(school).upper()):
        return False
    return True


def electives_status(electives: list[dict[str, Any]], rule: dict[str, Any]
                     ) -> tuple[bool, str]:
    """Check electives against the rule. -> (ok, human note).

    Each `required` spec reserves matching electives; the remaining electives
    must then total at least `min_other_credits`.
    """
    pool = list(electives)
    missing: list[str] = []
    for req in rule.get("required", []):
        matched = [e for e in pool if _matches(e, req)]
        need = int(req.get("count", 1))
        if len(matched) < need:
            missing.append(f"need {req.get('label', 'a specific elective')}")
        for e in matched[:need]:
            pool.remove(e)
    other = sum(e["credits"] for e in pool)
    need_cr = float(rule.get("min_other_credits", 0))
    if other < need_cr:
        missing.append(f"{need_cr - other:.0f}cr short of {need_cr:.0f} further electives")
    ok = not missing
    return ok, ("yes" if ok else "REVIEW: " + "; ".join(missing))


def electives_from_tx(cur: dict[str, Any], tx: dict[str, Any]) -> list[dict[str, Any]]:
    """Passed transcript courses that are not prescribed modules -- the electives."""
    core = tx.get("core_len")
    prescribed = {R.core_code(m["code"], core) for m in prescribed_modules(cur)}
    return [_elective_info(code, b.get("credits"))
            for code, b in tx.get("best", {}).items()
            if b["passed"] and code not in prescribed]


def classify_completion(cur: dict[str, Any], tx: dict[str, Any]) -> dict[str, Any]:
    """Return one student's completion picture: DG, DGOR, or None."""
    vac_codes = {m["code"] for m in prescribed_modules(cur) if is_vac_work(m)}

    outstanding: list[dict[str, Any]] = []
    for m in prescribed_modules(cur):
        if R._has(tx, m["code"]):
            continue
        b = R._best(tx, m["code"])
        outstanding.append({
            "code": m["code"], "name": m.get("name", ""),
            "credits": float(m.get("credits") or 0),
            "is_vac_work": m["code"] in vac_codes,
            "attempted": bool(b),
            "mark": (b["mark"] if b else None)})

    academic_left = [o for o in outstanding if not o["is_vac_work"]]
    vac_left = [o for o in outstanding if o["is_vac_work"]]

    rule = elective_rule(cur)
    electives = electives_from_tx(cur, tx)
    electives_ok, elective_note = electives_status(electives, rule)

    # Completion requires the prescribed modules AND the elective rule: one
    # Level-4 8-credit Engineering elective plus 24 further elective credits.
    # A student short on either is not yet complete -- they fall out of both
    # lists rather than being mis-listed.
    status: str | None = None
    if not academic_left and electives_ok:
        status = "DC" if not vac_left else "DGOR"

    return {"status": status,
            "outstanding": outstanding,
            "academic_outstanding": academic_left,
            "vac_outstanding": vac_left,
            "electives": electives,
            "electives_ok": electives_ok, "elective_note": elective_note}


def _as_engine_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Accept either engine-shaped rows (course_code, mark) or raw store rows
    (module_code, grade). The transcript index reads course_code/mark, so a row
    straight from the database must be mapped or every module reads as un-passed.
    """
    out = []
    for r in rows:
        if r.get("course_code") or r.get("code"):
            out.append(r)
        else:
            out.append({**r, "course_code": r.get("module_code", ""),
                        "mark": r.get("grade")})
    return out


def completion_lists(cur: dict[str, Any],
                     results_by_sn: dict[str, list[dict[str, Any]]],
                     bio: dict[str, dict[str, Any]] | None = None
                     ) -> dict[str, list[dict[str, Any]]]:
    """Run every student and split into the DC and DGOR admin lists."""
    bio = bio or {}
    rule = elective_rule(cur)
    vac_core = {R.core_code(m["code"]) for m in prescribed_modules(cur) if is_vac_work(m)}
    presc_core = {R.core_code(m["code"]) for m in prescribed_modules(cur)}
    dc: list[dict[str, Any]] = []
    dgor: list[dict[str, Any]] = []
    for sn, rows in results_by_sn.items():
        tx = R.index_transcript(_as_engine_rows(rows))
        c = classify_completion(cur, tx)
        if c["status"] is None:
            continue
        b = bio.get(sn, {})
        name = f"{b.get('surname','')}, {b.get('name','')}".strip(", ")
        electives = c["elective_note"]
        # The period the degree (or, for DGOR, the coursework) was completed.
        exclude = vac_core if c["status"] == "DGOR" else set()
        per = completion_period(rows, presc_core, exclude, rule, tx.get("core_len"))
        year, sem, supp = per if per else ("", "", False)
        base = {"student_number": sn, "name": name,
                "plan_code": b.get("plan_code", ""),
                "completed_year": year, "completed_semester": sem,
                "completed": (f"{year} S{sem}" + (" (supp)" if supp else "")) if per else "",
                "gpa": round(tx["gpa"]),
                "credits_passed": round(tx["credits_passed"]),
                "electives_ok": electives}
        if c["status"] == "DC":
            dc.append({**base, "note": "all prescribed modules passed"})
        else:
            vac = "; ".join(f"{o['code']} {o['name']}" for o in c["vac_outstanding"])
            dgor.append({**base, "outstanding": vac,
                         "note": "only vacation work outstanding"})
    # Newest completions first, then by name.
    order = lambda r: (-(r["completed_year"] or 0), -(r["completed_semester"] or 0), r["name"].lower())
    dc.sort(key=order)
    dgor.sort(key=order)
    return {"DC": dc, "DGOR": dgor}


_DG_FIELDS = ["student_number", "name", "plan_code", "completed_year",
              "completed_semester", "completed", "gpa",
              "credits_passed", "electives_ok", "note"]
_DGOR_FIELDS = ["student_number", "name", "plan_code", "completed_year",
                "completed_semester", "completed", "gpa",
                "credits_passed", "electives_ok", "outstanding", "note"]


def export_completion(rows: list[dict[str, Any]], path: str,
                      dgor: bool = False, fields: list[str] | None = None) -> Path:
    """Write one list to CSV for the administrator to capture."""
    if fields is None:
        want_dgor = dgor or bool(rows and "outstanding" in rows[0])
        fields = _DGOR_FIELDS if want_dgor else _DG_FIELDS
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    return out


def main() -> None:
    import sys
    from programme_loader import load_programme
    from data_loaders import load_results
    yaml_path = sys.argv[1] if len(sys.argv) > 1 else "../programmes/civil.yaml"
    csv_path = sys.argv[2] if len(sys.argv) > 2 else "../data/ers_data.csv"
    cur = load_programme(yaml_path)
    results = load_results(csv_path)
    lists = completion_lists(cur, results)
    print(f"{cur['programme']['name']}: "
          f"{len(lists['DC'])} degree-complete (DC), "
          f"{len(lists['DGOR'])} vac-work-only (DGOR)")


if __name__ == "__main__":
    main()
