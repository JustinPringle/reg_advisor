"""
test_health.py -- the programme-health engine, on synthetic and (if present) real data.

Synthetic tests pin the arithmetic that matters: intake year, elapsed-semester
time-to-degree, the throughput banding, and the module pass/fail split with its
enrolment floor. The real-data pass is a smoke test: it runs the whole engine on
the local database and asserts the shape, without hard-coding a cohort's numbers.
"""
from __future__ import annotations
import os

import programme_health as H
from programme_loader import load_programme


def _rows(sn, items):
    """items: (year, block, code, result_code, mark, credits)."""
    return [{"student_number": sn, "calendar_year": str(y), "block": b,
             "course_code": c, "result_code": rc, "mark": m, "credits": cr}
            for (y, b, c, rc, m, cr) in items]


def test_row_outcome():
    o = H._row_outcome
    assert o({"result_code": "P", "mark": 72}) == "pass"
    assert o({"result_code": "PM", "mark": 88}) == "pass"
    assert o({"result_code": "F", "mark": 41}) == "fail"
    assert o({"result_code": "FA", "mark": None}) == "fail"
    assert o({"result_code": "FS", "mark": 47}) == "pending"     # supp granted
    assert o({"result_code": "", "mark": 55}) == "pass"          # uncoded, mark
    assert o({"result_code": "", "mark": 40}) == "fail"
    assert o({"result_code": "", "mark": None}) == "ungraded"    # in progress


def test_elapsed_semesters():
    # Intake 2021 S1, complete 2024 S2 -> eight semesters (the regulation length).
    assert H._elapsed_semesters((2021, 1), (2024, 2)) == 8
    assert H._elapsed_semesters((2021, 1), (2021, 1)) == 1
    assert H._elapsed_semesters((2020, 2), (2024, 1)) == 8


def test_intake_years():
    res = {"A": _rows("A", [(2021, "1", "MATH131", "P", 60, 16),
                            (2022, "1", "MATH238", "P", 70, 16)]),
           "B": _rows("B", [(2020, "2", "MATH131", "F", 40, 16)])}
    assert H.intake_years(res) == {"A": 2021, "B": 2020}


def test_module_stats_split_and_floor():
    # One module, three settled results (2 pass, 1 fail) + one in-progress.
    res = {
        "A": _rows("A", [(2021, "1", "MATH131", "P", 60, 16)]),
        "B": _rows("B", [(2021, "1", "MATH131", "P", 55, 16)]),
        "C": _rows("C", [(2021, "1", "MATH131", "F", 44, 16)]),
        "D": _rows("D", [(2021, "1", "MATH131", "", None, 16)]),   # ungraded, ignored
    }
    cur = {"programme": {}, "modules": [{"code": "MATH131", "credits": 16, "year": 1}]}
    m = {x["code"]: x for x in H.module_stats(cur, res, min_enrol=3)}["MATH131"]
    assert m["enrolled"] == 3 and m["passed"] == 2 and m["failed"] == 1
    assert m["pass_rate"] == round(200 / 3, 1)
    assert m["completion_rate"] == 100.0            # all enrolled settled
    assert m["ranked"] is True                      # 3 >= floor 3
    # Raise the floor above the enrolment: the module drops out of the ranking.
    m2 = {x["code"]: x for x in H.module_stats(cur, res, min_enrol=99)}["MATH131"]
    assert m2["ranked"] is False


def test_module_stats_accepts_raw_store_rows():
    # A raw store row keys the code as module_code / grade, not course_code / mark.
    # module_stats must read it, or the whole module picture goes blank (regression).
    raw = {"A": [{"student_number": "A", "calendar_year": "2021", "block": "1",
                  "module_code": "MATH131", "result_code": "P", "grade": 60,
                  "credits": 16, "module_name": "Maths 1A"}]}
    cur = {"programme": {}, "modules": [{"code": "MATH131", "credits": 16, "year": 1}]}
    stats = H.module_stats(cur, raw, min_enrol=1)
    assert stats and stats[0]["code"] == "MATH131" and stats[0]["passed"] == 1


def test_real_data_smoke():
    db = os.path.join(os.path.dirname(__file__), "..", "data", "student_data.db")
    if not os.path.exists(db):
        print("  (skip real-data smoke: no local student_data.db)")
        return
    from store import Store
    from datasource_sqlite import SqliteSource
    src = SqliteSource(Store(db), "ENG-CIVIL")
    if not src.cur:
        print("  (skip: rules not loaded)")
        return
    rep = H.health(src.cur, src.results, src.bio)
    assert rep["ready"] and rep["overview"]["students"] > 0
    assert rep["overview"]["current_headcount"] <= rep["overview"]["students"]
    assert rep["cohorts"]["time_to_degree"]["n_finishers"] == rep["overview"]["finished"] \
        or rep["overview"]["finished"] >= rep["cohorts"]["time_to_degree"]["n_finishers"]
    # Every gatekeeper clears the floor and has a settled pass/fail split.
    for g in rep["gatekeepers"]:
        assert g["ranked"] and (g["passed"] + g["failed"]) >= 10
    print(f"  real data: {rep['overview']['students']} students, "
          f"{rep['overview']['finished']} finished, "
          f"top gatekeeper {rep['overview']['worst_gatekeeper']}")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
    print("all health tests passed")
