"""
data_loaders.py -- turn UKZN artefacts into the shapes the engines read.

Two loaders, both programme-agnostic:

  load_curriculum(xlsx)  -> normalized curriculum {programme, modules[...]},
                            parsing the free-text "Prereqs and co-reqs" column
                            into the engine's term grammar. Anything it cannot
                            parse cleanly becomes a {"review": ...} term, which
                            forces that module to a human -- fail-safe by design.

  load_results(csv)      -> {student_number: [result rows]} in engine shape,
                            with pass/level derived per row.

Neither hard-codes Civil Engineering: the curriculum's identity comes from the
sheet, and the transcript loader works for any student in the feed.
"""
from __future__ import annotations
from typing import Any
import re
import csv
import openpyxl

from regadvisor_engine import code_level

CODE_RE = re.compile(r"[A-Z]{2,5}\d[A-Z0-9]{1,3}")
HEADER_RE = re.compile(r"(\d)\D*year\s*Sem\s*(\d)", re.I)
PASS_CODES_DEFAULT = {"P", "PM"}


# --- Prerequisite-text parser -----------------------------------------------
def _parse_token(tok: str) -> Any:
    """One comma/semicolon-delimited chunk -> a term (or a review flag)."""
    t = tok.strip().rstrip(".")
    if not t:
        return None
    low = t.lower()

    # "X or Y (or Z)" -> OR of course codes
    if re.search(r"\bor\b", low) and CODE_RE.search(t):
        codes = CODE_RE.findall(t)
        if len(codes) >= 2:
            return {"any": [c for c in codes]}

    # A single course code, optionally "(40%)" min-mark or "(DP)"
    codes = CODE_RE.findall(t)
    if len(codes) == 1 and len(t) < len(codes[0]) + 8:
        mm = re.search(r"\((\d{2})\s*%\)", t)
        return {"code": codes[0], "min_mark": int(mm.group(1))} if mm else codes[0]

    # Aggregate conditions we can map to real metrics
    y = re.search(r"(\d)\s*(?:st|nd|rd|th)?\s*(?:yr|year)", low)
    if y and ("regist" in low or "in " in low or "study" in low or "permitted" in low):
        return {"min_year": int(y.group(1))}
    cl = re.search(r"(\d+)\s*cr\w*\s*(?:at\s*)?level\s*(\d)", low)
    if cl:
        return {"min_credits": int(cl.group(1)), "level": int(cl.group(2))}
    ct = re.search(r"(\d+)\s*cr\w*\s*total", low)
    if ct:
        return {"min_credits": int(ct.group(1))}
    sm = re.search(r"(\d+)\s*sem", low)
    if sm and "reg" in low:
        return {"min_sem": int(sm.group(1))}

    # Multiple bare codes in one chunk (e.g. "MATH238 MATH248") -> AND
    if len(codes) >= 2 and not re.search(r"[a-z]{4}", low):
        return {"all": list(codes)}

    # Opaque handbook prose -> route the module to a human, never auto-clear.
    return {"review": t}


def parse_prereqs(text: str) -> tuple[list[Any], list[Any], list[str]]:
    """Free-text cell -> (prereqs, coreqs, review_notes)."""
    if not text or not str(text).strip():
        return [], [], []
    s = str(text).strip()
    coreqs: list[Any] = []

    # Pull out an explicit co-requisite clause.
    m = re.search(r"co-?req[s]?\s*[:\-]?\s*(.+)", s, re.I)
    if m:
        clause = m.group(1)
        s = s[:m.start()].strip(" ;,")
        for part in re.split(r"[;,]", clause):
            term = _parse_token(part)
            if term is not None:
                coreqs.append(term)

    prereqs: list[Any] = []
    reviews: list[str] = []
    for part in re.split(r"[;,]", s):
        term = _parse_token(part)
        if term is None:
            continue
        if isinstance(term, dict) and "review" in term:
            reviews.append(term["review"])
        prereqs.append(term)
    return prereqs, coreqs, reviews


# --- Curriculum loader ------------------------------------------------------
def load_curriculum(xlsx_path: str, sheet: str | None = None,
                    programme_code: str = "", programme_name: str = "") -> dict[str, Any]:
    """Read a programme sheet into the normalized curriculum shape.

    Expects section headers like "1st year Sem 1" and module rows of
    code | name | credits | (ax) | prereq-text. Works for any sheet in this
    layout -- nothing Civil-specific.
    """
    wb = openpyxl.load_workbook(xlsx_path, data_only=True)
    ws = wb[sheet] if sheet else wb.worksheets[0]
    rows = list(ws.iter_rows(values_only=True))
    title = ""
    for r in rows[:3]:
        if r and r[0] and str(r[0]).strip():
            title = str(r[0]).strip()
            break

    modules: list[dict[str, Any]] = []
    year = sem = 0
    total_credits = 0.0
    for r in rows:
        cells = [("" if c is None else str(c).strip()) for c in r]
        first = cells[0] if cells else ""
        joined = " ".join(cells)
        hm = HEADER_RE.search(joined)
        if hm and not CODE_RE.fullmatch(first):
            year, sem = int(hm.group(1)), int(hm.group(2))
            continue
        if not first or first.lower().startswith(("credit subtotal", "name")):
            continue
        code_match = CODE_RE.fullmatch(first)
        credit_cell = cells[2] if len(cells) > 2 else ""
        prereq_cell = cells[4] if len(cells) > 4 else ""
        # Elective / language-choice / free rows -> elective (skipped in advice).
        if not code_match or " or" in first.lower() or first.lower() == "elective":
            modules.append({"code": first or f"ELECTIVE_Y{year}S{sem}_{len(modules)}",
                            "name": cells[1] if len(cells) > 1 else "Elective",
                            "credits": _num(credit_cell), "year": year, "sem": sem,
                            "type": "elective", "prereqs": [], "coreqs": [], "review_notes": []})
            continue
        prereqs, coreqs, reviews = parse_prereqs(prereq_cell)
        credits = 0.0 if credit_cell.upper() == "DP" else _num(credit_cell)
        total_credits += credits
        modules.append({"code": first, "name": cells[1] if len(cells) > 1 else first,
                        "credits": credits, "year": year, "sem": sem, "type": "prescribed",
                        "prereqs": prereqs, "coreqs": coreqs, "review_notes": reviews,
                        "is_dp": credit_cell.upper() == "DP"})

    programme = {"code": programme_code or _slug(title), "name": programme_name or title,
                 "total_credits": total_credits}
    return {"programme": programme, "modules": modules, "elective_groups": {}}


# --- Transcript loader ------------------------------------------------------
def load_results(csv_path: str, pass_codes: set[str] | None = None,
                 pass_mark: float = 50) -> dict[str, list[dict[str, Any]]]:
    """CSV -> {student_number: [rows]} in engine shape. Period = year:block."""
    pass_codes = pass_codes or PASS_CODES_DEFAULT
    by_student: dict[str, list[dict[str, Any]]] = {}
    with open(csv_path, newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            sn = (r.get("student_number") or "").strip()
            if not sn:
                continue
            code = (r.get("module_code") or "").strip()
            rc = (r.get("result_code") or "").strip().upper()
            mark = _num(r.get("grade"))
            passed = (rc in pass_codes) or (not rc and mark is not None and mark >= pass_mark)
            by_student.setdefault(sn, []).append({
                "student_number": sn,
                "period": f"{r.get('calendar_year','')}:{r.get('block','')}",
                "calendar_year": r.get("calendar_year", ""),
                "block": (r.get("block") or "").strip(),
                "course_code": code, "result_code": rc,
                "credits": _num(r.get("credits")) or 0,
                "mark": mark, "passed": passed,
                "year_of_study": _num(r.get("year_of_study")) or 0,
                "level": code_level(code),
            })
    return by_student


# --- helpers ----------------------------------------------------------------
def _num(v: Any) -> float | None:
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


def _slug(s: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "-", s.upper()).strip("-")[:24] or "PROG"
