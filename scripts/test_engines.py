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
    tx = R.index_transcript([{"course_code": "A100", "result_code": "P", "mark": 80}])  # gpa 80
    adv = R.eval_advice(cur, tx)
    assert [m["code"] for m in adv["passed"]] == ["A100"]
    assert [m["code"] for m in adv["can_register"]] == ["B200"]
    assert [m["code"] for m in adv["concession_possible"]] == ["C200"]   # near-miss, gpa high
    assert [m["code"] for m in adv["cannot_register"]] == ["D200"]       # 2 missing
    assert [m["code"] for m in adv["needs_review"]] == ["E200"]          # opaque prereq


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


def test_credit_cap_lookup():
    assert R.ers_credit_cap("ERS-ORANGE-SEM", "orange") == 56
    assert R.ers_credit_cap("ERS-RED-FIRST", "red") == 32
    assert R.ers_credit_cap("ERS-GREEN", "green") is None


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
    print("all passed")
