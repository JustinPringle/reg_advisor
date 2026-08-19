"""
test_standing_codes.py -- the standing-code map and cohort coverage.

Guards the four bugs fixed in the 2026-08 ERS pass:
  1. unmapped registrar codes must not read as green (fail-safe),
  2. one map shared by badge and checker -- no second hand-kept copy to drift,
  3. the check must cover the active cohort, not only code-bearing students,
  4. an unmapped code routes to review, never clears.

Synthetic data only -- no dependency on the local ERS export.
"""
from __future__ import annotations

import standing_codes as SC
import ers_check as X


def _rows(sn: str = "1", year: str = "2025", block: str = "2",
          code: str = "ENCV3XX", credits: int = 16, grade=72,
          rc: str = "P") -> list[dict]:
    return [{"student_number": sn, "calendar_year": year, "block": block,
             "module_code": code, "credits": credits, "grade": grade,
             "result_code": rc, "result_text": "Pass"}]


def test_status_is_failsafe() -> None:
    assert SC.status_of("FOO") == SC.REVIEW      # unseen code -> review, not green
    assert SC.status_of("") == SC.REVIEW         # blank -> review
    assert SC.status_of("rapb") == "red"         # case-insensitive
    # the readmit / final-probation family is red (robot_system_logic section C)
    for c in ("RAPB", "RDPB", "RASD", "RDSD", "RAAD", "RDAD", "RAFC"):
        assert SC.status_of(c) == "red", c
    # completions are not a risk standing
    for c in ("DC", "DCCL", "DCSL", "DGOR"):
        assert SC.status_of(c) == "green", c
    print("ok test_status_is_failsafe")


def test_risu_is_orange() -> None:
    # RISU: orange current standing (Justin, 2026-08-18); incoming RISU aliases
    # to RSK2 in ers_check, which also resolves orange.
    assert SC.status_of("RISU") == "orange"
    assert X.status_of(X._incoming_alias("RISU")) == "orange"
    print("ok test_risu_is_orange")


def test_programme_override_wins() -> None:
    pol = {"status_of_code": {"SUSP": "exclude"}}   # a programme reclassifies SUSP
    assert SC.status_of("SUSP") == "red"            # default
    assert SC.status_of("SUSP", pol) == "exclude"   # override
    print("ok test_programme_override_wins")


def test_single_source_of_truth() -> None:
    # neither consumer keeps its own copy of the map any more
    assert not hasattr(X, "STATUS_OF_CODE")
    import datasource_sqlite as DS
    assert not hasattr(DS, "STATUS_OF_CODE")
    print("ok test_single_source_of_truth")


def test_check_student_verdicts() -> None:
    assert X.check_student(_rows(), "RAPB")["registrar_status"] == "red"
    assert X.check_student(_rows(), "RAPB")["verdict"] != "review"
    assert X.check_student(_rows(), "")["verdict"] == "engine-only"
    assert X.check_student(_rows(), "COND")["verdict"] == "review"
    print("ok test_check_student_verdicts")


def test_roster_covers_the_cohort() -> None:
    parsed = {
        "students": [{"student_number": "1", "surname": "A", "name": "a"},
                     {"student_number": "2", "surname": "B", "name": "b"}],
        "results": [_rows("1")[0],
                    _rows("2", "2026", "1", "ENCV1XX", 16, 40, "F")[0]],
        "decisions": [{"student_number": "1", "calendar_year": "2026",
                       "semester": 1, "term_code": "RISK", "term_text": ""}],
    }
    assert X.check_parsed(parsed)["summary"]["total"] == 1        # code-bearing only
    rep = X.check_parsed(parsed, roster={"1", "2"})
    assert rep["summary"]["total"] == 2                            # whole cohort
    assert rep["summary"]["engine_only"] == 1                      # student 2, no code
    print("ok test_roster_covers_the_cohort")


def main() -> None:
    test_status_is_failsafe()
    test_risu_is_orange()
    test_programme_override_wins()
    test_single_source_of_truth()
    test_check_student_verdicts()
    test_roster_covers_the_cohort()
    print("\nall standing-code tests pass")


if __name__ == "__main__":
    main()
