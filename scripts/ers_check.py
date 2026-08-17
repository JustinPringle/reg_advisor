"""
ers_check.py -- check the ERS run's own code against the engine's.

After exams, ITS runs the ERS and prints a PDF: against each student it
proposes a term-decision code (RISK, RSK2, PROB, FPRR, FPMA, XNFA, CO). Staff
then read every one, correct the wrong ones by hand, and only then capture the
result and print the final PDF. This module automates the reading: for each
student it computes the code the engine would assign and lines it up against the
ERS's proposal, so a person checks the handful that disagree instead of all of
them.

Two codes live in two vocabularies, so the honest comparison is at the shared
standing level -- green / orange / red / exclude:

    registrar RISK/RSK2 -> orange     engine ERS-ORANGE-* -> orange
    registrar PROB/FPRR/FPMA -> red   engine ERS-RED-*     -> red
    registrar CO/GREEN/BLUE -> green  engine ERS-GREEN     -> green
    registrar XNFA/XACA/XAC -> exclude engine ERS-EXCLUDE  -> exclude

A row is a MATCH when the two standings agree, a MISMATCH otherwise. Every
mismatch is surfaced with the engine's own reasons and exported for the manual
pass -- nothing is auto-changed. The engine reads the PRIOR term's code as its
history input, taken from the second-latest decision in the same ERS.

    from ers_ingest import parse_file
    parsed = parse_file("ENG-CV_ERS_initial.pdf", "ENG-CIVIL")
    rep = check_parsed(parsed, cur)          # cur = load_programme(...)
    export_mismatches(rep, "data/ers_mismatches_ENG-CIVIL.csv")

Pure apart from the caller's file read. Standard library only.
"""
from __future__ import annotations
from typing import Any
import csv
from pathlib import Path

import ers_engine as E
import regadvisor_engine as R

# Registrar term code -> shared standing. Mirrors datasource_sqlite.STATUS_OF_CODE;
# kept here so the checker stands alone and can be unit-tested in isolation.
STATUS_OF_CODE: dict[str, str] = {
    "CO": "green", "GREEN": "green", "BLUE": "green",
    "RISK": "orange", "RSK2": "orange",
    "PROB": "red", "FPRR": "red", "FPRD": "red", "FPMA": "red", "FPDS": "red",
    "XNFA": "exclude", "XACA": "exclude", "XAC": "exclude",
}
_EXCLUDE = {"XNFA", "XACA", "XAC"}
PASS_CODES = {"P", "PM"}
_RANK = {"green": 0, "orange": 1, "red": 2, "exclude": 3}


# --- shape adapters ---------------------------------------------------------
def _shape_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Parser/DB result rows -> the engine's row shape (period, passed, ...)."""
    out = []
    for r in rows:
        code = str(r.get("module_code") or r.get("course_code") or "").strip()
        rc = str(r.get("result_code") or "").strip().upper()
        mark = r.get("grade", r.get("mark"))
        try:
            mark = float(mark)
        except (TypeError, ValueError):
            mark = None
        passed = (rc in PASS_CODES) or (not rc and mark is not None and mark >= 50)
        out.append({
            "student_number": r.get("student_number"),
            "period": f"{r.get('calendar_year','')}:{r.get('block','')}",
            "calendar_year": r.get("calendar_year", ""),
            "block": str(r.get("block") or "").strip(),
            "course_code": code, "result_code": rc,
            "credits": r.get("credits") or 0, "mark": mark, "passed": passed,
            "year_of_study": r.get("year_of_study") or 0,
            "level": R.code_level(code)})
    return out


def latest_two_decisions(decisions: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Per student: the CURRENT proposal (to check) and the PRIOR one (history).

    Sorts each student's decisions by (calendar_year, semester); the last is the
    proposal under review, the one before it feeds the engine's history input.
    """
    by_sn: dict[str, list[dict[str, Any]]] = {}
    for d in decisions:
        by_sn.setdefault(str(d["student_number"]), []).append(d)
    out: dict[str, dict[str, Any]] = {}
    for sn, ds in by_sn.items():
        ds.sort(key=lambda x: (str(x.get("calendar_year") or ""),
                               int(x.get("semester") or 0)))
        cur = ds[-1]
        prior = ds[-2] if len(ds) > 1 else None
        out[sn] = {
            "current": {"code": (cur.get("term_code") or "").upper(),
                        "text": cur.get("term_text") or "",
                        "year": cur.get("calendar_year"), "sem": cur.get("semester")},
            "prior": ({"code": (prior.get("term_code") or "").upper()} if prior else None)}
    return out


# --- the check ---------------------------------------------------------------
def check_student(rows: list[dict[str, Any]], registrar_code: str,
                  prior_code: str | None = None,
                  policy: dict[str, Any] | None = None) -> dict[str, Any]:
    """Compare one student's ERS proposal with the engine's calculation."""
    shaped = _shape_rows(rows)
    hist = {"last_status": STATUS_OF_CODE.get((prior_code or "").upper(), "none"),
            "appeals_exhausted": (registrar_code or "").upper() in _EXCLUDE}
    metrics = E.derive_metrics(shaped, policy, hist)
    ers = E.classify(metrics, policy=policy)

    reg_code = (registrar_code or "").upper()
    reg_status = STATUS_OF_CODE.get(reg_code, "unknown")
    eng_status = ers["status"]

    if reg_status == "unknown" or eng_status == "unknown":
        verdict, direction = "review", "unclassifiable"
    elif reg_status == eng_status:
        verdict, direction = "match", "same"
    else:
        verdict = "mismatch"
        direction = ("engine stricter" if _RANK.get(eng_status, 0) > _RANK.get(reg_status, 0)
                     else "engine more lenient")
    return {"registrar_code": reg_code, "registrar_status": reg_status,
            "engine_code": ers["code"], "engine_status": eng_status,
            "engine_label": ers["label"], "verdict": verdict, "direction": direction,
            "cumulative_pct": round(metrics["cumulative"]["credit_pct_passed"] * 100),
            "semester_pct": round(metrics["semester"]["credit_pct_passed"] * 100),
            "period": metrics["semester"]["period"],
            "reasons": ers["reasons"]}


def check_parsed(parsed: dict[str, list[dict[str, Any]]],
                 cur: dict[str, Any] | None = None,
                 policy: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run the check over a parsed ERS ({students, results, decisions}).

    `cur` is optional -- the standing check needs only results and decisions,
    not the prerequisite rules -- but when given, its policy overrides feed the
    engine so a programme with its own cut points is honoured.
    """
    policy = policy or ((cur or {}).get("rules") or {}).get("ers")
    bio = {str(s["student_number"]): s for s in parsed.get("students", [])}
    by_sn: dict[str, list[dict[str, Any]]] = {}
    for r in parsed.get("results", []):
        by_sn.setdefault(str(r["student_number"]), []).append(r)
    decisions = latest_two_decisions(parsed.get("decisions", []))

    rows: list[dict[str, Any]] = []
    for sn, dd in decisions.items():
        if sn not in by_sn:
            continue
        b = bio.get(sn, {})
        name = f"{b.get('surname','')}, {b.get('name','')}".strip(", ")
        chk = check_student(by_sn[sn], dd["current"]["code"],
                            (dd["prior"] or {}).get("code"), policy)
        rows.append({"student_number": sn, "name": name,
                     "year": dd["current"]["year"], "semester": dd["current"]["sem"],
                     **chk})

    rows.sort(key=lambda r: (r["verdict"] != "mismatch", r["verdict"] != "review",
                             r["name"].lower()))
    summary = {"total": len(rows),
               "match": sum(1 for r in rows if r["verdict"] == "match"),
               "mismatch": sum(1 for r in rows if r["verdict"] == "mismatch"),
               "review": sum(1 for r in rows if r["verdict"] == "review")}
    return {"rows": rows, "summary": summary}


# --- export ------------------------------------------------------------------
_FIELDS = ["student_number", "name", "year", "semester", "verdict", "direction",
           "registrar_code", "registrar_status", "engine_code", "engine_status",
           "engine_label", "cumulative_pct", "semester_pct", "period"]


def export_mismatches(report: dict[str, Any], path: str,
                      include_review: bool = True) -> Path:
    """Write the rows a person must action -- mismatches (and review cases)."""
    wanted = {"mismatch"} | ({"review"} if include_review else set())
    rows = [r for r in report["rows"] if r["verdict"] in wanted]
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    return out


def main() -> None:
    import sys
    from ers_ingest import parse_file
    path = sys.argv[1] if len(sys.argv) > 1 else "../data/ENG-CV_ERS.pdf"
    programme = sys.argv[2] if len(sys.argv) > 2 else "ENG-CIVIL"
    parsed = parse_file(path, programme)
    rep = check_parsed(parsed)
    s = rep["summary"]
    print(f"ERS check {path}: {s['total']} students -> "
          f"{s['match']} match, {s['mismatch']} mismatch, {s['review']} review")
    for r in rep["rows"]:
        if r["verdict"] == "mismatch":
            print(f"  {r['student_number']} {r['name']:26} "
                  f"registrar {r['registrar_code']:5}({r['registrar_status']}) vs "
                  f"engine {r['engine_status']:7} [{r['direction']}]")


if __name__ == "__main__":
    main()
