"""
backtest.py -- replay the standing engine at every period the registrar stated.

For each student and each period with a registrar standing (a term code, or a
colour where there is no code), cut the record back to what was known then --
results up to and including that semester, its supps and, for semester 2, the
year-long block -- and ask the ERS check what the engine would have said. The
score is the share of periods where the engine's standing matches the
registrar's.

This is the regression measure for any change to the standing engine: run it
before and after, and a change that moves the number has to explain why.

    python backtest.py                      # every programme in the store
    python backtest.py ENG-CIVIL --since 2023
    python backtest.py --db ../data/student_data.db --misses misses.csv
"""
from __future__ import annotations
from typing import Any
import argparse
import csv
from collections import Counter

import checks_service as CS
import ers_check as X
from standing_codes import SEM_OF_BLOCK
from store import Store


def _sem_key(row: dict[str, Any]) -> tuple[str, int]:
    return (str(row.get("calendar_year") or ""),
            SEM_OF_BLOCK.get(str(row.get("block") or "").strip().upper(), 0))


def backtest(store: Any, programme: str, since: str = "") -> dict[str, Any]:
    """Score the engine against every stated registrar standing."""
    cur, parsed, _ = CS._source(store, programme, "final")
    policy = ((cur or {}).get("rules") or {}).get("ers")
    complete = CS._complete_set(store, programme, cur)
    by_sn = X.group_by_sn(parsed)

    rows_out: list[dict[str, Any]] = []
    for sn, (results, decs, cols) in by_sn.items():
        periods = ({(str(d["calendar_year"]), X._int(d["semester"])) for d in decs}
                   | {(str(c["calendar_year"]), X._int(c["semester"])) for c in cols})
        for p in sorted(periods):
            if p[0] < since:
                continue
            known = [r for r in results if _sem_key(r) <= p]
            if not known:
                continue
            # Completion is known only for the latest period; earlier periods
            # were, by definition, before the student finished.
            done = sn in complete and p == max(periods)
            a = X.assess(known, decs, cols, policy, period=p, degree_complete=done)
            if a["registrar_status"] in ("none", X.REVIEW) or X._period_key(a["period"]) != p:
                continue                      # nothing to score at this period
            rows_out.append({"student_number": sn, "year": p[0], "semester": p[1],
                             "registrar_code": a["registrar_code"],
                             "registrar_status": a["registrar_status"],
                             "engine_code": a["engine_code"],
                             "engine_status": a["engine_status"],
                             "match": a["registrar_status"] == a["engine_status"]})
    n = len(rows_out)
    hits = sum(r["match"] for r in rows_out)
    return {"programme": programme, "periods": n, "match": hits,
            "pct": round(100 * hits / n, 1) if n else 0.0,
            "confusion": Counter((r["registrar_status"], r["engine_status"])
                                 for r in rows_out if not r["match"]),
            "rows": rows_out}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("programmes", nargs="*")
    ap.add_argument("--db", default="../data/student_data.db")
    ap.add_argument("--since", default="", help="first calendar year to score")
    ap.add_argument("--misses", help="write the disagreeing periods to this CSV")
    args = ap.parse_args()
    store = Store(args.db)
    progs = args.programmes or [p["code"] for p in store.programmes()]
    misses: list[dict[str, Any]] = []
    for prog in progs:
        r = backtest(store, prog, args.since)
        print(f"{prog:16} {r['match']}/{r['periods']} periods = {r['pct']}%")
        for (reg, eng), k in r["confusion"].most_common(6):
            print(f"    registrar {reg:8} engine {eng:8} x{k}")
        misses += [dict(x, programme=prog) for x in r["rows"] if not x["match"]]
    if args.misses:
        with open(args.misses, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=["programme", *misses[0]] if misses else ["programme"])
            w.writeheader()
            w.writerows(misses)


if __name__ == "__main__":
    main()
