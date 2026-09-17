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


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all datasource tests pass")
