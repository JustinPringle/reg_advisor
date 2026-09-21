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
    # RISU: orange current standing (Justin, 2026-08-18); a returning RISU
    # student re-enters on RISK (Justin, 2026-09-18), also orange.
    assert SC.status_of("RISU") == "orange"
    assert X._incoming_alias("RISU") == "RISK"
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


_POL = {"progression": {1: [48, 72, 54], 2: [96, 144, 108]}}


def test_orange_is_carried_until_cumulative_recovers() -> None:
    """A light load passed in full looks perfect on pass-rate alone, but leaves
    the student behind on credits. Trees B and C rehabilitate only once the
    cumulative line (75% of normal) is cleared -- the bug behind the 48
    'engine more lenient' mismatches."""
    # Two semesters, everything registered passed, but only 96 credits of the
    # 144 normal: at or above the 96 minimum, under the 108 three-quarter line.
    behind = [_rows("1", "2025", "1", "MOD1", 40, 60, "P")[0],
              _rows("1", "2025", "2", "MOD2", 56, 60, "P")[0]]
    assert X.check_student(behind, "GREEN", None, _POL)["engine_status"] == "green"
    assert X.check_student(behind, "RSK2", "RSK2", _POL)["engine_status"] == "orange"
    assert X.check_student(behind, "PROB", "PROB", _POL)["engine_status"] == "orange"
    # Back on the cumulative line, but this semester's load is short: 40 credits
    # passed against 50.4 (70% of one 72-credit semester). Rehabilitation needs
    # BOTH halves, so the orange stands -- and a light load is at risk on its own
    # account even with no history, which a pass RATE of 100% would miss.
    light = [_rows("1", "2025", "1", "MOD1", 72, 60, "P")[0],
             _rows("1", "2025", "2", "MOD2", 40, 60, "P")[0]]          # 112 cumulative
    assert X.check_student(light, "GREEN", None, _POL)["engine_code"] == "ERS-ORANGE-LOAD"
    assert X.check_student(light, "RSK2", "RSK2", _POL)["engine_status"] == "orange"
    # Clear both lines and the orange student is rehabilitated.
    recovered = behind + [_rows("1", "2025", "2", "MOD3", 32, 60, "P")[0]]  # 128, load 88
    assert X.check_student(recovered, "RSK2", "RSK2", _POL)["engine_status"] == "green"
    print("ok test_orange_is_carried_until_cumulative_recovers")


def test_colour_stands_in_for_a_missing_code() -> None:
    """A period in good standing carries no term code, only a colour. Without
    reading it the student is unclassifiable; with it the check can be made."""
    rows = _rows("1", "2026", "1", "MOD1", 72, 60, "P")
    assert X.check_student(rows, "")["verdict"] == "engine-only"
    checked = X.check_student(rows, "", registrar_colour="green")
    assert checked["registrar_source"] == "colour"
    assert checked["registrar_status"] == "green"
    assert checked["verdict"] == "match"
    # A code, when there is one, still wins over the colour.
    both = X.check_student(rows, "RISK", registrar_colour="green")
    assert both["registrar_source"] == "code" and both["registrar_status"] == "orange"
    # The prior period's colour is the history input when it carried no code.
    assert X.check_student(rows, "", prior_colour="orange",
                           registrar_colour="orange")["engine_status"] == "orange"
    print("ok test_colour_stands_in_for_a_missing_code")


def test_supp_counts_towards_its_semester() -> None:
    """The ERS decides on the "Main&Supp" total: a module failed in the main
    block and passed in the supp has been passed for that semester."""
    main = (_rows("1", "2026", "1", "MOD1", 40, 60, "P")
            + _rows("1", "2026", "1", "MOD2", 32, 45, "FS"))
    supp = _rows("1", "2026", "S1", "MOD2", 32, 52, "P")
    assert X.check_student(main, "")["semester_pct"] == 56           # main block only
    assert X.check_student(main + supp, "")["semester_pct"] == 100   # supp settles it
    print("ok test_supp_counts_towards_its_semester")


def test_annual_block_settles_with_semester_two() -> None:
    """Block 0 holds the augmented year-long modules; they count in semester 2,
    where the augmented progression table puts their credits."""
    sem2 = _rows("1", "2026", "2", "ENAG160", 8, 60, "P")
    annual = _rows("1", "2026", "0", "MATH160", 16, 45, "F")
    assert X.check_student(sem2, "")["semester_pct"] == 100
    assert X.check_student(sem2 + annual, "")["semester_pct"] == 33   # 8 of 24
    print("ok test_annual_block_settles_with_semester_two")


def test_blank_credits_filled_from_programme() -> None:
    """A credit the ERS left blank is taken from the programme; a printed one
    is never replaced."""
    from programme_loader import fill_missing_credits
    cur = {"modules": [{"code": "ENCH160", "credits": 8},
                       {"code": "MATH160", "credits": 16}],
           "catalogue": {"MATH160": {"credits": 32}}}
    rows = (_rows(code="ENCH160", credits=None)
            + _rows(code="MATH160", credits=None)
            + _rows(code="ENCV3XX", credits=16))
    got = [r["credits"] for r in fill_missing_credits(rows, cur)]
    assert got == [8, 16, 16], got         # programme value beats catalogue
    assert fill_missing_credits(rows, None) is rows
    print("ok test_blank_credits_filled_from_programme")


def main() -> None:
    test_status_is_failsafe()
    test_risu_is_orange()
    test_programme_override_wins()
    test_single_source_of_truth()
    test_check_student_verdicts()
    test_roster_covers_the_cohort()
    test_orange_is_carried_until_cumulative_recovers()
    test_colour_stands_in_for_a_missing_code()
    test_supp_counts_towards_its_semester()
    test_annual_block_settles_with_semester_two()
    test_blank_credits_filled_from_programme()
    print("\nall standing-code tests pass")


if __name__ == "__main__":
    main()
