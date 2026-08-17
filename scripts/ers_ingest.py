#!/usr/bin/env python3
"""
ers_ingest.py -- read any programme's ERS export into three record lists.

    students    one row per student: bio and standing snapshot
    results     one row per module attempt
    decisions   the registrar's term-decision code per (student, period)

It generalises the earlier Civil-only parser. Nothing here names Civil
Engineering: the plan code (ENG-CV, ENGEAP, ENG-ME, ...) is read straight from
each section line and carried on every row. The caller assigns the programme
LABEL for the whole file (say "ENG-CIVIL" or "ENG-CIVIL-AUG"); the plan code is
recorded beside it so a cross-registered period is visible rather than mis-stamped.

extract_text accepts a real PDF (via pdftotext -layout) or a file that is
already the pdftotext-layout dump, so the same reader works on the export and on
a saved text dump.

    from ers_ingest import parse_file
    rec = parse_file("ENG-CV_ERS.pdf", programme="ENG-CIVIL")
"""
from __future__ import annotations
from typing import Any, Optional
import re
import subprocess
from pathlib import Path

# A module code: four letters, a digit, then two more. Anchors a result row.
MODULE_RE = re.compile(r"^[A-Z]{4}\d[A-Z0-9]{2}")
# A student header: 9-digit number followed by a capitalised name.
STUDENT_RE = re.compile(r"\b(\d{9})\s+([A-Z][a-z]+(?:,\s*|\s+)[A-Za-z\s,]+)")
# A programme section line: "<year> <block> <PLAN> : <name>". PLAN is any
# uppercase token (ENG-CV, ENGEAP, ENG-ME); we no longer hard-code one.
SECTION_RE = re.compile(r"(\d{4})\s+(S?\d+)\s+([A-Z][A-Z0-9-]{2,})\s*:")
# The registrar's term decision, and the year it belongs to.
DECISION_HEADER_RE = re.compile(r"Term Decision Proposals For\s+(\d{4})")
DECISION_LINE_RE = re.compile(r"Semester\s+(\d+)\s+Proposed\s*:\s*([A-Z0-9]+)\s*:\s*(.*)")
# Credits earned by study period: "Summary By Study Period : 1: 72 2: 48".
SUMMARY_RE = re.compile(r"(\d+):\s*(\d+)")


def extract_text(path: str) -> str:
    """Return the layout text of an ERS file, whether PDF or already text."""
    with open(path, "rb") as fh:
        head = fh.read(5)
    if head.startswith(b"%PDF"):
        out = subprocess.run(["pdftotext", "-layout", path, "-"],
                             capture_output=True, text=True, check=True)
        return out.stdout
    return Path(path).read_text(encoding="utf-8", errors="replace")


def _parse_module_line(parts: list[str]) -> Optional[dict]:
    """One module row -> its fields, or None when the line is not a result."""
    if len(parts) < 2:
        return None
    module_code = parts[0]
    try:
        ind = parts.index("HA")
    except ValueError:
        try:
            ind = parts.index("PA")
        except ValueError:
            return None
    module_name = " ".join(parts[1:ind])
    after = parts[ind + 1:]
    credits = grade = result_code = result_text = None
    j = 0
    while j < len(after):
        p = after[j]
        if re.match(r"^\(?\d+\)?$", p) and credits is None:
            n = int(re.sub(r"[()]", "", p))
            if n <= 24:
                credits = n
            elif grade is None:
                grade = n
        elif re.match(r"^\d{2,3}$", p) and credits is not None and grade is None:
            grade = int(p)
        elif p in ("P", "F", "FS", "F/", "FA", "PM", "DE"):
            result_code = p
            if j + 1 < len(after):
                result_text = " ".join(after[j + 1:])
            break
        j += 1
    return {"module_code": module_code, "module_name": module_name,
            "credits": credits, "grade": grade,
            "result_code": result_code, "result_text": result_text}


def parse_ers(text: str, programme: str) -> dict[str, list[dict]]:
    """ERS layout text -> {'students', 'results', 'decisions'} for one programme."""
    lines = [ln.rstrip() for ln in text.split("\n")]
    results: list[dict] = []
    decisions: list[dict] = []
    students: dict[str, dict] = {}

    student = surname = name = None
    calendar_year = block = plan_code = None
    study_period = None
    pending_year = None
    # Per-student accumulators, flushed when the next student begins.
    pending_rows: list[dict] = []
    period_credits: dict[int, int] = {}

    def flush() -> None:
        """Stamp the current student's rows with year/credits, then bank them."""
        nonlocal pending_rows, period_credits
        if student and pending_rows:
            yos = max(period_credits) if period_credits else None
            total = sum(period_credits.values()) if period_credits else None
            for r in pending_rows:
                r["year_of_study"] = yos
                r["total_credits"] = total
                results.append(r)
            students.setdefault(student, {
                "student_number": student, "programme": programme,
                "surname": surname, "name": name, "plan_code": plan_code,
                "year_of_study": yos, "total_credits": total})
        pending_rows = []
        period_credits = {}

    for line in lines:
        if "Cancelled" in line:
            flush()
            student = None
            continue

        m = re.match(r"\s*(\d+)(?:st|nd|rd|th)\s+Year", line)
        if m:
            study_period = int(m.group(1))

        sm = STUDENT_RE.search(line)
        if sm:
            if sm.group(1) != student:      # a genuinely new student, not a page break
                flush()
                student = sm.group(1)
                bits = sm.group(2).replace(",", " ").split()
                surname = bits[0] if bits else ""
                name = bits[1] if len(bits) > 1 else ""
            continue

        if not student:
            continue

        dh = DECISION_HEADER_RE.search(line)
        if dh:
            pending_year = int(dh.group(1))
            continue
        dl = DECISION_LINE_RE.search(line)
        if dl:
            decisions.append({"student_number": student, "programme": programme,
                              "calendar_year": pending_year or calendar_year,
                              "semester": int(dl.group(1)),
                              "term_code": dl.group(2),
                              "term_text": dl.group(3).strip()})
            continue

        if "Summary By Study Period" in line:
            period_credits = {int(a): int(b) for a, b in SUMMARY_RE.findall(line)}
            continue

        sec = SECTION_RE.search(line)
        if sec and ":" in line and not MODULE_RE.match(line.strip()):
            calendar_year, block, plan_code = sec.group(1), sec.group(2), sec.group(3)
            continue

        if MODULE_RE.match(line.strip()):
            md = _parse_module_line(line.split())
            if md:
                pending_rows.append({
                    "student_number": student, "programme": programme,
                    "surname": surname, "name": name,
                    "calendar_year": calendar_year, "block": block,
                    "plan_code": plan_code, "study_period": study_period,
                    "year_of_study": None, "total_credits": None, **md})

    flush()
    return {"students": list(students.values()),
            "results": results, "decisions": decisions}


def parse_file(path: str, programme: str) -> dict[str, list[dict]]:
    """Convenience: read a file (PDF or text) and parse it for one programme."""
    return parse_ers(extract_text(path), programme)
