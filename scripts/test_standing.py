#!/usr/bin/env python3
"""Regression tests for the one standing the ERS check and the advisor share.

Guards the 2026-09-24 defects, where the advisor disagreed with the check on
71 of 320 Civil students:
  - the advisor classified without the programme's rules.ers, so the engine
    judged minimum progression by its built-in ratio instead of the table;
  - its history was the newest COLOUR period, so a newer code (an exclusion,
    which carries no colour row) was never seen;
  - its prior was the judged period's own standing -- the answer, fed back in.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

import ers_check as X
from advise import advise_student
from programme_loader import load_programme

HERE = Path(__file__).parent
CIVIL = load_programme(str(HERE.parent / "programmes" / "civil.yaml"))
POLICY = CIVIL["rules"]["ers"]


def res(year, block, code, credits, rc):
    return {"student_number": "1", "calendar_year": year, "block": block,
            "module_code": code, "credits": credits,
            "grade": 60 if rc == "P" else 40, "result_code": rc}


def dec(year, sem, code):
    return {"student_number": "1", "calendar_year": year, "semester": sem,
            "term_code": code, "term_text": ""}


def col(year, sem, colour):
    return {"student_number": "1", "calendar_year": year, "semester": sem,
            "colour": colour, "colour_text": ""}


# Two semesters: 88 of 120 credits passed. 73% on rate clears the built-in
# 48/72 ratio; 88 credits misses the table's 96 at semester 2.
ROWS = [res("2025", "1", c, cr, rc) for c, cr, rc in
        (("A1", 16, "P"), ("A2", 16, "P"), ("A3", 16, "P"), ("A4", 8, "P"), ("A5", 8, "F"))] + \
       [res("2025", "2", c, cr, rc) for c, cr, rc in
        (("B1", 16, "P"), ("B2", 8, "P"), ("B3", 8, "P"), ("B4", 8, "F"), ("B5", 16, "F"))]


def test_advice_reads_the_programme_policy():
    a = advise_student(CIVIL, X._shape_rows(ROWS), history={"last_status": "red"})
    assert a["ers"]["code"] == "ERS-RED-SECOND", a["ers"]["code"]


def test_prior_is_the_period_before_the_judged_one():
    a = X.assess(ROWS, [dec("2025", 2, "FPRR")],
                 [col("2025", 1, "green"), col("2025", 2, "red")], POLICY)
    assert (a["year"], a["semester"]) == ("2025", 2)
    assert a["prior_status"] == "green"            # 2025:1, not 2025:2's red
    assert a["registrar_status"] == "red"
    assert a["engine_code"] == "ERS-RED-FIRST"


def test_a_newer_code_beats_an_older_colour():
    """An exclusion carries no colour row; it must still govern."""
    s = X.standing(ROWS + [res("2026", "1", "C1", 16, "F")],
                   [dec("2025", 2, "RAPB"), dec("2026", 1, "XNFA")],
                   [col("2025", 1, "red"), col("2025", 2, "red")], POLICY)
    assert (s["source"], s["status"], s["code"]) == ("registrar", "exclude", "XNFA"), s


def test_the_engine_governs_only_where_the_registrar_is_silent():
    """Results out, ERS not yet run: the engine judges, from the last stated prior."""
    s = X.standing(ROWS, [], [col("2025", 1, "green")], POLICY)
    assert s["source"] == "engine" and s["period"] == "2025:2"
    assert s["check"]["prior_status"] == "green"
    assert s["status"] == s["check"]["engine_status"]


def test_real_data_advisor_equals_the_check():
    """On the captured store, the advisor's cross-check is the check's row."""
    db = HERE.parent / "data" / "student_data.db"
    if not db.exists():
        print("  (skipped: no data/student_data.db)")
        return
    from store import Store
    import checks_service as CS
    from datasource_sqlite import SqliteSource
    st = Store(str(db))
    for p in st.programmes():
        rows = {r["student_number"]: r for r in CS.ers_check(st, p["code"])["rows"]}
        src = SqliteSource(st, p["code"])
        for sn in src.current_students():
            if sn in rows:
                got = src.standing(sn)["check"]
                assert got["engine_code"] == rows[sn]["engine_code"], (p["code"], sn)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all standing tests pass")
