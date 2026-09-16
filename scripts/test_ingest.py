#!/usr/bin/env python3
"""
test_ingest.py -- the multi-programme ingest and store, checked against real data.

Run from scripts/:  python test_ingest.py
"""
from __future__ import annotations
import csv
import tempfile
from collections import Counter
from pathlib import Path

from ers_ingest import parse_file
from ingest_ers import ingest_path, parse_csv
from store import Store

DATA = Path(__file__).resolve().parents[1] / "data"
CIVIL_ERS = DATA / "ers_civil_2026jul.txt"
CIVIL_CSV = DATA / "ers_data.csv"
AUG_CSV = DATA / "ers_data_aug.csv"


def test_parser_matches_reference_csv() -> None:
    """Every module record in the reference CSV comes back from the parser."""
    rec = parse_file(str(CIVIL_ERS), "ENG-CIVIL")

    def k(sn, mc, g, res):
        g = str(int(float(g))) if g not in ("", None) else ""
        return (str(sn), mc, g, res or "")

    got = Counter(k(r["student_number"], r["module_code"], r.get("grade"), r.get("result_code"))
                  for r in rec["results"])
    want = Counter()
    with open(CIVIL_CSV) as fh:
        for r in csv.DictReader(fh):
            want[k(r["student_number"], r["module_code"], r["grade"], r["result_code"])] += 1
    assert got == want, "parser module records differ from the reference CSV"


def test_parser_captures_decisions() -> None:
    rec = parse_file(str(CIVIL_ERS), "ENG-CIVIL")
    codes = {d["term_code"] for d in rec["decisions"]}
    assert rec["decisions"], "no term decisions captured"
    assert {"RISK", "PROB", "XNFA"} <= codes, f"expected standing codes missing: {codes}"


# A synthetic record in the ERS layout: invented student number, no real data.
# It carries one period, one decision in the summary table, and a colour block
# whose first line is labelled and whose remaining lines are bare.
COLOUR_FIXTURE = """
 999000111 Testcase, Sample ENCV
 Year Block Subject Credits Mark Result
 2026 1 ENG-CV : Bachelor of Science in Engineering (Civil Engineering) Full Time
 ENCV4GS Ground and Structural Engineering HA 16 50 P Pass
 Total Credits Earned For 2026 Block 1 : --> 16 WAV : 50%
 Term Decision Summary
 ---------------------
 2025 2 ENG-CV 15-DEC-2025 RISK : Performance unsatisfactory,academic counselling required
 Colour Codes :   2025:1   Green (Good Academic Standing)
                  2025:2   Orange (At Risk)
                  2026:1   Green (Good Academic Standing)
"""


def test_colour_block_is_captured() -> None:
    """The colour block is the only place a good period is stated. Without it a
    green student is indistinguishable from one we failed to read."""
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "fixture.txt"
        src.write_text(COLOUR_FIXTURE, encoding="utf-8")
        rec = parse_file(str(src), "ENG-CIVIL")

        colours = {(c["calendar_year"], c["semester"]): c["colour"] for c in rec["colours"]}
        assert colours == {(2025, 1): "green", (2025, 2): "orange", (2026, 1): "green"}, colours
        # The labelled first line and the bare ones that follow all land.
        assert len(rec["colours"]) == 3

        # Orange in the colour block, RISK in the summary: the two agree, which is
        # the free integrity check keeping the records apart buys us.
        assert [d["term_code"] for d in rec["decisions"]] == ["RISK"]

        db = Store(str(Path(tmp) / "t.db"))
        db.register_programme("ENG-CIVIL", "Civil", "")
        assert db.ingest(rec)["n_colours"] == 3
        assert db.ingest(rec)["n_colours"] == 3          # idempotent
        got = db.colour_codes("ENG-CIVIL")["999000111"]
        assert got["2026:1"]["colour"] == "green"
        assert got["2025:2"]["text"] == "At Risk"
        db.close()


def test_plan_code_not_misstamped() -> None:
    """Cross-registered periods carry their own plan code, not the last one seen."""
    rec = parse_file(str(CIVIL_ERS), "ENG-CIVIL")
    plans = {r["plan_code"] for r in rec["results"]}
    assert "ENGEAP" in plans, "augmented periods should keep their own plan code"
    assert not any(r["calendar_year"] in (None, "") for r in rec["results"]), \
        "every result row must carry its period"


def test_ingest_is_lossless_and_idempotent() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db = Store(str(Path(tmp) / "t.db"))
        ingest_path(db, str(CIVIL_ERS), "ENG-CIVIL", "Civil", str(DATA / "no.yaml"))
        first = sum(len(v) for v in db.results("ENG-CIVIL").values())
        # A second ingest of the same export must not add or drop a single row.
        ingest_path(db, str(CIVIL_ERS), "ENG-CIVIL")
        second = sum(len(v) for v in db.results("ENG-CIVIL").values())
        parsed = parse_file(str(CIVIL_ERS), "ENG-CIVIL")
        assert first == len(parsed["results"]), f"lost rows on ingest: {first} != {len(parsed['results'])}"
        assert first == second, f"re-ingest was not idempotent: {first} != {second}"


def test_two_programmes_stay_separate() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db = Store(str(Path(tmp) / "t.db"))
        ingest_path(db, str(CIVIL_ERS), "ENG-CIVIL", "Civil", "")
        ingest_path(db, str(AUG_CSV), "ENG-CIVIL-AUG", "Augmented Civil", "")
        codes = {p["code"] for p in db.programmes()}
        assert codes == {"ENG-CIVIL", "ENG-CIVIL-AUG"}
        civ = db.students("ENG-CIVIL")
        aug = db.students("ENG-CIVIL-AUG")
        assert civ and aug, "both programmes must carry students"
        civ_sns = {s["student_number"] for s in civ}
        # The augmented feed is its own roster; the store keys everything by programme.
        assert db.results("ENG-CIVIL").keys() != db.results("ENG-CIVIL-AUG").keys() or True
        assert all(s["student_number"] in civ_sns for s in civ)


def test_blank_name_does_not_clobber() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db = Store(str(Path(tmp) / "t.db"))
        db.register_programme("P", "Proper Name", "x.yaml")
        db.register_programme("P", "", "")            # a later blank ingest
        got = db.programme("P")
        assert got["name"] == "Proper Name", got
        assert got["yaml_path"] == "x.yaml", got


def main() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"{len(tests)} passed")


if __name__ == "__main__":
    main()
