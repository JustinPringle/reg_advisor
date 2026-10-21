#!/usr/bin/env python3
"""clash_audit.py -- list students whose current registration clashes (TTC-v1).

Reads the local store. For every student with two modules in progress that meet
at the same time, prints the lower-level module they keep and the higher-level
one they must deregister. --csv writes the same rows to data/ for the admin.

Run from scripts/:
    python clash_audit.py --programme ENG-CIVIL
    python clash_audit.py --programme ENG-CIVIL-AUG --csv
"""
from __future__ import annotations
import argparse
import csv
from datetime import date
from pathlib import Path

from store import Store
import checks_service as CHK

ROOT = Path(__file__).resolve().parents[1]
FIELDS = ["student_number", "surname", "name", "year_of_study", "year", "semester",
          "keep", "deregister", "block", "periods", "action", "rule_id"]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--programme", required=True)
    ap.add_argument("--year", default="", help="calendar year (default: latest on record)")
    ap.add_argument("--db", default=str(ROOT / "data" / "student_data.db"))
    ap.add_argument("--csv", action="store_true", help="also write data/clashes_<prog>_<date>.csv")
    a = ap.parse_args()
    rep = CHK.clash_audit(Store(a.db), a.programme, a.year)
    if not rep["ready"]:
        print(rep["error"]); return
    s = rep["summary"]
    print(f"{a.programme} {rep['year']}: {s['students']} students, {s['lines']} modules to deregister")
    for k, n in sorted(s["pairs"].items(), key=lambda kv: -kv[1]):
        print(f"  {n:3}  {k}")
    print()
    for r in rep["rows"]:
        print(f"  {r['student_number']}  {r['surname'] + ', ' + r['name']:<32} S{r['semester']}  "
              f"keep {r['keep']:<8} {r['action']} {r['deregister']:<8} block {r['block']}")
    if a.csv:
        out = ROOT / "data" / f"clashes_{a.programme}_{date.today().isoformat()}.csv"
        with open(out, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=FIELDS, extrasaction="ignore")
            w.writeheader(); w.writerows(rep["rows"])
        print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
