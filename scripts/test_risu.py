"""
test_risu.py -- the School's RISU guide (RISU-v1) in the engine and the check.

Point 2 of the guide: a first-year with a final MATH131 mark below 40 who also
failed another first-semester prerequisite module is advised to suspend
semester 2 (RISU). Point 1: failing every module is counselled, not coded.

Synthetic data only, apart from reading the Civil rule file.
"""
from __future__ import annotations
from pathlib import Path

import ers_check as X
from programme_loader import load_programme

CIVIL = load_programme(str(Path(__file__).resolve().parent.parent
                           / "programmes" / "civil.yaml"))
POL = CIVIL["rules"]["ers"]


def _sem1(marks: dict[str, tuple], year: str = "2026", block: str = "1") -> list[dict]:
    """{code: (mark, result_code)} -> result rows, 16 credits each."""
    return [{"student_number": "1", "calendar_year": year, "block": block,
             "module_code": c, "credits": 16, "grade": m, "result_code": rc}
            for c, (m, rc) in marks.items()]


PASSES = {"CHEM181": (60, "P"), "ENCH1TC": (60, "P"), "ENME1DR": (60, "P"),
          "MATH132": (60, "P"), "PHYS151": (60, "P")}


def _code(rows: list[dict], reg: str = "RISK", pol=POL) -> dict:
    return X.check_student(rows, reg, None, pol)


def test_rule_fires_on_the_guide_case() -> None:
    rows = _sem1({**PASSES, "MATH131": (36, "F"), "PHYS151": (35, "F")})
    assert _code(rows)["engine_code"] == "ERS-ORANGE-RISU"
    print("ok test_rule_fires_on_the_guide_case")


def test_rule_needs_both_halves() -> None:
    gate_only = _sem1({**PASSES, "MATH131": (36, "F")})
    gate_40 = _sem1({**PASSES, "MATH131": (40, "F"), "PHYS151": (35, "F")})
    assert _code(gate_only)["engine_code"] != "ERS-ORANGE-RISU"
    assert _code(gate_40)["engine_code"] != "ERS-ORANGE-RISU"     # 40 is not below 40
    print("ok test_rule_needs_both_halves")


def test_fail_without_mark_is_below_gate_deferred_is_not_a_result() -> None:
    fa = _sem1({**PASSES, "MATH131": (None, "FA"), "MATH132": (35, "F")})
    de = _sem1({**PASSES, "MATH131": (30, "F"), "MATH132": (None, "DE")})
    assert _code(fa)["engine_code"] == "ERS-ORANGE-RISU"
    assert _code(de)["engine_code"] != "ERS-ORANGE-RISU"
    print("ok test_fail_without_mark_is_below_gate_deferred_is_not_a_result")


def test_supp_result_replaces_main() -> None:
    rows = (_sem1({**PASSES, "MATH131": (36, "F"), "PHYS151": (46, "FS")})
            + _sem1({"PHYS151": (55, "P")}, block="S1"))
    assert _code(rows)["engine_code"] != "ERS-ORANGE-RISU"          # passed the supp
    print("ok test_supp_result_replaces_main")


def test_first_semester_only() -> None:
    rows = (_sem1(PASSES, year="2025", block="2")
            + _sem1({**PASSES, "MATH131": (36, "F"), "PHYS151": (35, "F")}))
    assert _code(rows)["engine_code"] != "ERS-ORANGE-RISU"
    print("ok test_first_semester_only")


def test_code_level_mismatch_both_ways() -> None:
    risu = _sem1({**PASSES, "MATH131": (36, "F"), "PHYS151": (35, "F")})
    risk = _sem1({**PASSES, "MATH131": (45, "F"), "PHYS151": (35, "F")})
    a, b = _code(risu, "RISK"), _code(risk, "RISU")
    assert (a["verdict"], a["direction"]) == ("mismatch", "engine stricter")
    assert (b["verdict"], b["direction"]) == ("mismatch", "engine more lenient")
    assert _code(risu, "RISU")["verdict"] == "match"
    # A programme without the rule neither emits RISU nor flags a registrar RISU.
    bare = {k: v for k, v in POL.items() if k != "risu"}
    assert _code(risu, "RISU", bare)["verdict"] == "match"
    print("ok test_code_level_mismatch_both_ways")


def test_failed_all_is_counselled() -> None:
    allf = _sem1({c: (45, "F") for c in [*PASSES, "MATH131"]})
    assert _code(allf)["counsel"] is True
    assert _code(_sem1({**PASSES, "MATH131": (36, "F")}))["counsel"] is False
    print("ok test_failed_all_is_counselled")


def test_list_matches_the_catalogue() -> None:
    """with_any_failed must be exactly the Y1S1 modules that gate a Y1S2 module."""
    def codes(term):
        if isinstance(term, str):
            yield term
        elif isinstance(term, dict):
            if "code" in term:
                yield term["code"]
            for k in ("all", "any", "of"):
                for t in term.get(k) or []:
                    yield from codes(t)
    s1 = {m["code"] for m in CIVIL["modules"] if m.get("year") == 1 and m.get("sem") == 1}
    gating = {c for m in CIVIL["modules"] if m.get("year") == 1 and m.get("sem") == 2
              for t in m.get("prereqs") or [] for c in codes(t)} & s1
    gate = POL["risu"]["gate"]["code"]
    assert set(POL["risu"]["with_any_failed"]) == gating - {gate}, gating
    print("ok test_list_matches_the_catalogue")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
    print("\nall RISU tests pass")
