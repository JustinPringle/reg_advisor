"""
test_engines.py -- fast checks on the pure engines. Run: python test_engines.py

These prove the ported behaviour without any UKZN data, which is the point of
keeping the engines pure: a second programme is just different dicts.
"""
import ers_engine as E
import regadvisor_engine as R


def test_core_code_collapses_sittings():
    assert R.core_code("ENCV2SAH1") == "ENCV2SA"
    assert R.core_code("ENCV2SAHS1") == "ENCV2SA"     # supplementary -> same core
    assert R.core_code("ELEC_Y4S2A") == "ELEC_Y4S2A"  # synthetic slot left whole
    assert R.core_code("MATH132") == "MATH132"         # short real code unchanged


def test_prereq_grammar():
    tx = R.index_transcript([
        {"course_code": "MATH131", "result_code": "P", "mark": 65},
        {"course_code": "PHYS151", "result_code": "P", "mark": 40},
    ])
    assert R.eval_term("MATH131", tx)["met"]
    assert not R.eval_term("MATH999", tx)["met"]
    assert R.eval_term({"code": "PHYS151", "min_mark": 40}, tx)["met"]
    assert not R.eval_term({"code": "PHYS151", "min_mark": 50}, tx)["met"]   # 40 < 50
    assert R.eval_term({"any": ["MATH131", "MATH999"]}, tx)["met"]           # OR
    assert not R.eval_term({"all": ["MATH131", "MATH999"]}, tx)["met"]       # AND
    assert R.eval_term({"review": "opaque"}, tx)["missing"] == ["review:opaque"]


def test_four_buckets():
    cur = {"programme": {}, "modules": [
        {"code": "A100", "credits": 8, "type": "prescribed", "prereqs": []},
        {"code": "B200", "credits": 8, "type": "prescribed", "prereqs": ["A100"]},          # met
        {"code": "C200", "credits": 8, "type": "prescribed", "prereqs": ["A100", "X999"]},  # 1 missing
        {"code": "D200", "credits": 8, "type": "prescribed", "prereqs": ["X998", "X999"]},  # 2 missing
        {"code": "E200", "credits": 8, "type": "prescribed", "prereqs": [{"review": "opaque"}]},
    ]}
    # X999 carried at 47 -- attempted and above the concession floor, which is
    # what makes C200 a near-miss rather than a block. (Before carry_ok existed
    # this fixture omitted X999 entirely and the test asserted the old rule.)
    tx = R.index_transcript([{"course_code": "A100", "result_code": "P", "mark": 80, "credits": 8},
                             {"course_code": "X999", "result_code": "F", "mark": 47, "credits": 8}])
    adv = R.eval_advice(cur, tx)
    assert [m["code"] for m in adv["passed"]] == ["A100"]
    assert [m["code"] for m in adv["can_register"]] == ["B200"]
    assert [m["code"] for m in adv["concession_possible"]] == ["C200"]   # near-miss, gpa high
    assert [m["code"] for m in adv["cannot_register"]] == ["D200"]       # 2 missing
    assert [m["code"] for m in adv["needs_review"]] == ["E200"]          # opaque prereq



def _finalist_cur():
    """Two capstones in sem 2, plus one lower-level module in each semester."""
    return {"programme": {}, "rules": {"finalist": {
                "enabled": True, "applies_to": ["CAP1", "CAP2"],
                "coregister_sem": 2,
                "special_exam": {"applies_to_sem": 1, "band": [40, 49],
                                 "requires_attempted": True},
                "rule_id": "FIN-v1"}},
            "modules": [
                {"code": "L1S1", "credits": 16, "type": "prescribed", "year": 2, "sem": 1, "prereqs": []},
                {"code": "L2S2", "credits": 16, "type": "prescribed", "year": 2, "sem": 2, "prereqs": []},
                {"code": "CAP1", "credits": 24, "type": "prescribed", "year": 4, "sem": 2,
                 "prereqs": [{"all": ["L1S1", "L2S2"]}]},
                {"code": "CAP2", "credits": 24, "type": "prescribed", "year": 4, "sem": 2,
                 "prereqs": [{"all": ["L1S1", "L2S2"]}]}]}


def _bucket(adv, code):
    for name, rows in adv.items():
        if name != "repeat_needed" and any(m["code"] == code for m in rows):
            return name
    return None


def test_finalist_coregisters_outstanding_sem2():
    """Sem-2 module outstanding: it runs alongside the capstones, so the degree
    still finishes this year. Two requirements short, which the concession rule
    would refuse -- the finalist route has no ceiling."""
    cur = _finalist_cur()
    tx = R.index_transcript([{"course_code": "L1S1", "result_code": "P", "mark": 60, "credits": 16}])
    adv = R.eval_advice(cur, tx)
    assert _bucket(adv, "CAP1") == "concession_possible"
    fin = next(m for m in adv["concession_possible"] if m["code"] == "CAP1")["finalist"]
    assert fin["coregister"] == ["L2S2"] and fin["later_sitting"] == []


def test_finalist_later_sitting_inside_band():
    """Sem-1 module failed at 44: inside the 40-49 band, so it can be cleared at
    a later sitting. No merit floor -- WAM here is well under 55."""
    cur = _finalist_cur()
    tx = R.index_transcript([{"course_code": "L1S1", "result_code": "F", "mark": 44, "credits": 16},
                             {"course_code": "L2S2", "result_code": "P", "mark": 52, "credits": 16}])
    assert tx["gpa_passed"] < 55
    adv = R.eval_advice(cur, tx)
    assert _bucket(adv, "CAP1") == "concession_possible"
    assert _bucket(adv, "CAP2") == "concession_possible"
    fin = next(m for m in adv["concession_possible"] if m["code"] == "CAP1")["finalist"]
    assert fin["later_sitting"] == [{"code": "L1S1", "mark": 44}]


def test_finalist_below_band_blocks():
    """Sem-1 module failed at 38: below the band, no later sitting, so the
    degree cannot finish this year."""
    cur = _finalist_cur()
    tx = R.index_transcript([{"course_code": "L1S1", "result_code": "F", "mark": 38, "credits": 16},
                             {"course_code": "L2S2", "result_code": "P", "mark": 80, "credits": 16}])
    adv = R.eval_advice(cur, tx)
    assert _bucket(adv, "CAP1") == "cannot_register"


def test_finalist_never_attempted_blocks():
    """Sem-1 module never sat cannot be cleared this year, however strong the
    rest of the record."""
    cur = _finalist_cur()
    tx = R.index_transcript([{"course_code": "L2S2", "result_code": "P", "mark": 85, "credits": 16}])
    adv = R.eval_advice(cur, tx)
    assert _bucket(adv, "CAP1") == "cannot_register"


def test_finalist_disabled_falls_back_to_concession_rule():
    """With the route off, the capstones are judged as ordinary modules."""
    cur = _finalist_cur()
    cur["rules"]["finalist"]["enabled"] = False
    tx = R.index_transcript([{"course_code": "L1S1", "result_code": "F", "mark": 44, "credits": 16},
                             {"course_code": "L2S2", "result_code": "P", "mark": 52, "credits": 16}])
    adv = R.eval_advice(cur, tx)
    assert _bucket(adv, "CAP1") == "cannot_register"   # 44 is below the 45 floor


def test_wam_is_passed_modules_only():
    """The concession gate, the evidence score and the auto-clear rule must all
    read the same average: passed modules, credit-weighted."""
    cur = {"programme": {}, "modules": [
        {"code": "C200", "credits": 8, "type": "prescribed",
         "prereqs": [{"code": "X999", "min_mark": 50}]}]}
    tx = R.index_transcript([{"course_code": "A100", "result_code": "P", "mark": 56, "credits": 8},
                             {"course_code": "X999", "result_code": "F", "mark": 47, "credits": 8}])
    assert round(tx["gpa_passed"]) == 56 and round(tx["gpa"]) != 56
    adv = R.eval_advice(cur, tx)
    assert [m["code"] for m in adv["concession_possible"]] == ["C200"]
    assert R.concession_evidence(cur, tx, "C200")["gpa"] == 56.0
    import triage as T
    ok, why = T.autoclear("C200", tx, ["X999"], "green", False)
    assert ok and "WAM 56" in why


def test_probation_minimum_not_a_cap():
    """56 is a floor for a red student and nothing is a ceiling."""
    cur = _finalist_cur()
    cur["rules"]["load"] = {"probation_min": 56, "probation_statuses": ["red"],
                            "completion_reduces": True}
    tx = R.index_transcript([{"course_code": "L1S1", "result_code": "P", "mark": 60, "credits": 16}])
    # Green student: the rule does not apply at all, at any load.
    assert R.probation_load_check(cur, tx, {"CAP1"}, "green", 24)["verdict"] == "n/a"
    # Red, 56 or more: meets.
    assert R.probation_load_check(cur, tx, {"CAP1", "CAP2", "L2S2"}, "red", 64)["verdict"] == "meets"


def test_probation_reduced_when_registration_completes_degree():
    """Under 56 but the basket finishes the degree -> reduced, on PC sign-off.
    The later sitting is not counted in the registered load."""
    cur = _finalist_cur()
    tx = R.index_transcript([{"course_code": "L1S1", "result_code": "F", "mark": 44, "credits": 16},
                             {"course_code": "L2S2", "result_code": "P", "mark": 52, "credits": 16}])
    chk = R.probation_load_check(cur, tx, {"CAP1", "CAP2"}, "red", 48)
    assert chk["verdict"] == "reduced"
    assert chk["plan"]["later_sitting"] == [{"code": "L1S1", "mark": 44}]
    assert "sign-off" in chk["reason"]


def test_probation_short_is_a_warning_not_a_block():
    """Under 56 and the degree does not complete: registration still proceeds;
    the shortfall surfaces as a term decision at the end of the semester."""
    cur = _finalist_cur()
    tx = R.index_transcript([{"course_code": "L1S1", "result_code": "F", "mark": 30, "credits": 16}])
    chk = R.probation_load_check(cur, tx, {"CAP1"}, "red", 24)
    assert chk["verdict"] == "short" and "negative term decision" in chk["reason"]


def test_registration_basket_must_actually_include_the_work():
    """Leaving an outstanding module off the basket means no completion, so no
    reduction -- eligibility to take it is not the same as taking it."""
    cur = _finalist_cur()
    tx = R.index_transcript([{"course_code": "L1S1", "result_code": "P", "mark": 60, "credits": 16}])
    assert R.completion_plan(cur, tx, {"CAP1", "CAP2", "L2S2"})["completes"]
    assert not R.completion_plan(cur, tx, {"CAP1", "CAP2"})["completes"]


def test_ers_classify_is_data_driven():
    # Any programme: pass a custom policy, thresholds move, no code change.
    # Two semesters, 50% cumulative pass-rate.
    def row(p, c, mk, rc, ok):
        return {"period": p, "course_code": c, "credits": 16, "mark": mk,
                "result_code": rc, "passed": ok}
    rows = [row("2024:1", "A", 60, "P", True), row("2024:1", "B", 30, "F", False),
            row("2024:2", "C", 60, "P", True), row("2024:2", "D", 30, "F", False)]
    # One policy flows into BOTH derive_metrics and classify.
    low = {**E.DEFAULT_POLICY, "min_progression_pct": 0.4}
    high = {**E.DEFAULT_POLICY, "min_progression_pct": 0.6}
    m_low, m_high = E.derive_metrics(rows, low), E.derive_metrics(rows, high)
    assert abs(m_low["cumulative"]["credit_pct_passed"] - 0.5) < 1e-9
    assert m_low["history"]["semesters_registered"] == 2
    assert E.classify(m_low, policy=low)["status"] == "orange"   # 50% >= 40% floor
    assert E.classify(m_high, policy=high)["status"] == "red"    # 50% < 60% floor


def test_no_standing_carries_a_credit_cap():
    """The old table read 56/48/32 as ceilings. Confirmed with the Programme
    Coordinator: no standing is capped, and the 56 is the probation REGISTRATION
    MINIMUM (see test_probation_minimum_not_a_cap). Every lookup is now None;
    the function survives only until its callers move to rules.load."""
    for code, status in (("ERS-ORANGE-SEM", "orange"), ("ERS-RED-FIRST", "red"),
                         ("ERS-GREEN", "green")):
        assert R.ers_credit_cap(code, status) is None


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
    print("all passed")
