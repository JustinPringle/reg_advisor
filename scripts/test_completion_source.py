#!/usr/bin/env python3
"""Completion (DC/DGOR) on the initial ERS run as well as the final record.

The DC list goes to the exam board BEFORE the final ERS is run, so the
Completion tab must read an initial run on file exactly as it reads the
captured record -- same rule, nothing written to the store. The ERS check's
"who has finished" set must follow the same source.

Synthetic data only (invented student numbers). Run from scripts/:
    python test_completion_source.py
"""
from __future__ import annotations
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import checks_service as CHK
import completion as C
from programme_loader import load_programme
from store import Store

PROG = "P-TEST"
YAML = str(Path(__file__).resolve().parent.parent / "programmes" / "civil.yaml")
LAST = "ENCV4DS"          # the module the finisher passes in the latest semester


def _row(sn, code, year, block, credits=8, grade=70, rc="P"):
    return {"programme": PROG, "student_number": sn, "calendar_year": str(year),
            "block": str(block), "module_code": code, "module_name": code,
            "credits": credits, "grade": grade, "result_code": rc}


def _transcript(sn, finish: bool):
    """Every prescribed Civil module passed, plus the electives the rule needs.
    `finish=False` leaves LAST unassessed, as the final record stood before the
    initial run posted it."""
    cur = load_programme(YAML)
    rows = [_row(sn, m["code"], 2025, 1) for m in C.prescribed_modules(cur)
            if m["code"] != LAST]
    rows += [_row(sn, "ENCV4EL", 2025, 1, credits=8),     # Level-4 8cr EN elective
             _row(sn, "LING101", 2024, 1, credits=16),
             _row(sn, "LING102", 2024, 2, credits=16)]
    rows.append(_row(sn, LAST, 2026, 2) if finish
                else _row(sn, LAST, 2026, 2, grade=None, rc=None))
    return rows


def _parsed(finish: bool):
    sn = "900000001"
    return {"students": [{"programme": PROG, "student_number": sn,
                          "surname": "Test", "name": "Finisher",
                          "year_of_study": 4, "total_credits": 576}],
            "results": _transcript(sn, finish), "decisions": [], "colours": []}


def _store(initial=None):
    """A store holding the final record (LAST not yet posted), and optionally an
    initial run on file whose parse is `initial`."""
    d = tempfile.mkdtemp()
    st = Store(os.path.join(d, "t.db"))
    st.register_programme(PROG, "Test", YAML)
    st.ingest(_parsed(finish=False))
    if initial is not None:
        path = os.path.join(d, "initial.txt")
        Path(path).write_text("stub")
        st.add_document(PROG, "initial", "initial.txt", path)
        CHK.parse_file = lambda p, prog: {k: list(v) for k, v in initial.items()}
    return st


def test_no_initial_on_file_says_so():
    st = _store()
    out = CHK.completion(st, PROG, source="initial")
    assert out["ready"] is False and out["source"] == "initial"
    assert "no initial ERS" in out["error"]
    assert out["summary"] == {"DC": 0, "DGOR": 0}


def test_final_is_the_default_and_unchanged():
    st = _store()
    out = CHK.completion(st, PROG)
    assert out["ready"] and out["source"] == "final"
    assert out["summary"]["DC"] == 0          # LAST not yet posted in the record


def test_initial_lists_this_semesters_finisher():
    st = _store(initial=_parsed(finish=True))
    out = CHK.completion(st, PROG, source="initial")
    assert out["ready"] and out["source"] == "initial"
    assert [r["student_number"] for r in out["DC"]] == ["900000001"], out["DC"]
    assert out["DC"][0]["completed"] == "2026 S2"
    assert out["DC"][0]["name"] == "Test, Finisher"


def test_same_data_gives_the_same_list_from_either_source():
    st = _store(initial=_parsed(finish=False))
    a = CHK.completion(st, PROG, source="final")
    b = CHK.completion(st, PROG, source="initial")
    assert a["DC"] == b["DC"] and a["DGOR"] == b["DGOR"]


def test_initial_writes_nothing_to_the_store():
    st = _store(initial=_parsed(finish=True))
    before = st.db.execute("select count(*), sum(grade is null) from results").fetchone()
    CHK.completion(st, PROG, source="initial")
    after = st.db.execute("select count(*), sum(grade is null) from results").fetchone()
    assert tuple(before) == tuple(after)
    assert CHK.completion(st, PROG, source="final")["summary"]["DC"] == 0


def test_period_scope_applies_to_initial():
    st = _store(initial=_parsed(finish=True))
    hit = CHK.completion(st, PROG, "2026", "2", source="initial")
    miss = CHK.completion(st, PROG, "2026", "1", source="initial")
    assert hit["summary"]["DC"] == 1 and miss["summary"]["DC"] == 0
    assert miss["periods"] == hit["periods"]          # picker stays put


def test_ers_check_reads_completion_from_its_own_source():
    """Checking the initial run, a student who finished this semester is degree
    complete -- not judged on the final record, which is a semester behind."""
    st = _store(initial=_parsed(finish=True))
    cur, init, _ = CHK._source(st, PROG, "initial")
    _, fin, _ = CHK._source(st, PROG, "final")
    assert CHK._complete_set(cur, init) == {"900000001"}
    assert CHK._complete_set(cur, fin) == set()
    d = CHK.student_detail(st, PROG, "900000001", source="initial")
    assert d["ready"] and d["engine_code"] == "ERS-COMPLETE", d.get("engine_code")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
    print("\nall completion-source tests pass")
