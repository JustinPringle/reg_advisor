#!/usr/bin/env python3
"""test_timetable.py -- the timetable clash rule (TTC-v1). Run: python test_timetable.py

The rule: two modules that share a lecture period cannot both be registered;
the student deregisters the higher-level one and takes the lower-level one.
The synthetic tests pin the rule; the real-data tests pin the case that
surfaced it (GEOL215 and ENCV3G2, block F, semester 2) and run only where the
local store and the 2026 timetable are present.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

import regadvisor_engine as R
from advise import check_additions
from programme_loader import load_programme, load_timetable

HERE = Path(__file__).parent
ROOT = HERE.parent
TT = {2: {"GEOL215": {"block": "F", "periods": {"Mon2", "Mon3", "Wed3"}},
          "ENCV3G2": {"block": "F", "periods": {"Mon2", "Mon3", "Wed3"}},
          "MATH141": {"block": "F", "periods": {"Mon2", "Tue7"}},
          "ENCV3MS": {"block": "C", "periods": {"Tue2"}},
          "ENCV3XX": {"block": "C", "periods": {"Tue2"}}}}


def cur_with(modules, timetable=TT, rule=None):
    mods = [{"type": "prescribed", "prereqs": [], "credits": 16, **m} for m in modules]
    return {"programme": {}, "modules": mods, "timetable": timetable,
            "rules": {"timetable": rule or {}}}


def test_lower_level_stays_higher_goes():
    res = R.find_clashes(["ENCV3G2", "GEOL215"], 2, TT)
    assert res["keep"] == ["GEOL215"]
    assert [d["code"] for d in res["drop"]] == ["ENCV3G2"]
    assert res["drop"][0]["with"] == ["GEOL215"] and res["drop"][0]["block"] == "F"
    assert res["drop"][0]["periods"] == ["Mon2", "Mon3", "Wed3"]   # day order


def test_lowest_first_cascade():
    """MATH141 (L1) beats GEOL215 (L2), and both GEOL215 and ENCV3G2 go: each
    shares a period with the kept first-year module."""
    res = R.find_clashes(["ENCV3G2", "GEOL215", "MATH141"], 2, TT)
    assert res["keep"] == ["MATH141"]
    assert sorted(d["code"] for d in res["drop"]) == ["ENCV3G2", "GEOL215"]


def test_same_level_is_a_tie_not_a_drop():
    res = R.find_clashes(["ENCV3MS", "ENCV3XX"], 2, TT)
    assert not res["drop"] and res["ties"][0]["codes"] == ["ENCV3MS", "ENCV3XX"]


def test_unplaced_module_never_dropped():
    res = R.find_clashes(["ENCV3G2", "ZULN101"], 2, TT)
    assert not res["drop"] and not res["ties"]
    assert R.find_clashes(["ENCV3G2", "GEOL215"], 1, TT)["drop"] == []   # wrong semester


def test_advice_moves_the_higher_module():
    cur = cur_with([{"code": "GEOL215", "year": 2, "sem": 2},
                    {"code": "ENCV3G2", "year": 3, "sem": 2}])
    adv = R.eval_advice(cur, R.index_transcript([]))
    assert [m["code"] for m in adv["can_register"]] == ["GEOL215"]
    row = adv["timetable_clash"][0]
    assert row["code"] == "ENCV3G2" and row["clash"]["was"] == "can_register"
    assert "deregister ENCV3G2" in row["clash"]["reason"]


def test_blocked_lower_module_displaces_nothing():
    """A module the student cannot register will not be on the timetable, so
    it does not push the higher one out."""
    cur = cur_with([{"code": "GEOL215", "year": 2, "sem": 2, "prereqs": ["X999"]},
                    {"code": "ENCV3G2", "year": 3, "sem": 2}])
    adv = R.eval_advice(cur, R.index_transcript([]))
    assert [m["code"] for m in adv["can_register"]] == ["ENCV3G2"]
    assert not adv["timetable_clash"]


def test_exempt_and_disabled():
    mods = [{"code": "GEOL215", "year": 2, "sem": 2}, {"code": "ENCV3G2", "year": 3, "sem": 2}]
    adv = R.eval_advice(cur_with(mods, rule={"exempt": ["GEOL215"]}), R.index_transcript([]))
    assert not adv["timetable_clash"] and len(adv["can_register"]) == 2
    adv = R.eval_advice(cur_with(mods, rule={"enabled": False}), R.index_transcript([]))
    assert not adv["timetable_clash"] and len(adv["can_register"]) == 2
    adv = R.eval_advice(cur_with(mods, timetable={}), R.index_transcript([]))
    assert not adv["timetable_clash"]                 # no timetable: advice as before


def test_coc_check_against_existing_registration():
    cur = cur_with([{"code": "GEOL215", "year": 2, "sem": 2},
                    {"code": "ENCV3G2", "year": 3, "sem": 2}])
    # Asking for the higher module while holding the lower: REVIEW.
    chk = check_additions(cur, [], ["ENCV3G2"], registered=["GEOL215"])
    assert chk["rows"][0]["verdict"] == "REVIEW" and "TTC-v1" in chk["rows"][0]["reason"]
    # Asking for the lower module while holding the higher: CLEARED, and the
    # reason tells the admin to undo the existing registration.
    chk = check_additions(cur, [], ["GEOL215"], registered=["ENCV3G2"])
    row = chk["rows"][0]
    assert row["verdict"] == "CLEARED" and "deregister ENCV3G2" in row["reason"]
    assert chk["clashes"][0]["registered"] is True


def test_real_programmes_load_the_timetable():
    if not (ROOT / "catalogue" / "timetable.yaml").exists():
        print("  skip: no catalogue/timetable.yaml"); return
    tt = load_timetable()
    assert tt[2]["GEOL215"]["block"] == "F" and tt[2]["ENCV3G2"]["block"] == "F"
    assert tt[2]["GEOL215"]["periods"] & tt[2]["ENCV3G2"]["periods"]
    for y in ("civil.yaml", "augmented_civil.yaml"):
        cur = load_programme(str(ROOT / "programmes" / y), validate=False)
        assert cur["timetable"] and cur["rules"]["timetable"]["rule_id"] == "TTC-v1"


def test_real_audit_finds_the_geology_cases():
    """The GEOL215 / ENCV3G2 students found by hand: each keeps GEOL215 and
    deregisters ENCV3G2 -- unless they are also repeating MATH141, which is a
    lower level again and takes the slot from both."""
    db = ROOT / "data" / "student_data.db"
    if not db.exists() or not (ROOT / "catalogue" / "timetable.yaml").exists():
        print("  skip: no local store or timetable"); return
    from store import Store
    import checks_service as CHK
    rep = CHK.clash_audit(Store(str(db)), "ENG-CIVIL", "2026")
    if not rep["ready"]:
        print("  skip:", rep["error"]); return
    got = {(r["student_number"], r["keep"], r["deregister"]) for r in rep["rows"]}
    for sn in ("224015924", "224027859", "223084718"):       # Gina, Kubeka, Madikizela
        assert (sn, "GEOL215", "ENCV3G2") in got, sn
    assert ("224093720", "MATH141", "ENCV3G2") in got          # Ntombela: MATH141 too
    assert ("224093720", "MATH141", "GEOL215") in got


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
    print("all timetable tests pass")
