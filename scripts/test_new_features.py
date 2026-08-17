"""
test_new_features.py -- checks for the three features, on real ERS data.

Run from scripts/:  python test_new_features.py
"""
from __future__ import annotations
import tempfile
from pathlib import Path

from store import Store
from ingest_ers import ingest_path
import programme_catalogue as PC
import completion as C
import ers_check as X
import checks_service as CHK
import regadvisor_engine as R
from programme_loader import load_programme
from data_loaders import load_results

PROG = "../programmes"
CIVIL_YAML = f"{PROG}/civil.yaml"
CIVIL_TXT = "../data/ers_civil.txt"


def test_catalogue_reads_folder():
    rows = PC.catalogue(PROG)
    codes = {r["code"] for r in rows}
    assert "ENG-CIVIL" in codes, codes
    civ = next(r for r in rows if r["code"] == "ENG-CIVIL")
    assert civ["name"] == "Civil Engineering"
    assert civ["modules"] > 0 and civ["error"] is None
    assert PC.resolve("ENG-CIVIL", PROG)["yaml_path"].endswith("civil.yaml")
    assert PC.resolve("NOPE", PROG) is None
    print(f"  catalogue: {len(rows)} programmes, resolve works")


def test_vac_work_detection():
    cur = load_programme(CIVIL_YAML)
    vac = [m["code"] for m in C.prescribed_modules(cur) if C.is_vac_work(m)]
    assert vac == ["ENCV4VW"], vac
    print(f"  vac-work module identified: {vac}")


def test_dg_and_dgor_synthetic():
    """DG needs every academic module + vac passed; DGOR needs only vac left."""
    cur = load_programme(CIVIL_YAML)
    presc = C.prescribed_modules(cur)
    # Build a transcript that passes every prescribed module.
    def rows_passing(codes):
        return [{"course_code": c, "result_code": "P", "credits": 8, "mark": 75,
                 "block": "1", "calendar_year": "2025", "year_of_study": 4} for c in codes]
    all_codes = [m["code"] for m in presc]
    tx_full = R.index_transcript(rows_passing(all_codes))
    assert C.classify_completion(cur, tx_full)["status"] == "DG"
    # Now drop the vac-work pass only -> DGOR.
    non_vac = [c for c in all_codes if c != "ENCV4VW"]
    tx_novac = R.index_transcript(rows_passing(non_vac))
    c = C.classify_completion(cur, tx_novac)
    assert c["status"] == "DGOR", c["status"]
    assert [o["code"] for o in c["vac_outstanding"]] == ["ENCV4VW"]
    # Drop an academic module -> neither.
    tx_gap = R.index_transcript(rows_passing(all_codes[:-3]))
    assert C.classify_completion(cur, tx_gap)["status"] is None
    print("  DG / DGOR / neither classify correctly on synthetic transcripts")


def test_completion_on_real_civil():
    cur = load_programme(CIVIL_YAML)
    res = load_results("../data/ers_data.csv")
    lists = C.completion_lists(cur, res)
    assert len(lists["DG"]) >= 1, "expected at least one degree-complete student"
    # Every DG row has no outstanding column; every DGOR row names vac work.
    for r in lists["DGOR"]:
        assert "ENCV4VW" in r["outstanding"]
    print(f"  real Civil: DG={len(lists['DG'])}, DGOR={len(lists['DGOR'])}")


def test_ers_check_on_real_civil():
    from ers_ingest import parse_file
    parsed = parse_file(CIVIL_TXT, "ENG-CIVIL")
    rep = X.check_parsed(parsed)
    s = rep["summary"]
    assert s["total"] > 100
    assert s["match"] + s["mismatch"] + s["review"] == s["total"]
    # Each row carries both codes and a verdict.
    r0 = rep["rows"][0]
    for k in ("registrar_code", "engine_code", "verdict", "direction"):
        assert k in r0
    print(f"  ERS check real Civil: {s}")


def test_prior_code_feeds_history():
    """The second-latest decision is handed to the engine as history."""
    decs = [{"student_number": "1", "calendar_year": "2024", "semester": 2,
             "term_code": "RISK", "term_text": ""},
            {"student_number": "1", "calendar_year": "2025", "semester": 1,
             "term_code": "PROB", "term_text": ""}]
    two = X.latest_two_decisions(decs)
    assert two["1"]["current"]["code"] == "PROB"
    assert two["1"]["prior"]["code"] == "RISK"
    print("  prior/current decision split works")


def test_initial_final_storage():
    """Final is ingested into the record; initial is stored but kept out of it."""
    with tempfile.TemporaryDirectory() as tmp:
        db = Store(str(Path(tmp) / "t.db"))
        # Final ERS -> full ingest.
        ingest_path(db, CIVIL_TXT, "ENG-CIVIL", "Civil Engineering", CIVIL_YAML)
        db.add_document("ENG-CIVIL", "final", "final.pdf", CIVIL_TXT)
        n_final = len(db.results("ENG-CIVIL"))
        assert n_final > 100
        # Initial ERS -> record the document only, do NOT ingest.
        db.add_document("ENG-CIVIL", "initial", "initial.pdf", CIVIL_TXT)
        assert len(db.results("ENG-CIVIL")) == n_final, "initial must not change the record"
        # current_document returns the right file per kind.
        assert db.current_document("ENG-CIVIL", "initial")["filename"] == "initial.pdf"
        assert db.current_document("ENG-CIVIL", "final")["filename"] == "final.pdf"
        assert len(db.documents("ENG-CIVIL")) == 2
        # Service can check both sources.
        fin = CHK.ers_check(db, "ENG-CIVIL", "final")
        ini = CHK.ers_check(db, "ENG-CIVIL", "initial")
        assert fin["ready"] and ini["ready"]
        assert fin["summary"]["total"] > 0 and ini["summary"]["total"] > 0
        comp = CHK.completion(db, "ENG-CIVIL")
        assert comp["ready"]
        print(f"  storage: final ingested ({n_final} result-students), "
              f"initial kept out; check(final)={fin['summary']}, "
              f"completion DG={comp['summary']['DG']}")


def test_only_current_document_flagged():
    with tempfile.TemporaryDirectory() as tmp:
        db = Store(str(Path(tmp) / "t.db"))
        db.add_document("P", "final", "v1.pdf", "/x/v1")
        db.add_document("P", "final", "v2.pdf", "/x/v2")
        cur = db.current_document("P", "final")
        assert cur["filename"] == "v2.pdf", "newest final must be current"
        currents = [d for d in db.documents("P") if d["is_current"]]
        assert len(currents) == 1, "only one final may be current"
        print("  document versioning: newest final is the single current one")


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    print(f"Running {len(tests)} checks\n")
    for t in tests:
        t()
    print(f"\nAll {len(tests)} checks passed.")


if __name__ == "__main__":
    main()
