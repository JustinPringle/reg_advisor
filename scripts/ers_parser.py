#!/usr/bin/env python3
"""
ers_parser.py -- read an ERS export into two files.

    data/ers_data.csv     module results (one row per module attempt)
    data/ers_history.csv  the registrar's term-decision code per student

The second file is the piece the earlier parser dropped. The ERS carries a
"Term Decision Proposals" block against each student -- RISK, PROB, FPRR and so
on -- and that code is the authoritative academic standing. Capturing it lets
the advisor show the registrar's own verdict and lets ers_engine read the prior
term's status (history.last_status) instead of always starting from "none".

extract_text accepts a real PDF (via pdftotext) or a file that is already the
pdftotext -layout output, so the same script works on the export and on a saved
text dump.

Run from the scripts/ directory:
    python ers_parser.py ../data/ENG-CV_ERS_1_Jul_2026.pdf
"""
from __future__ import annotations
from typing import Optional
import csv
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"

MODULE_RE = re.compile(r"^[A-Z]{4}\d[A-Z0-9]{2}")
STUDENT_RE = re.compile(r"\b(\d{9})\s+([A-Z][a-z]+(?:,\s*|\s+)[A-Za-z\s,]+)")
DECISION_HEADER_RE = re.compile(r"Term Decision Proposals For\s+(\d{4})")
DECISION_LINE_RE = re.compile(r"Semester\s+(\d+)\s+Proposed\s*:\s*([A-Z0-9]+)\s*:\s*(.*)")


# --- text extraction --------------------------------------------------------
def extract_text(path: str) -> str:
    """Return the layout text of an ERS file, whether PDF or already text."""
    with open(path, "rb") as fh:
        head = fh.read(5)
    if head.startswith(b"%PDF"):
        out = subprocess.run(["pdftotext", "-layout", path, "-"],
                             capture_output=True, text=True, check=True)
        return out.stdout
    return Path(path).read_text(encoding="utf-8", errors="replace")


# --- module-result parsing (kept from the original parser) ------------------
def parse_module_line(parts: list[str]) -> Optional[dict]:
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


def parse_ers(text: str) -> tuple[list[dict], list[dict]]:
    """Return (module rows, history rows) from ERS layout text."""
    lines = [ln.rstrip() for ln in text.split("\n")]
    modules: list[dict] = []
    history: list[dict] = []

    student = surname = name = calendar_year = block = None
    study_period = None
    pending_year = None

    for line in lines:
        if "Cancelled" in line:
            student = None
            continue

        m = re.match(r"\s*(\d+)(?:st|nd|rd|th)\s+Year", line)
        if m:
            study_period = int(m.group(1))

        sm = STUDENT_RE.search(line)
        if sm:
            student = sm.group(1)
            bits = sm.group(2).replace(",", " ").split()
            surname = bits[0] if bits else ""
            name = bits[1] if len(bits) > 1 else ""
            continue

        if not student:
            continue

        # Term-decision block: header sets the year, the next Proposed line
        # (if any) carries the code for the student whose record this ends.
        dh = DECISION_HEADER_RE.search(line)
        if dh:
            pending_year = int(dh.group(1))
            continue
        dl = DECISION_LINE_RE.search(line)
        if dl:
            history.append({"student_number": student,
                            "calendar_year": pending_year or calendar_year,
                            "semester": int(dl.group(1)),
                            "term_code": dl.group(2),
                            "term_text": dl.group(3).strip()})
            continue

        if "ENG-CV" in line and "Bachelor" in line:
            ym = re.search(r"(\d{4})\s+(S?\d+)\s+ENG-CV", line)
            if ym:
                calendar_year, block = ym.group(1), ym.group(2)
            continue

        if MODULE_RE.match(line.strip()):
            md = parse_module_line(line.split())
            if md:
                modules.append({"student_number": student, "surname": surname,
                                "name": name, "calendar_year": calendar_year,
                                "block": block, "study_period": study_period,
                                **md})
    return modules, history


# --- write ------------------------------------------------------------------
def write_csv(rows: list[dict], path: Path, fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fields})


def main() -> None:
    src = sys.argv[1] if len(sys.argv) > 1 else str(DATA / "ENG-CV_ERS_1_Jul_2026.pdf")
    modules, history = parse_ers(extract_text(src))

    write_csv(history, DATA / "ers_history.csv",
              ["student_number", "calendar_year", "semester", "term_code", "term_text"])
    print(f"wrote {len(history)} term-decision rows -> data/ers_history.csv")

    # Refreshing ers_data.csv is optional; the repo already carries a copy. Pass
    # --results to regenerate it from this export.
    if "--results" in sys.argv:
        write_csv(modules, DATA / "ers_data.csv",
                  ["student_number", "surname", "name", "calendar_year", "block",
                   "study_period", "module_code", "module_name", "credits",
                   "grade", "result_code", "result_text"])
        print(f"wrote {len(modules)} module rows -> data/ers_data.csv")


if __name__ == "__main__":
    main()
