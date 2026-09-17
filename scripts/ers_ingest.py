#!/usr/bin/env python3
"""
ers_ingest.py -- read any programme's ERS export into three record lists.

    students    one row per student: bio and standing snapshot
    results     one row per module attempt
    decisions   the registrar's term-decision code per (student, period)
    colours     the registrar's colour per (student, period) -- green/orange/red

The last two are different records and are kept apart. A decision code is only
written when something needs saying (RISK, PROB, ...), so most periods have
none; the colour block states a standing for EVERY period, including the green
ones. Keeping both lets a green period be read as green rather than as missing,
and lets each cross-check the other -- 2023:2 Orange beside 2023:2 RISK.

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

# ---------------------------------------------------------------------------
# What each pattern looks for in the ERS text. One line of the export, one
# pattern. Read them beside a real export and they should be obvious.
# ---------------------------------------------------------------------------

# A module result row. Starts with a module code: four letters, a digit, two more.
#   "ENCV4GS Ground and Structural Engineering HA 16 50 P Pass"
MODULE_RE = re.compile(r"^[A-Z]{4}\d[A-Z0-9]{2}")

# The line that starts a student's record: student number, then their name.
#   "222009802 Bisseru, Deyajal ENCV"
STUDENT_RE = re.compile(r"\b(\d{9})\s+([A-Z][a-z]+(?:,\s*|\s+)[A-Za-z\s,]+)")

# The line that starts one study period inside a record: year, block, plan code.
#   "2026 1 ENG-CV : Bachelor of Science in Engineering (Civil Engineering)"
# The plan is read, never assumed, so a period taken on another plan shows up as
# itself instead of being stamped with this programme's code.
SECTION_RE = re.compile(r"(\d{4})\s+(S?\d+)\s+([A-Z][A-Z0-9-]{2,})\s*:")

# THE REGISTRAR'S VERDICT, in three places. All three are wanted.
#
# 1. This year's proposal, under a header that names the year:
#      "Term Decision Proposals For 2026"
#      "  Semester 1 Proposed : RSK2 : Still at risk, continue Counselling"
DECISION_HEADER_RE = re.compile(r"Term Decision Proposals For\s+(\d{4})")
DECISION_LINE_RE = re.compile(r"Semester\s+(\d+)\s+Proposed\s*:\s*([A-Z0-9]+)\s*:\s*(.*)")

# 2. A past decision, listed in the "Term Decision Summary" at the foot of the
#    record, one line per period that carried one:
#      "2023 2 ENG-CV 13-DEC-2023 RISK : Performance unsatisfactory, ..."
DECISION_HISTORY_RE = re.compile(
    r"(\d{4})\s+(S?\d+)\s+[A-Z][A-Z0-9-]{2,}\s+\d{2}-[A-Z]{3}-\d{4}\s+([A-Z0-9]+)\s*:\s*(.*)")

# 3. The Colour Codes block, last thing in the record. This is the one the
#    summary table does NOT give us: a period in good standing carries no
#    decision code at all, but it DOES carry a colour. Without this block a
#    green student is indistinguishable from a student we failed to read.
#      "Colour Codes : 2022:1  Green (Good Academic Standing)"
#      "               2023:2  Orange (At Risk)"
#    Only the first line carries the "Colour Codes :" label; the rest are bare,
#    so the pattern matches the period-and-colour part wherever it appears.
#
#    BLUE is the fourth word the block uses -- "2021:1 Blue (Outstanding
#    Academic Achievement)" -- and leaving it out did not read a blue period as
#    unknown, it dropped the row entirely, so the best students were the ones
#    the check could not see. Blue resolves to green in standing_codes.
COLOUR_RE = re.compile(r"(\d{4}):(\d)\s+(Green|Orange|Red|Blue)\b\s*(?:\(([^)]*)\))?",
                       re.IGNORECASE)

# Credits earned per study period, one line near the foot of the record:
#   "Summary By Study Period : 1:184  2:128  3:136  4: 80"
SUMMARY_RE = re.compile(r"(\d+):\s*(\d+)")

# The stream the student belongs to (ENCV, ENME, ENEL ...), which rides on the
# header line after the name. "ENGEAP" cannot match -- \b needs a boundary after
# two letters -- so the augmented access token is excluded for free.
STREAM_RE = re.compile(r"\bEN[A-Z]{2}\b")

# How the student was admitted: "Access : ENGEAP 2021", "Access : CT-ENG".
# A mainstream feed uses this to hand augmented students to their own programme
# rather than claim them.
ACCESS_RE = re.compile(r"Access\s*:\s*([A-Z0-9-]+)")


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


def parse_ers(text: str, programme: str,
              keep_streams: Optional[set[str]] = None,
              exclude_access: Optional[set[str]] = None) -> dict[str, list[dict]]:
    """ERS layout text -> {'students', 'results', 'decisions', 'colours'} for one programme.

    keep_streams, when given, keeps only students whose header stream set meets it
    (set intersection). A student carrying several streams (a first-year still
    choosing, e.g. ENCV ENME) is kept if ANY of them is wanted. Students with no
    readable stream code cannot be placed; they are dropped from the kept set and
    returned under 'skipped' so the caller can surface them rather than lose them.
    """
    lines = [ln.rstrip() for ln in text.split("\n")]
    results: list[dict] = []
    decisions: list[dict] = []
    colours: list[dict] = []
    students: dict[str, dict] = {}

    student = surname = name = None
    calendar_year = block = plan_code = None
    study_period = None
    pending_year = None
    # Per-student accumulators, flushed when the next student begins.
    pending_rows: list[dict] = []
    period_credits: dict[int, int] = {}
    student_plan: Optional[str] = None   # current plan: the most recent period's
    student_streams: dict[str, set[str]] = {}   # header stream codes seen per student
    student_access: dict[str, str] = {}          # access route per student (first seen)

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
                "surname": surname, "name": name, "plan_code": student_plan or plan_code,
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
                student_plan = None
                bits = sm.group(2).replace(",", " ").split()
                surname = bits[0] if bits else ""
                name = bits[1] if len(bits) > 1 else ""
            # Header stream codes ride after the name; read them from the raw line.
            tail = line.split(sm.group(1), 1)[1]
            student_streams.setdefault(student, set()).update(STREAM_RE.findall(tail))
            continue

        if not student:
            continue

        am = ACCESS_RE.search(line)
        if am and student not in student_access:
            student_access[student] = am.group(1)
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

        cm = COLOUR_RE.search(line)
        if cm:
            colours.append({"student_number": student, "programme": programme,
                            "calendar_year": int(cm.group(1)),
                            "semester": int(cm.group(2)),
                            "colour": cm.group(3).lower(),
                            "colour_text": (cm.group(4) or "").strip()})
            continue

        hx = DECISION_HISTORY_RE.search(line)
        if hx:
            decisions.append({"student_number": student, "programme": programme,
                              "calendar_year": int(hx.group(1)),
                              "semester": 2 if hx.group(2).endswith("2") else 1,
                              "term_code": hx.group(3),
                              "term_text": hx.group(4).strip()})
            continue

        if "Summary By Study Period" in line:
            period_credits = {int(a): int(b) for a, b in SUMMARY_RE.findall(line)}
            continue

        sec = SECTION_RE.search(line)
        if sec and ":" in line and not MODULE_RE.match(line.strip()):
            calendar_year, block, plan_code = sec.group(1), sec.group(2), sec.group(3)
            if student_plan is None:        # first section after the header = latest period
                student_plan = plan_code
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

    # Stamp every student with the stream codes and access route read from its header.
    for srec in students.values():
        srec["stream_codes"] = sorted(student_streams.get(srec["student_number"], set()))
        srec["access_code"] = student_access.get(srec["student_number"], "")

    if keep_streams is None and exclude_access is None:
        return {"students": list(students.values()), "results": results,
                "decisions": decisions, "colours": colours, "skipped": []}

    # Two gates, both fail-safe:
    #   keep_streams   -- keep only students whose header streams meet the set
    #                     (a shared feed filtered to the streams this programme owns);
    #   exclude_access -- drop students admitted through another programme's access
    #                     route (e.g. a mainstream feed defers ENGEAP augmented students).
    # A student failing either gate is surfaced under 'skipped', never lost.
    want = set(keep_streams) if keep_streams is not None else None
    deny = set(exclude_access) if exclude_access is not None else set()
    kept, skipped = set(), []
    for s2 in students.values():
        sn = s2["student_number"]
        streams = set(s2["stream_codes"])
        access = s2["access_code"]
        if want is not None and not (streams & want):
            skipped.append({"student_number": sn, "stream_codes": sorted(streams),
                            "access_code": access, "reason": "stream"})
        elif access in deny:
            skipped.append({"student_number": sn, "stream_codes": sorted(streams),
                            "access_code": access, "reason": "access"})
        else:
            kept.add(sn)
    return {
        "students":  [s2 for s2 in students.values()    if s2["student_number"] in kept],
        "results":   [r  for r  in results               if r["student_number"] in kept],
        "decisions": [d  for d  in decisions             if d["student_number"] in kept],
        "colours":   [c  for c  in colours                if c["student_number"] in kept],
        "skipped":   skipped}


def parse_file(path: str, programme: str,
               keep_streams: Optional[set[str]] = None,
               exclude_access: Optional[set[str]] = None) -> dict[str, list[dict]]:
    """Convenience: read a file (PDF or text) and parse it for one programme."""
    return parse_ers(extract_text(path), programme, keep_streams, exclude_access)
