#!/usr/bin/env python3
"""
triage.py -- batch a cycle of applications into two piles.

The picker tool investigates one student. This does the volume work: read the
application queue, clear the routine straight to admin, and route only the hard
cases to an academic. The coordinator stops clicking through students and works
a short exception list instead.

Three lanes:

  register            prereqs met, within the credit cap        -> admin
  concession-auto     a routine concession the academic         -> admin
                      pre-authorised (see CONCESSION_AUTOCLEAR)
  academic            everything else                           -> academic

Auto-clearing a concession is an academic act, so the academic OWNS the rule
below and signs it off once; the tool only applies it. It is deliberately
narrow: a single missing prerequisite that the student CARRIED at 40-49 in that
exact module, a weighted average above the floor, sound standing, within the
cap. A hard fail, an un-attempted prerequisite, two gaps, red standing, or an
over-cap term all fall through to the academic. This is decision D4 -- confirm
the numbers before it goes live.

Run from scripts/:
    python triage.py --demo         # build a queue from the cohort and triage
    python triage.py ../data/applications.csv
Outputs data/admin_worklist.csv and data/academic_queue.csv.
"""
from __future__ import annotations
from typing import Any
import csv
import sys
from datetime import date
from pathlib import Path

import programme_loader
from data_loaders import load_results
from advise import advise_student, check_additions
from regadvisor_engine import core_code, concession_evidence
from datasource import CsvSource

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"

# --- D4: auto-clear rule -- now authored in the programme file --------------
# The academic-owned waiver rule lives in programmes/<name>.yaml under
# rules.autoclear and is read from cur["rules"]["autoclear"]. This dict is only
# the fallback when a programme omits the block. See DEFAULT_RULES in
# programme_loader.py for the field meanings.
CONCESSION_AUTOCLEAR: dict[str, Any] = {
    "enabled": True,
    "min_wam": 55,                 # weighted-average floor to auto-clear
    "carry_band": (46, 49),        # a near-miss carry (>45) in the exact prereq
    "single_miss_only": True,      # only one missing prerequisite
    "allowed_standings": {"green", "orange"},  # never red / exclude
    "rule_id": "CAC-v1",
}


def carry_mark(tx: dict[str, Any], prereq: str,
               band: tuple[int, int]) -> float | None:
    """Return the mark if the student carried this exact prereq at 40-49
    (attempted, sub-minimum), else None (un-attempted or hard fail)."""
    b = tx.get("best", {}).get(core_code(prereq, tx.get("core_len")))
    if b and b.get("mark") is not None and not b["passed"] \
            and band[0] <= b["mark"] <= band[1]:
        return b["mark"]
    return None


def autoclear(mod_code: str, tx: dict[str, Any], missing: list[str],
              ers_status: str, over_cap: bool,
              rule: dict[str, Any] | None = None) -> tuple[bool, str]:
    """Decide whether one concession line auto-clears. Returns (ok, reason)."""
    r = rule or CONCESSION_AUTOCLEAR
    if not r["enabled"]:
        return False, ""
    if ers_status not in r["allowed_standings"]:
        return False, ""
    if over_cap:
        return False, ""
    hard = [m for m in missing if not str(m).startswith("review:")]
    if r["single_miss_only"] and len(hard) != 1:
        return False, ""
    mk = carry_mark(tx, hard[0], tuple(r["carry_band"])) if hard else None
    if mk is None:
        return False, ""
    wam = tx.get("gpa", 0)
    if wam < r["min_wam"]:
        return False, ""
    return True, (f"concession auto-cleared [{r['rule_id']}]: single "
                  f"{int(r['carry_band'][0])}-{int(r['carry_band'][1])} carry "
                  f"in {hard[0]} ({mk:.0f}), WAM {wam:.0f}, {ers_status}, within cap")


# --- triage one student's requested modules ---------------------------------
def triage_student(cur: dict[str, Any], rows: list[dict[str, Any]],
                   codes: list[str], name: str,
                   rule: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    a = advise_student(cur, rows)
    tx, ers = a["tx"], a["ers"]
    chk = check_additions(cur, rows, codes)
    over_cap = a["cap"] is not None and chk["add_credits"] > a["cap"]
    bucket = {m["code"]: b for b in ("can_register", "concession_possible",
                                     "cannot_register", "needs_review")
              for m in a["advice"][b]}
    by_code = {m["code"]: m for m in cur["modules"]}
    verdict = {r["code"]: r for r in chk["rows"]}

    out = []
    for code in codes:
        mod = by_code.get(code, {"code": code, "name": "", "credits": 0})
        base = {"student_number": rows[0]["student_number"], "name": name,
                "module_code": code, "module_name": mod.get("name", ""),
                "credits": mod.get("credits", 0)}
        v = verdict.get(code, {"verdict": "REVIEW", "reason": "not evaluated"})
        b = bucket.get(code)

        if v["verdict"] == "CLEARED":
            out.append({**base, "lane": "register", "action": "register",
                        "reason": "prereqs met, within cap"})
            continue
        if b == "concession_possible":
            missing = next((m["prereq_check"]["missing"]
                            for m in a["advice"]["concession_possible"]
                            if m["code"] == code), [])
            ok, why = autoclear(code, tx, missing, ers["status"], over_cap, rule)
            if ok:
                out.append({**base, "lane": "concession-auto",
                            "action": "register (concession)", "reason": why})
                continue
            ev = concession_evidence(cur, tx, code)
            out.append({**base, "lane": "academic", "action": "decide",
                        "ers_status": ers["status"], "score": ev["score"],
                        "reason": v["reason"]})
            continue
        out.append({**base, "lane": "academic", "action": "decide",
                    "ers_status": ers["status"], "score": "",
                    "reason": v["reason"]})
    return out


def triage_queue(cur, results, apps: dict[str, list[str]],
                 names: dict[str, str], rule=None) -> dict[str, Any]:
    admin, academic = [], []
    for sn, codes in apps.items():
        if sn not in results:
            continue
        for row in triage_student(cur, results[sn], codes, names.get(sn, ""), rule):
            (admin if row["lane"] in ("register", "concession-auto") else academic).append(row)
    n_apps = len(admin) + len(academic)
    return {"admin": admin, "academic": academic,
            "summary": {"students": len(apps), "lines": n_apps,
                        "to_admin": len(admin), "to_academic": len(academic),
                        "auto_concessions": sum(1 for r in admin if r["lane"] == "concession-auto"),
                        "pct_hands_off": round(100 * len(admin) / n_apps) if n_apps else 0}}


# --- demo queue + I/O -------------------------------------------------------
def demo_apps(src: CsvSource, per_student: int = 6) -> tuple[dict, dict]:
    """A plausible cycle: each student applies for what they are eligible or
    near-eligible to take (can_register + concession_possible)."""
    apps, names = {}, {}
    for sn in src.results:
        d = src.get_student(sn)
        codes = [m["code"] for m in d["advice"]["concession_possible"]] \
              + [m["code"] for m in d["advice"]["can_register"]]
        if not codes:
            continue
        apps[sn] = codes[:per_student]
        names[sn] = f"{d['bio'].get('surname','')}, {d['bio'].get('name','')}".strip(", ")
    return apps, names


def read_apps(path: Path) -> dict[str, list[str]]:
    apps: dict[str, list[str]] = {}
    with open(path, newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            sn = (r.get("student_number") or "").strip()
            code = (r.get("module_code") or "").strip()
            if sn and code:
                apps.setdefault(sn, []).append(code)
    return apps


def write_csv(rows, path, fields):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def main() -> None:
    src = CsvSource()
    today = date.today().isoformat()
    rule = (src.cur.get("rules") or {}).get("autoclear") or CONCESSION_AUTOCLEAR

    if "--demo" in sys.argv or len(sys.argv) == 1:
        apps, names = demo_apps(src)
    else:
        apps = read_apps(Path(sys.argv[1]))
        names = {sn: b.get("surname", "") + ", " + b.get("name", "")
                 for sn, b in src.bio.items()}

    r = triage_queue(src.cur, src.results, apps, names, rule)
    for row in r["admin"]:
        row["decided"] = today

    write_csv(r["admin"], DATA / "admin_worklist.csv",
              ["student_number", "name", "module_code", "module_name",
               "credits", "lane", "action", "reason", "decided"])
    write_csv(r["academic"], DATA / "academic_queue.csv",
              ["student_number", "name", "module_code", "module_name",
               "ers_status", "score", "action", "reason"])

    s = r["summary"]
    print(f"queue: {s['lines']} lines from {s['students']} students")
    print(f"  -> admin    : {s['to_admin']:4}  ({s['pct_hands_off']}% hands-off, "
          f"of which {s['auto_concessions']} auto-cleared concessions)")
    print(f"  -> academic : {s['to_academic']:4}")
    print("wrote data/admin_worklist.csv and data/academic_queue.csv")


if __name__ == "__main__":
    main()
