#!/usr/bin/env python3
"""Regression tests for in_progress_now.

Guards the 2026-09-17 defect: an ERS export is a snapshot, so a blank
"registered, nothing posted yet" row from an older export outlives the period
it describes. Rows are never reconciled away on re-ingest, so without a
current-year test they read as live registrations years later.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from datasource import in_progress_now, latest_year


def row(year, code, mark=None, rc=""):
    return {"calendar_year": year, "course_code": code, "mark": mark, "result_code": rc}


def test_blank_row_from_an_older_year_is_not_current():
    rows = [row("2025", "ENCV2MW"), row("2026", "ENCV3FB")]
    assert in_progress_now(rows, "2026") == ["ENCV3FB"]


def test_settled_rows_are_never_in_progress():
    rows = [row("2026", "ENCV3FA", mark=51, rc="P"), row("2026", "ENCV3CW")]
    assert in_progress_now(rows, "2026") == ["ENCV3CW"]


def test_a_student_with_no_current_year_rows_is_registered_for_nothing():
    assert in_progress_now([row("2024", "MATH141")], "2026") == []


def test_latest_year_reads_the_newest_row():
    assert latest_year({"1": [row("2024", "A")], "2": [row("2026", "B")]}) == "2026"


def test_an_older_blank_never_overwrites_a_posted_result():
    """Ingest order: a mid-year export ingested after a newer one must not blank
    the result the newer one posted (220090947's 2021 results were lost this way)."""
    import os, tempfile
    from store import Store

    def ers(grade, rc):
        return {"students": [], "decisions": [], "results": [{
            "programme": "T", "student_number": "1", "calendar_year": "2021",
            "block": "2", "module_code": "MATH142", "credits": 16,
            "grade": grade, "result_code": rc}]}

    path = os.path.join(tempfile.mkdtemp(), "t.db")
    st = Store(path)
    st.ingest(ers(75, "P"))          # newer export: result posted
    st.ingest(ers(None, None))       # older mid-year export: registered, blank
    got = [tuple(r) for r in st.db.execute("select grade, result_code from results")]
    assert got == [(75.0, "P")], got
    st.ingest(ers(40, "F"))          # a posted value still updates
    got = [tuple(r) for r in st.db.execute("select grade, result_code from results")]
    assert got == [(40.0, "F")], got


def test_ingest_order_does_not_change_the_snapshot():
    """Newest-first and oldest-first loads give identical students, decisions and
    colours; an older export cannot restore a pre-appeal code."""
    import os, tempfile
    from store import Store

    def ers(year, sem, code, yos, colour):
        r = dict(programme="T", student_number="1")
        return {"students": [dict(r, surname="X", year_of_study=yos, total_credits=yos * 100)],
                "results": [dict(r, calendar_year=year, block=str(sem), module_code="M",
                                 credits=16, grade=60, result_code="P")],
                "decisions": [dict(r, calendar_year="2025", semester=1, term_code=code)],
                "colours": [dict(r, calendar_year="2025", semester=1, colour=colour)]}

    july = ers("2025", 1, "FPRR", 2, "red")        # decided in July
    jan = ers("2025", 2, "RAPB", 3, "red")         # appeal outcome, next January

    def load(*docs):
        st = Store(os.path.join(tempfile.mkdtemp(), "t.db"))
        for d in docs:
            st.ingest(d)
        q = lambda sql: [tuple(x) for x in st.db.execute(sql)]
        return (q("select year_of_study, total_credits, as_of from students"),
                q("select term_code, as_of from term_decisions"),
                q("select colour, as_of from colour_codes"))

    newest_first, oldest_first = load(jan, july), load(july, jan)
    assert newest_first == oldest_first, (newest_first, oldest_first)
    assert newest_first[0] == [(3.0, 300.0, "2025:2")]
    assert newest_first[1] == [("RAPB", "2025:2")]


def test_mid_semester_codes_do_not_date_an_export():
    """A July export lists semester-2 deregistrations (DE, no mark). They are not
    assessed results, so the export stays dated to semester 1."""
    from store import _as_of
    rows = [{"calendar_year": "2025", "block": "1", "grade": 60, "result_code": "P"},
            {"calendar_year": "2025", "block": "2", "grade": None, "result_code": "DE"},
            {"calendar_year": "2025", "block": "2", "grade": None, "result_code": "F/"}]
    assert _as_of(rows) == "2025:1"


def test_the_newer_export_wins_a_changed_result():
    """Two exports post different results for one sitting (a remark): the newer
    export's result stands, whatever the load order."""
    import os, tempfile
    from store import Store

    def ers(grade, rc, posted_to):
        r = dict(programme="T", student_number="1")
        return {"students": [], "decisions": [], "results": [
            dict(r, calendar_year="2021", block="1", module_code="ENCV3ST",
                 credits=16, grade=grade, result_code=rc),
            dict(r, calendar_year=posted_to[0], block=posted_to[1], module_code="X",
                 credits=8, grade=60, result_code="P")]}

    older, newer = ers(47, "F", ("2021", "1")), ers(50, "P", ("2022", "1"))
    for docs in ((older, newer), (newer, older)):
        st = Store(os.path.join(tempfile.mkdtemp(), "t.db"))
        for d in docs:
            st.ingest(d)
        got = [tuple(x) for x in st.db.execute(
            "select grade, result_code from results where module_code='ENCV3ST'")]
        assert got == [(50.0, "P")], got


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all datasource tests pass")
