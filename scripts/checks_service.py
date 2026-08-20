"""
checks_service.py -- run the completion and ERS checks from the store or a PDF.

Two sources:

  final    the captured record in the database. Completion lists and the ERS
           self-consistency check read from here.

  initial  a raw ERS run kept on file but never ingested into the main tables.
           The ERS check re-parses it on demand, so staff can check the run
           BEFORE capture without letting its provisional codes into the record.

Keeping the database as the single "final" record, while parsing an initial PDF
only when asked, is how the tool stores just the final yet still checks the
initial.
"""
from __future__ import annotations
from typing import Any

from programme_loader import load_programme
import completion as C
import ers_engine as E 
import ers_check as X
from ers_ingest import parse_file


def store_to_parsed(store: Any, programme: str) -> dict[str, list[dict[str, Any]]]:
    """The captured (final) data in the parser's {students, results, decisions} shape."""
    results: list[dict[str, Any]] = []
    for sn, rows in store.results(programme).items():
        results.extend(rows)
    return {"students": store.students(programme),
            "results": results,
            "decisions": store.decisions(programme)}


def results_by_sn(store: Any, programme: str) -> dict[str, list[dict[str, Any]]]:
    return store.results(programme)


def _load_cur(store: Any, programme: str) -> dict[str, Any] | None:
    meta = store.programme(programme) or {}
    yaml_path = meta.get("yaml_path")
    if not yaml_path:
        return None
    try:
        return load_programme(yaml_path)
    except (OSError, ValueError):
        return None


def completion(store: Any, programme: str,
               year: str = "", sem: str = "") -> dict[str, Any]:
    """Degree-complete (DC) and vac-work-only (DGOR) lists.

    `year`/`sem` scope by COMPLETION PERIOD -- the period a student met the last
    requirement -- not by who is active now. `periods` is always the full
    breakdown, so the picker stays put while the lists filter beneath it.
    """
    cur = _load_cur(store, programme)
    if cur is None:
        return {"ready": False, "DC": [], "DGOR": [], "periods": [],
                "summary": {"DC": 0, "DGOR": 0}}
    bio = {r["student_number"]: r for r in store.students(programme)}
    lists = C.completion_lists(cur, store.results(programme), bio)
    dc, dgor = lists["DC"], lists["DGOR"]

    counts: dict[tuple[str, Any], dict[str, int]] = {}
    for tag, rows in (("dc", dc), ("dgor", dgor)):
        for r in rows:
            k = (str(r.get("completed_year") or ""), r.get("completed_semester") or "")
            counts.setdefault(k, {"dc": 0, "dgor": 0})[tag] += 1
    periods = [{"year": y, "semester": s, "dc": v["dc"], "dgor": v["dgor"]}
               for (y, s), v in counts.items() if y]
    periods.sort(key=lambda p: (p["year"], str(p["semester"])), reverse=True)

    if year and sem:
        keep = lambda r: (str(r.get("completed_year")) == year
                          and str(r.get("completed_semester")) == sem)
        dc = [r for r in dc if keep(r)]
        dgor = [r for r in dgor if keep(r)]
    return {"ready": True, "DC": dc, "DGOR": dgor, "periods": periods,
            "summary": {"DC": len(dc), "DGOR": len(dgor)}}


def ers_check(store: Any, programme: str, source: str = "final",
              only: set[str] | None = None) -> dict[str, Any]:
    """Compare registrar codes with the engine's, on the chosen source.

    `only` scopes the report to one cohort: pass the cycle's student numbers and
    the rows and summary cover just those students.
    """
    cur = _load_cur(store, programme)
    if source == "initial":
        doc = store.current_document(programme, "initial")
        if not doc:
            return {"ready": False, "source": source,
                    "error": "no initial ERS on file for this programme",
                    "rows": [], "summary": {}}
        parsed = parse_file(doc["stored_path"], programme)
    else:
        source = "final"
        parsed = store_to_parsed(store, programme)

    report = X.check_parsed(parsed, cur, roster=only)
    if only is not None:
        rows = [r for r in report["rows"] if str(r["student_number"]) in only]
        report = {"rows": rows, "summary": X._summary(rows)}
    return {"ready": True, "source": source, **report}

def student_detail(store: Any, programme: str, sn: str,
                   source: str = "final") -> dict[str, Any]:
    """One student's full record, for auditing a mismatch by hand.

    Returns the engine verdict and its reasons, the credit totals it counted,
    the term-decision history that selects the tree, and every module result by
    period. Same source rule as ers_check: `final` reads the store, `initial`
    re-parses the raw run.
    """
    cur = _load_cur(store, programme)
    if source == "initial":
        doc = store.current_document(programme, "initial")
        if not doc:
            return {"ready": False, "source": source,
                    "error": "no initial ERS on file for this programme"}
        parsed = parse_file(doc["stored_path"], programme)
    else:
        source = "final"
        parsed = store_to_parsed(store, programme)

    sn = str(sn)
    rows = [r for r in parsed["results"] if str(r["student_number"]) == sn]
    if not rows:
        return {"ready": False, "source": source, "student_number": sn,
                "error": "student not found in this source"}
    bio = next((s for s in parsed["students"]
                if str(s["student_number"]) == sn), {})
    sdecs = [d for d in parsed["decisions"] if str(d["student_number"]) == sn]

    # The engine's own view -- same call the cohort check makes.
    dd = X.latest_two_decisions(sdecs).get(sn, {})
    reg = (dd.get("current") or {}).get("code", "")
    prior = (dd.get("prior") or {}).get("code")
    policy = ((cur or {}).get("rules") or {}).get("ers")
    chk = X.check_student(rows, reg, prior, policy)

    # Credit totals as the engine counts them (best attempt per course).
    shaped = X._shape_rows(rows)
    full = E.derive_metrics(shaped, policy, None)
    metrics = full["cumulative"]
    # metrics = E.derive_metrics(shaped, policy, None)["cumulative"]

    # Module record by period, using the engine's pass rule (shaped is 1:1).
    periods: dict[str, dict[str, Any]] = {}
    for raw, sh in zip(rows, shaped):
        p = sh["period"]
        b = periods.setdefault(p, {"period": p, "registered": 0.0,
                                   "passed": 0.0, "modules": []})
        credit = float(raw.get("credits") or 0)
        b["registered"] += credit
        if sh["passed"]:
            b["passed"] += credit
        b["modules"].append({
            "code": raw.get("module_code"), "name": raw.get("module_name"),
            "credits": raw.get("credits"), "mark": raw.get("grade"),
            "result_code": raw.get("result_code"),
            "result": raw.get("result_text"), "passed": sh["passed"]})

    decisions = sorted(
        [{"year": d.get("calendar_year"), "semester": d.get("semester"),
          "code": (d.get("term_code") or "").upper(),
          "text": d.get("term_text") or "", "kind": d.get("kind", "")}
         for d in sdecs],
        key=lambda x: (str(x["year"]), int(x["semester"] or 0)))

    return {"ready": True, "source": source, "student_number": sn,
            "name": f"{bio.get('surname','')}, {bio.get('name','')}".strip(", "),
            "verdict": chk["verdict"], "direction": chk["direction"],
            "registrar_code": chk["registrar_code"],
            "registrar_status": chk["registrar_status"],
            "engine_code": chk["engine_code"], "engine_status": chk["engine_status"],
            "engine_label": chk["engine_label"], "reasons": chk["reasons"],
            "prior_code": prior or "",
            "cumulative_pct": chk["cumulative_pct"],
            "semester_pct": chk["semester_pct"], "period": chk["period"],
            "credits_passed": metrics["credits_passed_to_date"],
            "credits_assessed": metrics["credits_expected_to_date"],
            "semesters_completed": full["history"]["semesters_completed"],   # add
            "thresholds": full["thresholds"],
            "periods": [periods[k] for k in sorted(periods)],
            "decisions": decisions}


def health(store: Any, programme: str, min_enrol: int = 10,
           window: tuple[tuple[int, int], tuple[int, int]] | None = None) -> dict[str, Any]:
    """The programme-health read-out: intake, throughput, time-to-degree, and
    module health. A pure aggregate of the captured record against the
    programme's own rules -- the same "finished" and "blocking" definitions the
    Completion tab and the advice engine use, so nothing here can disagree with
    them. Returns {"ready": False} until the programme's rule file is authored.

    `window` ((y0, s0), (y1, s1)) scopes the read-out to a period for the ECSA
    report; None reads the whole record.
    """
    import programme_health as PH
    cur = _load_cur(store, programme)
    if cur is None:
        return {"ready": False}
    bio = {r["student_number"]: r for r in store.students(programme)}
    return PH.health(cur, store.results(programme), bio,
                     min_enrol=min_enrol, window=window)