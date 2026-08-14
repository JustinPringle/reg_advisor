"""
advise.py -- ties the two engines to UKZN data.

Three entry points:

  advise_student(cur, rows)          full picture for one student: ERS standing,
                                     credit cap, four advice buckets, per-blocked
                                     reasons.

  check_additions(cur, rows, codes)  the COC check: for the modules a student
                                     asks to add, stamp each CLEARED or REVIEW
                                     with a reason -- academics decide, the tool
                                     clears the compliant majority.

  sweep_programme(cur, results)      whole-cohort ERS distribution + how many
                                     applications would auto-clear (the backtest).

Run:  python advise.py                 -> demo on the bundled Civil data
      python advise.py 218028575       -> report for one student
"""
from __future__ import annotations
from typing import Any
import sys
from collections import Counter

import ers_engine as E
import regadvisor_engine as R
from data_loaders import load_curriculum, load_results

XLSX = "/mnt/project/Course_insert_sheet_with_prereqs.xlsx"
CSV = "/mnt/project/ers_data.csv"


def advise_student(cur: dict[str, Any], rows: list[dict[str, Any]],
                   policy: dict[str, Any] | None = None,
                   history: dict[str, Any] | None = None) -> dict[str, Any]:
    tx = R.index_transcript(rows)
    metrics = E.derive_metrics(rows, policy, history)
    ers = E.classify(metrics, policy=policy)
    cap = R.ers_credit_cap(ers["code"], ers["status"])
    advice = R.eval_advice(cur, tx)
    return {"tx": tx, "metrics": metrics, "ers": ers, "cap": cap, "advice": advice}


def check_additions(cur: dict[str, Any], rows: list[dict[str, Any]],
                    codes: list[str], policy: dict[str, Any] | None = None) -> dict[str, Any]:
    """Stamp each requested module CLEARED or REVIEW with a reason.

    CLEARED only when: it is a catalogued prescribed module, prereqs are met,
    it is not already passed, and the term stays within the ERS credit cap.
    Everything else fails safe toward a human.
    """
    a = advise_student(cur, rows, policy)
    tx, advice, cap = a["tx"], a["advice"], a["cap"]
    by_code = {m["code"]: m for m in cur["modules"]}
    # Map each code to (bucket, advice-row) -- the row carries prereq_check.
    row_of: dict[str, tuple[str, dict]] = {}
    for name in ("can_register", "concession_possible", "cannot_register", "needs_review"):
        for m in advice[name]:
            row_of[m["code"]] = (name, m)
    add_credits = sum(float(by_code[c]["credits"]) for c in codes if c in by_code)
    over_cap = cap is not None and add_credits > cap

    out = []
    for code in codes:
        mod = by_code.get(code)
        if mod is None:
            out.append({"code": code, "verdict": "REVIEW", "reason": "uncatalogued module (elective / other discipline)"})
            continue
        if mod.get("type") == "elective":
            out.append({"code": code, "verdict": "REVIEW", "reason": "elective slot - needs coordinator sign-off"})
            continue
        if R._has(tx, code):
            out.append({"code": code, "verdict": "REVIEW", "reason": "already passed"})
            continue
        bucket, arow = row_of.get(code, ("cannot_register", None))
        if bucket == "can_register" and over_cap:
            out.append({"code": code, "verdict": "REVIEW",
                        "reason": f"prereqs met but term load {add_credits:.0f}cr exceeds {cap}cr cap ({a['ers']['code']})"})
        elif bucket == "can_register":
            out.append({"code": code, "verdict": "CLEARED", "reason": "prereqs met, within credit cap"})
        elif bucket == "concession_possible":
            ev = R.concession_evidence(cur, tx, code)
            out.append({"code": code, "verdict": "REVIEW",
                        "reason": f"near-miss - concession may apply (score {ev['score']}/100 {ev['recommendation']}; missing {', '.join(ev['missing']) or '-'})"})
        elif bucket == "needs_review":
            note = "; ".join(mod.get("review_notes") or ["opaque prerequisite"])
            out.append({"code": code, "verdict": "REVIEW", "reason": f"prerequisite needs human check: {note}"})
        else:
            miss = ", ".join(arow["prereq_check"]["unmet"]) if arow else "prerequisites"
            out.append({"code": code, "verdict": "REVIEW", "reason": f"blocked - missing {miss}"})
    return {"ers": a["ers"], "cap": cap, "add_credits": add_credits, "rows": out}


def sweep_programme(cur: dict[str, Any], results: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    dist: Counter = Counter()
    capped = 0
    per_student = []
    for sn, rows in results.items():
        a = advise_student(cur, rows)
        dist[a["ers"]["status"]] += 1
        if a["cap"] is not None:
            capped += 1
        adv = a["advice"]
        per_student.append({"sn": sn, "status": a["ers"]["status"], "cap": a["cap"],
                            "clear": len(adv["can_register"]), "concession": len(adv["concession_possible"]),
                            "blocked": len(adv["cannot_register"]), "review": len(adv["needs_review"])})
    return {"n": len(results), "distribution": dict(dist), "capped": capped, "per_student": per_student}


# --- text report ------------------------------------------------------------
def format_report(sn: str, a: dict[str, Any]) -> str:
    tx, m, ers, cap, adv = a["tx"], a["metrics"], a["ers"], a["cap"], a["advice"]
    L = [f"Student {sn}",
         f"  passed {len(tx['passed_set'])} courses | GPA {tx['gpa']:.0f} | {tx['credits_passed']:.0f} credits"
         f" | year {tx['year_of_study']} | {tx['semesters_registered']} semesters",
         f"  ERS standing: {ers['status'].upper()}  [{ers['code']}]  {ers['label']}",
         f"    cumulative {m['cumulative']['credit_pct_passed']*100:.0f}%  ·"
         f"  latest semester {m['semester']['credit_pct_passed']*100:.0f}% ({m['semester']['period']})",
         f"    credit cap this term: {cap if cap is not None else 'no cap'}",
         f"  Can register ({len(adv['can_register'])}): {' '.join(x['code'] for x in adv['can_register']) or '-'}",
         f"  Concession possible ({len(adv['concession_possible'])}): {' '.join(x['code'] for x in adv['concession_possible']) or '-'}",
         f"  Blocked ({len(adv['cannot_register'])}): {' '.join(x['code'] for x in adv['cannot_register']) or '-'}",
         f"  Needs review ({len(adv['needs_review'])}): {' '.join(x['code'] for x in adv['needs_review']) or '-'}"]
    for x in adv["cannot_register"][:6]:
        L.append(f"      {x['code']} <- missing {', '.join(x['prereq_check']['unmet'])}")
    return "\n".join(L)


def main() -> None:
    cur = load_curriculum(XLSX, programme_code="ENG-CIVIL", programme_name="Civil Engineering")
    results = load_results(CSV)

    if len(sys.argv) > 1:
        sn = sys.argv[1]
        if sn not in results:
            print(f"student {sn} not found"); return
        print(format_report(sn, advise_student(cur, results[sn])))
        return

    print(f"Curriculum: {cur['programme']['name']} "
          f"({sum(1 for m in cur['modules'] if m['type']=='prescribed')} prescribed modules, "
          f"{cur['programme']['total_credits']:.0f} credits)\n")

    sn = "218028575"
    print(format_report(sn, advise_student(cur, results[sn])))

    print("\n--- COC check: student 226051631 asks to add these modules ---")
    chk = check_additions(cur, results["226051631"], ["ENCV2SA", "MATH238", "ENCV3ST", "LING202"])
    print(f"  ERS {chk['ers']['code']}  cap {chk['cap']}  requested {chk['add_credits']:.0f}cr")
    for row in chk["rows"]:
        print(f"    {row['verdict']:8} {row['code']:8} {row['reason']}")

    print("\n--- Programme sweep (backtest over the whole cohort) ---")
    sw = sweep_programme(cur, results)
    print(f"  {sw['n']} students | ERS {sw['distribution']} | {sw['capped']} carry a credit cap")


if __name__ == "__main__":
    main()
