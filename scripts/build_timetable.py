#!/usr/bin/env python3
"""Build catalogue/timetable.yaml from the School's semester timetable workbooks.

Input: the Civil Engineering timetable the School issues each semester, one
workbook per semester (e.g. Civ_Eng_Ttable_2nd_Semester_2026V2.xlsx). Three
sheets are read:

    TimeTable          the grid: 11 periods x 5 days, one row per year of study
    Blocking (CODES)   module codes by year and lecture block (A-F)
    Blocking           the lecture names the grid cells show, same layout

Output: for each semester, every module the grid places, with its lecture block
and the exact periods it meets:

    semesters:
      2:
        GEOL215: {block: F, periods: [Fri3, Mon2, Mon3, Wed3, Wed4, Wed5]}

Two modules clash when they share a period. The periods, not the block letter,
are the test, because the grid hard-codes some lectures outside their block
(2nd-year Survey sits in Friday period 1, which is block A). The block letter is
kept for messages only.

What is read, and what is not:
  * Lectures only. A cell marked (P), (T) or (P/T) is a practical or tutorial,
    run in rotating groups, and is not a hard clash. (L) and (L/T) count.
  * Years 2-4 map a grid name to a code through the two blocking sheets, which
    share one layout: the name at (year, block) in "Blocking" is the module at
    the same cell in "Blocking (CODES)". A cell that itself contains a module
    code (e.g. "Elective ENEL4EE") maps to that code directly.
  * Year 1 modules are not coded in the blocking sheets, so FIRST_YEAR_NAMES
    below maps their grid names. Their block is the letter most of their
    lecture periods carry.
  * OPTION, SEMINAR, FORUM and the like are not modules and are skipped. Any
    other name that maps to no code is listed in meta.warnings, as is any
    disagreement between the two blocking sheets.

Usage (from the repo root):
    python scripts/build_timetable.py \
        data/timetables/Civ_Eng_Ttable_1st_semester_2026V2.xlsx:1 \
        data/timetables/Civ_Eng_Ttable_2nd_Semester_2026V2.xlsx:2 \
        --year 2026 --alias ENCV3GA=ENCV3G1 --out catalogue/timetable.yaml

--alias corrects a code the timetabler wrote differently from ITS; each one is
recorded in the output's meta so the correction stays on the record.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import os
import re
import sys
import warnings
from collections import Counter

import openpyxl
import yaml

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri"]
CODE_RE = re.compile(r"\b([A-Z]{4}\d[A-Z0-9]{2})\b")
YEAR_RE = re.compile(r"^\s*(\d)\s*(?:ST|ND|RD|R|TH)", re.I)   # "3R YRD" is in the sheets
NOT_A_MODULE = {"", "-", "option", "seminar", "forum", "lunch", "free",
                "elective", "x"}

# Year-1 lecture names as the grid prints them -> ITS code. The blocking sheets
# carry no first-year codes, so this is the only hand-kept part of the build.
FIRST_YEAR_NAMES = {
    "physics 1a": "PHYS151", "physics 1b": "PHYS152",
    "applied maths 1a": "MATH132", "applied maths 1b": "MATH142",
    "maths 1a": "MATH131", "maths 1b": "MATH141", "math 1b": "MATH141",
    "chem 1a": "CHEM181", "chem 1b": "CHEM191",
    "tech comm": "ENCH1TC", "eng dwg": "ENME1DR",
    "critical social justice": "EDSJ1CS",
    "intro to civ design": "ENCV1ED", "intro to civil design": "ENCV1ED",
    "intro to eng materials": "ENME1EM", "intro to materials": "ENME1EM",
}


def norm(name: object) -> str:
    """Lower-case, single-spaced, without the trailing '/' the sheets use to
    mark a shared slot ("Maths Sys/" and "Maths Sys" are one module)."""
    s = re.sub(r"\s+", " ", str(name or "")).strip().lower()
    s = re.sub(r"\s*&\s*", "&", s.replace(".", ""))   # "Ground & Structures", "Constr. Contracts"
    return s.rstrip("/ ").strip()


def kind(name: object) -> tuple[str, bool]:
    """(name without its bracket, is_lecture). (P), (T) and (P/T) are group
    sessions; (L), (L/T) and no bracket are lectures."""
    s = norm(name)
    m = re.search(r"\(([^)]*)\)\s*$", s)
    if not m:
        return s, True
    tag = m.group(1).replace(" ", "")
    return s[:m.start()].strip().rstrip("/ ").strip(), "l" in tag


def year_rows(ws) -> dict[int, list[object]]:
    """{year: [cell A..F]} for the rows labelled '1ST YR'..'4TH YR' in col G.
    The first row found for a year wins (the CODES sheets repeat the labels
    further right on a shifted row, which is a lookup aid, not data)."""
    out: dict[int, list[object]] = {}
    for row in ws.iter_rows(min_row=1, max_row=12, max_col=7, values_only=True):
        m = YEAR_RE.match(str(row[6] or ""))
        if m and int(m.group(1)) not in out:
            out[int(m.group(1))] = list(row[:6])
    return out


def name_map(wb, warn: list[str], sem: int) -> tuple[dict[tuple[int, str], str],
                                                      dict[str, str]]:
    """({(year, grid name): code}, {code: block}) from the two blocking sheets."""
    codes = year_rows(wb["Blocking (CODES)"])
    names = year_rows(wb["Blocking"])
    nm: dict[tuple[int, str], str] = {}
    block: dict[str, str] = {}
    for yr in sorted(set(codes) | set(names)):
        crow, nrow = codes.get(yr, [None] * 6), names.get(yr, [None] * 6)
        spare_codes, spare_names = [], []
        for i, letter in enumerate("ABCDEF"):
            c = CODE_RE.findall(str(crow[i] or "").upper())
            n = norm(nrow[i])
            if c and n not in NOT_A_MODULE:
                nm[(yr, n)] = c[0]
                block[c[0]] = letter
            elif c:
                spare_codes.append((letter, c[0]))
            elif n not in NOT_A_MODULE:
                spare_names.append((letter, n))
        # One code and one name left over in a row: the two sheets disagree on
        # which block the module sits in. Pair them, keep the CODES block, and
        # say so -- the timetabler has to settle it.
        if len(spare_codes) == 1 and len(spare_names) == 1:
            (cl, c), (nl, n) = spare_codes[0], spare_names[0]
            nm[(yr, n)] = c
            block[c] = cl
            warn.append(f"S{sem} Y{yr}: {c} is block {cl} on 'Blocking (CODES)' but "
                        f"'{n}' is block {nl} on 'Blocking'; used {cl}")
        else:
            for cl, c in spare_codes:
                block[c] = cl
                warn.append(f"S{sem} Y{yr}: {c} (block {cl}) has no lecture name on 'Blocking'")
            for nl, n in spare_names:
                warn.append(f"S{sem} Y{yr}: '{n}' (block {nl}) has no code on 'Blocking (CODES)'")
    return nm, block


def read_semester(path: str, sem: int, aliases: dict[str, str],
                  warn: list[str]) -> dict[str, dict[str, object]]:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")      # the workbooks carry unparseable headers
        wb = openpyxl.load_workbook(path, data_only=True)
    nm, block = name_map(wb, warn, sem)
    ws = wb["TimeTable"]
    periods: dict[str, set[str]] = {}
    letters: dict[str, Counter] = {}
    unmapped: set[str] = set()
    period = None
    day_letter: list[object] = [None] * 5
    for row in ws.iter_rows(min_row=3, values_only=True):
        a, yr = row[0], row[1]
        if isinstance(a, str) and a.strip().lower().startswith("notes"):
            break
        if not isinstance(yr, int):
            continue
        if yr == 1 and isinstance(a, int):
            period = a
            day_letter = [row[2 + 4 * d] for d in range(5)]
        if period is None:
            continue
        for d in range(5):
            cell = row[3 + 4 * d]
            n, lecture = kind(cell)
            if n in NOT_A_MODULE or not lecture:
                continue
            found = CODE_RE.findall(str(cell).upper())
            code = (found[0] if found else
                    nm.get((yr, n)) or (FIRST_YEAR_NAMES.get(n) if yr == 1 else None))
            if not code:
                unmapped.add(f"Y{yr} '{n}'")
                continue
            code = aliases.get(code, code)
            periods.setdefault(code, set()).add(f"{DAYS[d]}{period}")
            lt = str(day_letter[d] or "").strip().upper()
            if lt in tuple("ABCDEF"):
                letters.setdefault(code, Counter())[lt] += 1
    for u in sorted(unmapped):
        warn.append(f"S{sem} {u}: on the grid but maps to no code (skipped)")
    out: dict[str, dict[str, object]] = {}
    for code in sorted(periods):
        raw = next((k for k, v in aliases.items() if v == code), code)
        b = block.get(raw) or block.get(code)
        if not b and letters.get(code):
            b = letters[code].most_common(1)[0][0]
        out[code] = {"block": b, "periods": sorted(periods[code], key=_pkey)}
    for raw in block:
        code = aliases.get(raw, raw)
        if code not in out:
            warn.append(f"S{sem} {code}: coded in block {block[raw]} but has no lecture on the grid")
    return out


def _pkey(p: str) -> tuple[int, int]:
    return DAYS.index(p[:3]), int(p[3:])


def check_against_catalogue(sems: dict[int, dict[str, object]], cat_path: str,
                            warn: list[str]) -> None:
    if not os.path.exists(cat_path):
        return
    with open(cat_path, encoding="utf-8") as fh:
        known = set((yaml.safe_load(fh) or {}).get("modules") or {})
    for sem, mods in sems.items():
        for code in mods:
            if code not in known:
                warn.append(f"S{sem} {code}: not in catalogue/modules.yaml -- a timetable "
                            f"typo? correct it with --alias {code}=<ITS code>")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("workbooks", nargs="+", help="path.xlsx:SEM, one per semester")
    ap.add_argument("--year", type=int, required=True, help="timetable calendar year")
    ap.add_argument("--alias", action="append", default=[], metavar="OLD=NEW")
    ap.add_argument("--catalogue", default="catalogue/modules.yaml")
    ap.add_argument("--out", default="catalogue/timetable.yaml")
    args = ap.parse_args(argv)

    aliases = dict(a.split("=", 1) for a in args.alias)
    warn: list[str] = []
    sems: dict[int, dict[str, object]] = {}
    sources = []
    for spec in args.workbooks:
        path, _, sem = spec.rpartition(":")
        if not path or sem not in ("1", "2"):
            ap.error(f"{spec}: expected path.xlsx:1 or path.xlsx:2")
        sems[int(sem)] = read_semester(path, int(sem), aliases, warn)
        sources.append(os.path.basename(path))
    check_against_catalogue(sems, args.catalogue, warn)

    doc = {"meta": {"calendar_year": args.year,
                    "generated": _dt.date.today().isoformat(),
                    "source": sources,
                    "basis": "lecture periods on the School timetable grid; "
                             "(P)/(T) group sessions excluded",
                    "aliases": aliases or {},
                    "warnings": warn},
           "semesters": sems}
    header = ("# timetable.yaml -- lecture periods per module, per semester.\n"
              "#\n"
              "# Two modules clash when they share a period. Read by\n"
              "# programme_loader.load_timetable and regadvisor_engine.find_clashes.\n"
              "#\n"
              "# GENERATED FILE -- do not hand-edit. Regenerate with:\n"
              "#   python scripts/build_timetable.py <S1.xlsx>:1 <S2.xlsx>:2 \\\n"
              f"#       --year {args.year}"
              + "".join(f" --alias {k}={v}" for k, v in aliases.items())
              + " --out catalogue/timetable.yaml\n\n")
    body = yaml.safe_dump(doc, sort_keys=False, default_flow_style=None, width=100)
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(header + body)
    n = sum(len(v) for v in sems.values())
    print(f"wrote {args.out}: {n} module-semesters, {len(warn)} warnings")
    for w in warn:
        print("  warn:", w)
    return 0


if __name__ == "__main__":
    sys.exit(main())
