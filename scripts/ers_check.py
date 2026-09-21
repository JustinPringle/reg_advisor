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

A row is a MATCH when the two standings agree, a MISMATCH otherwise. One code
is compared below the standing: RISU against ERS-ORANGE-RISU, since RISU and
RISK are both orange but advise different things. Every
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
from standing_codes import status_of, status_of_colour, EXCLUDE_CODES, REVIEW

# The registrar-code -> standing map lives in standing_codes -- one source of
# truth, shared with the badge path -- so the two can never drift. status_of()
# honours a programme's rules.ers.status_of_code override.
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
    key = lambda x: (str(x.get("calendar_year") or ""), int(x.get("semester") or 0))
    by_sn: dict[str, list[dict[str, Any]]] = {}
    for d in decisions:
        by_sn.setdefault(str(d["student_number"]), []).append(d)
    # The run's evaluation period is the newest decision in the file. Only
    # students with a decision at that period are being evaluated this cycle;
    # those whose newest decision is older (graduated, excluded, not proposed)
    # are history-only and must not be checked against a stale code.
    current_period = max((key(d) for d in decisions), default=("", 0))
    out: dict[str, dict[str, Any]] = {}
    for sn, ds in by_sn.items():
        ds.sort(key=key)
        if key(ds[-1]) != current_period:
            continue
        cur = ds[-1]
        before = [d for d in ds if key(d) < current_period]
        prior = before[-1] if before else None
        out[sn] = {
            "current": {"code": (cur.get("term_code") or "").upper(),
                        "text": cur.get("term_text") or "",
                        "year": cur.get("calendar_year"), "sem": cur.get("semester")},
            "prior": ({"code": (prior.get("term_code") or "").upper()} if prior else None)}
    return out

def _dec_key(d: dict[str, Any]) -> tuple[str, int]:
    return (str(d.get("calendar_year") or ""), int(d.get("semester") or 0))


def latest_decision_by_sn(decisions: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Each student's newest decision, whatever its period -- the history input
    for a student the current run did not propose a code for."""
    out: dict[str, dict[str, Any]] = {}
    for d in decisions:
        sn = str(d["student_number"])
        if sn not in out or _dec_key(d) > _dec_key(out[sn]):
            out[sn] = d
    return out


def _int(v: Any) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def _colours_by_sn(colours: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Colour rows grouped per student, oldest period first."""
    out: dict[str, list[dict[str, Any]]] = {}
    for c in colours:
        out.setdefault(str(c["student_number"]), []).append(
            {"year": str(c.get("calendar_year") or ""), "sem": _int(c.get("semester")),
             "colour": (c.get("colour") or "").lower()})
    for rows in out.values():
        rows.sort(key=lambda r: (r["year"], r["sem"]))
    return out


def _colour_around(rows: list[dict[str, Any]], year: str,
                   sem: int) -> tuple[str, dict[str, Any] | None]:
    """(colour for this period, the period before it) from one student's rows."""
    here = next((r for r in rows if r["year"] == year and r["sem"] == sem), None)
    before = [r for r in rows if (r["year"], r["sem"]) < (year, sem)]
    return ((here or {}).get("colour", ""), before[-1] if before else None)


def _run_period(decisions: list[dict[str, Any]]) -> tuple[str, int]:
    """The cycle under evaluation: the newest decision period in the file."""
    return max((_dec_key(d) for d in decisions), default=("", 0))


_SEM_OF_BLOCK = {"0": 2, "1": 1, "S1": 1, "2": 2, "S2": 2, "S3": 2, "S4": 2}


def _period_key(period: str) -> tuple[str, int]:
    """"2025:S1" -> ("2025", 1). A supplementary block belongs to the semester
    it supplements, the same rule the engine and the cohort picker use."""
    year, _, block = str(period or "").rpartition(":")
    return (year, _SEM_OF_BLOCK.get(block.strip().upper(), 0))


def _summary(rows: list[dict[str, Any]]) -> dict[str, int]:
    from collections import Counter
    c = Counter(r["verdict"] for r in rows)
    return {"total": len(rows), "match": c.get("match", 0),
            "mismatch": c.get("mismatch", 0), "review": c.get("review", 0),
            "engine_only": c.get("engine-only", 0)}


# Incoming (prior) term codes normalised before the trees read them. A RISU
# student sits semester 2 out and re-enters on RISK (Justin, 2026-09-18).
INCOMING_ALIASES: dict[str, str] = {"RISU": "RISK"}

def _incoming_alias(code: str) -> str:
    return INCOMING_ALIASES.get((code or "").upper(), (code or "").upper())

# --- the check ---------------------------------------------------------------
def check_student(rows: list[dict[str, Any]], registrar_code: str,
                  prior_code: str | None = None,
                  policy: dict[str, Any] | None = None,
                  registrar_colour: str | None = None,
                  prior_colour: str | None = None,
                  degree_complete: bool = False) -> dict[str, Any]:
    """Compare one student's ERS standing with the engine's calculation.

    The registrar states a standing two ways and both are read. A term code
    (RISK, PROB, ...) is written only when something needs saying; the colour
    block states a colour for every period, including the good ones. So the code
    is used when there is one and the colour answers otherwise -- for the current
    period and for the prior one the engine reads as its history.
    """
    shaped = _shape_rows(rows)
    prior_code = _incoming_alias(prior_code)          # normalise before use
    prior_status = status_of(prior_code, policy) if prior_code \
        else status_of_colour(prior_colour or "", policy)
    hist = {"last_status": prior_status if prior_status != REVIEW else "none",
            "degree_complete": bool(degree_complete),
            "appeals_exhausted": (registrar_code or "").upper() in EXCLUDE_CODES}
    metrics = E.derive_metrics(shaped, policy, hist)
    ers = E.classify(metrics, policy=policy)

    reg_code = (registrar_code or "").upper()
    eng_status = ers["status"]

    colour = (registrar_colour or "").lower()
    if not reg_code and not colour:
        # Neither a code nor a colour this cycle: nothing to line the engine up
        # against. Classify anyway so the student is seen rather than dropped.
        reg_status, verdict, direction = "none", "engine-only", "no registrar standing"
        source = "none"
    else:
        source = "code" if reg_code else "colour"
        reg_status = status_of(reg_code, policy) if reg_code \
            else status_of_colour(colour, policy)
        if reg_status == REVIEW or eng_status == "unknown":
            verdict, direction = "review", "unclassifiable"
        elif reg_status == eng_status:
            verdict, direction = "match", "same"
        else:
            verdict = "mismatch"
            direction = ("engine stricter" if _RANK.get(eng_status, 0) > _RANK.get(reg_status, 0)
                         else "engine more lenient")
        # RISU and RISK share a standing, so the standing test cannot see them
        # differ. Where the programme authors the RISU rule, compare the code
        # too: RISU (suspend) is the stricter advice of the two.
        eng_risu = ers["code"] == "ERS-ORANGE-RISU"
        if verdict == "match" and (policy or {}).get("risu") \
                and eng_risu != (reg_code == "RISU"):
            verdict = "mismatch"
            direction = "engine stricter" if eng_risu else "engine more lenient"
    return {"registrar_code": reg_code, "registrar_status": reg_status,
            "registrar_colour": colour, "registrar_source": source,
            "prior_status": hist["last_status"],
            "engine_code": ers["code"], "engine_status": eng_status,
            "engine_label": ers["label"], "verdict": verdict, "direction": direction,
            "cumulative_pct": round(metrics["cumulative"]["credit_pct_passed"] * 100),
            "semester_pct": round(metrics["semester"]["credit_pct_passed"] * 100),
            "period": metrics["semester"]["period"],
            # Guide point 1: failed every first-semester module. Coded RISK, but
            # counselled toward RISU -- advice for a person, not a mismatch.
            "counsel": metrics["history"]["semesters_registered"] <= 1
                       and metrics["first_semester"]["failed_all"],
            "reasons": ers["reasons"]}


def check_parsed(parsed: dict[str, list[dict[str, Any]]],
                 cur: dict[str, Any] | None = None,
                 policy: dict[str, Any] | None = None,
                 roster: set[str] | None = None,
                 complete: set[str] | None = None) -> dict[str, Any]:
    """Run the check over a parsed ERS ({students, results, decisions}).

    `cur` is optional -- the standing check needs only results and decisions,
    not the prerequisite rules -- but when given, its policy overrides feed the
    engine so a programme with its own cut points is honoured.

    `complete` is the set of students who have finished the degree, computed by
    the caller from completion.py. Progression is not assessed for them.

    `roster` is the active cohort. When given, every student in it is classified,
    not only those the registrar proposed a code for this cycle: a student with
    no current proposal is still run through the engine and reported `engine-only`
    so the whole active cohort is seen, not the code-bearing subset.
    """
    policy = policy or ((cur or {}).get("rules") or {}).get("ers")
    bio = {str(s["student_number"]): s for s in parsed.get("students", [])}
    by_sn: dict[str, list[dict[str, Any]]] = {}
    for r in parsed.get("results", []):
        by_sn.setdefault(str(r["student_number"]), []).append(r)
    decs = parsed.get("decisions", [])
    decisions = latest_two_decisions(decs)          # current-period proposals
    latest_any = latest_decision_by_sn(decs)        # newest decision, any period
    run_year, run_sem = _run_period(decs)
    colours = _colours_by_sn(parsed.get("colours") or [])
    codes_by_period = {(str(d["student_number"]), str(d.get("calendar_year") or ""),
                        _int(d.get("semester"))): (d.get("term_code") or "")
                       for d in decs}

    # Anyone the registrar stated a standing for this period, plus the roster.
    # The colour block covers the students who carry no code -- leaving them out
    # would hide every student in good standing from the check.
    stated = {sn for sn, rows in colours.items()
              if any(r["year"] == str(run_year) and r["sem"] == _int(run_sem) for r in rows)}

    # Every period the registrar stated ANY standing for, per student, oldest
    # first. A student registered this cycle but not evaluated in it -- a
    # finalist carrying one second-semester module, a readmit who sat the first
    # semester out -- has no standing at the run period and would otherwise
    # drop out of the report unseen. They are checked at their own last stated
    # period instead, which is the period the engine is reading anyway.
    stated_periods: dict[str, set[tuple[str, int]]] = {}
    for (sn, y, sem) in codes_by_period:
        stated_periods.setdefault(sn, set()).add((y, sem))
    for sn, rows in colours.items():
        for r in rows:
            stated_periods.setdefault(sn, set()).add((r["year"], r["sem"]))

    # Registered at or after the run period AND carrying a standing from an
    # earlier one: still on the books, and there is something to check them
    # against. A student with no stated standing anywhere has nothing to
    # compare, and stays with the roster path that reports them engine-only.
    active = {sn for sn, rows in by_sn.items()
              if sn in stated_periods
              and any((str(r.get("calendar_year") or ""),
                       _SEM_OF_BLOCK.get(str(r.get("block") or "").strip().upper(), 0))
                      >= (str(run_year), _int(run_sem)) for r in rows)}
    targets = (set(decisions) | stated | active
               | (set(roster) if roster is not None else set()))

    rows: list[dict[str, Any]] = []
    for sn in targets:
        if sn not in by_sn:
            continue
        b = bio.get(sn, {})
        name = f"{b.get('surname','')}, {b.get('name','')}".strip(", ")
        dd = decisions.get(sn)
        if dd:
            reg_code = dd["current"]["code"]
            prior = (dd["prior"] or {}).get("code")
            year, sem = dd["current"]["year"], dd["current"]["sem"]
        else:                                        # active, no proposal this cycle
            reg_code = ""
            prior = (latest_any.get(sn) or {}).get("term_code")
            year, sem = run_year, run_sem
        # The colour for this period, and the standing of the period before it.
        # The colour block runs to the current period for every student, so the
        # period immediately before is the right history input. It REPLACES the
        # newest-decision fallback rather than deferring to it: a student's last
        # code can be years old, and reading a stale RISK as this term's history
        # holds a long-recovered student down.
        this_colour, prev = _colour_around(colours.get(sn, []), str(year), _int(sem))
        # No standing at the run period: fall back to the student's own last
        # stated one rather than dropping them.
        back = None
        if not reg_code and not this_colour:
            past = sorted(p for p in stated_periods.get(sn, set())
                          if p < (str(year), _int(sem)))
            if past:
                back = past[-1]
                year, sem = back
                reg_code = codes_by_period.get((sn, year, sem), "")
                this_colour, prev = _colour_around(colours.get(sn, []), year, sem)
        if prev:
            prior = codes_by_period.get((sn, prev["year"], prev["sem"]), "")
        chk = check_student(by_sn[sn], reg_code, prior, policy,
                            registrar_colour=this_colour,
                            prior_colour=(prev or {}).get("colour"),
                            degree_complete=sn in (complete or set()))
        # A back-period check is only honest when the engine is reading that
        # same period. If it is not, the comparison would put an old code
        # against a newer verdict -- exactly the stale read this avoids -- so
        # it goes to a person instead.
        if back and _period_key(chk["period"]) != back:
            chk = {**chk, "verdict": "review",
                   "direction": f"no standing at {run_year}:{run_sem}; "
                                f"last stated {back[0]}:{back[1]}"}
        rows.append({"student_number": sn, "name": name,
                     "year": year, "semester": sem,
                     "checked_at_run_period": back is None, **chk})

    # Order by what a person must action: real disagreements first, then the
    # unclassifiable, then engine-only students the engine flags (green last).
    order = {"mismatch": 0, "review": 1, "engine-only": 2, "match": 3}
    rows.sort(key=lambda r: (order.get(r["verdict"], 9),
                             r["engine_status"] == "green",
                             r["name"].lower()))
    return {"rows": rows, "summary": _summary(rows)}


# --- export ------------------------------------------------------------------
_FIELDS = ["student_number", "name", "year", "semester", "verdict", "direction",
           "registrar_code", "registrar_colour", "registrar_source", "registrar_status", "engine_code", "engine_status",
           "engine_label", "cumulative_pct", "semester_pct", "period", "counsel"]


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
