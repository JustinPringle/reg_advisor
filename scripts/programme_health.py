"""
programme_health.py -- the health of a programme, for accreditation and review.

A pure read-out of the captured record against the programme's own rules. It
answers the questions a coordinator (and ECSA) actually ask of a programme:

  intake        how many students entered each year.
  throughput    of an intake cohort, how many finished, and in how long -- the
                DHET-style cohort table (regulation time, +1yr, +2yr, still
                here, gone) that accreditation asks for.
  time-to-degree the spread of how long finishers took, in semesters.
  module health which modules stall the most students (the gatekeepers), and a
                full per-module record: enrolment, pass rate, mean, repeats, and
                the blocking factor -- how many later modules each one gates.

Two definitions are borrowed wholesale so this page can never disagree with the
rest of the tool. "Finished" is completion.completion_lists -- the same DC/DGOR
the Completion tab shows. "Blocking factor" is regadvisor_engine.blocking_factor
-- the same downstream count the advice engine already computes. Nothing here
invents a threshold; every number is either counted from the record or read from
the programme file.

No I/O, no DOM: (curriculum, results, bio) -> a dict ready for JSON.

OPEN DECISIONS surfaced, not resolved (see health()['decisions']):
  * intake year = a student's earliest result year. For an articulation student
    who spent a year under an augmented code, that earliest year is the augmented
    entry, which may or may not be the intake you want counted here.
  * regulation length reads programme.regulation_years (default 4). Augmented
    Civil is five years; set it in that programme file.
  * a supplementary sitting (S1/S2) is attributed to the semester it supplements,
    following the completion module's own rule.
"""
from __future__ import annotations
from typing import Any
from collections import defaultdict
from math import ceil

import regadvisor_engine as R
import completion as C
from datasource_sqlite import semester_of_block

# Result codes, keyed the way the rest of the tool keys them. Mainstream Civil:
# a coded pass is P/PM; a coded fail is F (Fail) or FA (Fail, no admission to
# exam). Everything else with a code -- FS (supp granted), DE (deferred), F/
# (continuing) -- is a live, not-yet-settled outcome, counted as "pending" so it
# neither flatters nor damns a module's pass rate. An uncoded row with a mark is
# read on the 50 pass mark, as the transcript index does.
PASS_CODES = {"P", "PM"}
FAIL_CODES = {"F", "FA"}
DEFAULT_MIN_ENROL = 10          # a module needs this many results to be ranked
DEFAULT_REGULATION_YEARS = 4    # a mainstream BScEng; augmented sets 5 in its file


# --- small helpers ----------------------------------------------------------
def _yr(v: Any) -> int | None:
    s = str(v).strip()
    return int(s) if s.isdigit() else None


def _code_of(r: dict[str, Any]) -> Any:
    """The module code, whichever shape the row is in. Engine rows carry
    `course_code`/`code`; a raw store row carries `module_code`. Reading only one
    would silently drop every row of the other shape (module stats went blank
    when fed store rows directly -- caught in test)."""
    return r.get("course_code") or r.get("code") or r.get("module_code")


def _row_outcome(r: dict[str, Any]) -> str:
    """One result row -> 'pass' | 'fail' | 'pending' | 'ungraded'.

    'ungraded' is a registration with neither a code nor a mark -- an in-progress
    module, not yet an attempt; it is left out of enrolment counts entirely.
    """
    rc = str(r.get("result_code") or "").strip().upper()
    mark = r.get("mark", r.get("grade"))
    if not rc and mark is None:
        return "ungraded"
    if rc in PASS_CODES:
        return "pass"
    if rc in FAIL_CODES:
        return "fail"
    if not rc:
        try:
            return "pass" if float(mark) >= 50 else "fail"
        except (TypeError, ValueError):
            return "pending"
    return "pending"


def _first_cycle(rows: list[dict[str, Any]]) -> tuple[int, int] | None:
    """A student's earliest (year, semester) with a result row."""
    cycles = []
    for r in rows:
        y = _yr(r.get("calendar_year"))
        s = semester_of_block(r.get("block"))
        if y and s:
            cycles.append((y, s))
    return min(cycles) if cycles else None


def _active_years(rows: list[dict[str, Any]]) -> set[int]:
    return {y for r in rows if (y := _yr(r.get("calendar_year"))) is not None}


def _elapsed_semesters(intake: tuple[int, int], done: tuple[int, int]) -> int:
    """Inclusive semester count from intake to completion. Intake 2021 S1 to
    completion 2024 S2 is eight semesters -- the regulation length of a 4-year
    degree, and the shape of the coordinator's own example."""
    (iy, isem), (cy, csem) = intake, done
    return (cy - iy) * 2 + (csem - isem) + 1


# --- intake -----------------------------------------------------------------
def intake_years(results_by_sn: dict[str, list[dict[str, Any]]]) -> dict[str, int]:
    """Each student's intake year: the earliest year they hold a result. See the
    module docstring for the articulation caveat this carries."""
    out: dict[str, int] = {}
    for sn, rows in results_by_sn.items():
        ys = _active_years(rows)
        if ys:
            out[sn] = min(ys)
    return out


# --- module health ----------------------------------------------------------
def module_stats(cur: dict[str, Any],
                 results_by_sn: dict[str, list[dict[str, Any]]],
                 min_enrol: int = DEFAULT_MIN_ENROL) -> list[dict[str, Any]]:
    """Every module seen in the record, with its pass/fail picture, mean mark,
    repeat burden, blocking factor and a gatekeeper score.

    Keyed on the core code, so a re-sit and its base collapse to one module. Pass
    rate is of students who produced a settled result (pass or fail), never of
    everyone enrolled -- dividing by enrolment understates a module exactly the
    way scoring a missing mark as zero does. Completion rate carries the rest of
    the story, so the two are read together.
    """
    core_len = cur.get("core_len") or R.DEFAULT_CORE_LEN
    catalogue = {R.core_code(m["code"], core_len): m for m in cur.get("modules", [])}
    blocking = R.blocking_factor(cur)
    # blocking is keyed on the catalogue's own code form; map onto core codes.
    block_by_core = {R.core_code(k, core_len): v for k, v in blocking.items()}

    agg: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"enrolled": 0, "passed": 0, "failed": 0, "pending": 0,
                 "marks": [], "students": set(), "attempts": defaultdict(int),
                 "by_year": defaultdict(lambda: {"p": 0, "f": 0}), "name": ""})
    for sn, rows in results_by_sn.items():
        for r in rows:
            code = R.core_code(_code_of(r), core_len)
            if not code:
                continue
            outcome = _row_outcome(r)
            if outcome == "ungraded":
                continue
            a = agg[code]
            a["enrolled"] += 1
            a["students"].add(sn)
            a["attempts"][sn] += 1
            if not a["name"] and r.get("module_name"):
                a["name"] = str(r.get("module_name") or "")
            mark = r.get("mark", r.get("grade"))
            if mark is not None:
                a["marks"].append(mark)
            y = _yr(r.get("calendar_year"))
            if outcome == "pass":
                a["passed"] += 1
                if y:
                    a["by_year"][y]["p"] += 1
            elif outcome == "fail":
                a["failed"] += 1
                if y:
                    a["by_year"][y]["f"] += 1
            else:
                a["pending"] += 1

    out: list[dict[str, Any]] = []
    for code, a in agg.items():
        settled = a["passed"] + a["failed"]
        n_students = len(a["students"])
        repeats = sum(1 for c in a["attempts"].values() if c > 1)
        cat = catalogue.get(code)
        trend = [{"year": y, "pass_rate": round(100 * v["p"] / (v["p"] + v["f"]))}
                 for y, v in sorted(a["by_year"].items()) if (v["p"] + v["f"])]
        fail_rate = (100 * a["failed"] / settled) if settled else 0.0
        block = block_by_core.get(code, 0)
        # Gatekeeper score: how many students a module stalls, weighted by how
        # much it gates downstream. fail-rate x students-failed surfaces scale
        # (a 35% fail on 400 beats 100% on 2); (1 + blocking) lifts a module that
        # also blocks later ones. Only ranked when enrolment clears the floor.
        score = (fail_rate * a["failed"] / 100.0) * (1 + block) if settled else 0.0
        out.append({
            "code": code,
            "name": a["name"] or (cat.get("name") if cat else "") or "",
            "credits": float((cat.get("credits") if cat else 0) or 0),
            "level": R.code_level(code),
            "year": (cat.get("year") if cat else None),
            "catalogued": cat is not None,
            "prescribed": bool(cat and cat.get("type", "prescribed")
                               not in ("elective", "free_elective", "core_elective")),
            "enrolled": a["enrolled"],
            "students": n_students,
            "passed": a["passed"],
            "failed": a["failed"],
            "pending": a["pending"],
            "pass_rate": round(100 * a["passed"] / settled, 1) if settled else None,
            "fail_rate": round(fail_rate, 1) if settled else None,
            "completion_rate": round(100 * settled / a["enrolled"], 1) if a["enrolled"] else None,
            "mean": round(sum(a["marks"]) / len(a["marks"]), 1) if a["marks"] else None,
            "repeat_rate": round(100 * repeats / n_students, 1) if n_students else 0.0,
            "blocking": block,
            "score": round(score, 1),
            "ranked": settled >= min_enrol,
            "trend": trend,
        })
    out.sort(key=lambda m: (-m["score"], -(m["fail_rate"] or 0)))
    return out


def gatekeepers(mods: list[dict[str, Any]], top: int = 12) -> list[dict[str, Any]]:
    """The ranked bottlenecks: modules that clear the enrolment floor, worst
    gatekeeper score first. These are the modules to look at, not the tiny-n
    modules a raw fail-rate sort would float to the top."""
    ranked = [m for m in mods if m["ranked"]]
    return ranked[:top]


# --- cohorts, throughput, time-to-degree ------------------------------------
def cohort_analysis(cur: dict[str, Any],
                    results_by_sn: dict[str, list[dict[str, Any]]],
                    bio: dict[str, dict[str, Any]],
                    regulation_years: int = DEFAULT_REGULATION_YEARS,
                    current_year: int | None = None) -> dict[str, Any]:
    """Intake cohorts followed to their outcome: the throughput table, the
    per-cohort survival curve, and the time-to-degree spread.

    "Finished" is the completion module's DC/DGOR verdict -- the same list the
    Completion tab shows. A student who has not finished is "active" if they hold
    a result in the last two years, otherwise "left". Time-to-degree is measured
    in elapsed semesters from a finisher's first cycle to their completion cycle.
    """
    reg_sem = regulation_years * 2
    intake = intake_years(results_by_sn)
    years = sorted({y for y in intake.values()})
    all_years = sorted({y for rows in results_by_sn.values() for y in _active_years(rows)})
    cy = current_year or (all_years[-1] if all_years else None)

    lists = C.completion_lists(cur, results_by_sn, bio)
    finished: dict[str, dict[str, Any]] = {}
    for tag, rows in (("DC", lists["DC"]), ("DGOR", lists["DGOR"])):
        for r in rows:
            finished[r["student_number"]] = {**r, "kind": tag}

    # Per-student time-to-degree, from intake cycle to completion cycle.
    tt: dict[str, dict[str, Any]] = {}
    for sn, r in finished.items():
        fc = _first_cycle(results_by_sn.get(sn, []))
        cyr, csem = r.get("completed_year"), r.get("completed_semester")
        if fc and isinstance(cyr, int) and isinstance(csem, int):
            elapsed = _elapsed_semesters(fc, (cyr, csem))
            registered = len({(y, semester_of_block(x.get("block")))
                              for x in results_by_sn[sn]
                              for y in [_yr(x.get("calendar_year"))]
                              if y and semester_of_block(x.get("block"))})
            tt[sn] = {"intake": fc[0], "elapsed_semesters": elapsed,
                      "elapsed_years": ceil(elapsed / 2),
                      "registered_semesters": registered,
                      "on_time": elapsed <= reg_sem, "kind": r["kind"]}

    # Throughput table: one row per intake cohort, banded by time to finish.
    bands = ["regulation", "reg+1yr", "reg+2yr", "reg+3yr", "reg+4yr+"]

    def band_of(elapsed: int) -> str:
        over_years = ceil((elapsed - reg_sem) / 2)
        if over_years <= 0:
            return "regulation"
        return bands[min(over_years, 4)]

    cohorts: list[dict[str, Any]] = []
    for iy in years:
        members = [sn for sn, y in intake.items() if y == iy]
        n = len(members)
        band_counts = {b: 0 for b in bands}
        n_done = 0
        for sn in members:
            if sn in tt:
                band_counts[band_of(tt[sn]["elapsed_semesters"])] += 1
                n_done += 1
            elif sn in finished:
                n_done += 1                       # finished but no datable cycle
        active = sum(1 for sn in members
                     if sn not in finished
                     and cy is not None
                     and max(_active_years(results_by_sn[sn]) or {0}) >= cy - 1)
        left = n - n_done - active
        elapsed_now = (cy - iy) if cy else 0
        cohorts.append({
            "intake": iy, "n": n,
            "completed": n_done,
            "completion_rate": round(100 * n_done / n) if n else 0,
            "active": active, "left": left,
            "bands": band_counts,
            "years_elapsed": elapsed_now,
            # A cohort can only be judged on throughput once it has had at least
            # the regulation time to graduate; younger cohorts are still running.
            "mature": elapsed_now >= regulation_years,
        })

    # Per-cohort survival: headcount registered each calendar year after intake,
    # with cumulative graduates -- the throughput-linked-to-intake curve.
    survival: list[dict[str, Any]] = []
    for iy in years:
        members = [sn for sn, y in intake.items() if y == iy]
        if len(members) < 5:                       # skip stragglers; noise, not cohorts
            continue
        series = []
        for y in range(iy, (cy or iy) + 1):
            reg = sum(1 for sn in members if y in _active_years(results_by_sn[sn]))
            grad = sum(1 for sn in members
                       if sn in finished and isinstance(finished[sn].get("completed_year"), int)
                       and finished[sn]["completed_year"] <= y)
            series.append({"year": y, "registered": reg, "graduated": grad})
        survival.append({"intake": iy, "n": len(members), "series": series})

    # Time-to-degree distribution (all finishers with a datable cycle).
    dist: dict[int, int] = defaultdict(int)
    for v in tt.values():
        dist[v["elapsed_semesters"]] += 1
    ttd = sorted(v["elapsed_semesters"] for v in tt.values())
    median = ttd[len(ttd) // 2] if ttd else None

    return {
        "regulation_years": regulation_years,
        "regulation_semesters": reg_sem,
        "current_year": cy,
        "intake_by_year": {str(y): sum(1 for v in intake.values() if v == y)
                           for y in years},
        "cohorts": cohorts,
        "bands": bands,
        "survival": survival,
        "time_to_degree": {
            "distribution": [{"semesters": k, "n": dist[k]} for k in sorted(dist)],
            "median_semesters": median,
            "n_finishers": len(tt),
            "on_time": sum(1 for v in tt.values() if v["on_time"]),
        },
        "totals": {
            "students": len(results_by_sn),
            "finished": len(finished),
            "dc": len(lists["DC"]), "dgor": len(lists["DGOR"]),
        },
    }


# --- top level --------------------------------------------------------------
def health(cur: dict[str, Any],
           results_by_sn: dict[str, list[dict[str, Any]]],
           bio: dict[str, dict[str, Any]],
           min_enrol: int = DEFAULT_MIN_ENROL) -> dict[str, Any]:
    """The whole picture, ready for JSON: overview KPIs, intake, throughput,
    time-to-degree, and module health."""
    prog = cur.get("programme", {})
    reg_years = int(prog.get("regulation_years") or DEFAULT_REGULATION_YEARS)
    all_years = sorted({y for rows in results_by_sn.values()
                        for y in _active_years(rows)})
    cy = all_years[-1] if all_years else None

    cohorts = cohort_analysis(cur, results_by_sn, bio, reg_years, cy)
    mods = module_stats(cur, results_by_sn, min_enrol)
    gks = gatekeepers(mods)

    current_head = sum(1 for rows in results_by_sn.values()
                       if cy and cy in _active_years(rows))
    ttd = cohorts["time_to_degree"]
    worst = gks[0] if gks else None

    return {
        "ready": True,
        "programme": {"code": prog.get("code", ""), "name": prog.get("name", ""),
                      "total_credits": prog.get("total_credits"),
                      "regulation_years": reg_years},
        "overview": {
            "students": len(results_by_sn),
            "current_headcount": current_head,
            "current_year": cy,
            "finished": cohorts["totals"]["finished"],
            "median_time_semesters": ttd["median_semesters"],
            "on_time_finishers": ttd["on_time"],
            "n_finishers": ttd["n_finishers"],
            "worst_gatekeeper": (worst["code"] if worst else None),
            "modules_catalogued": sum(1 for m in mods if m["catalogued"]),
            "modules_seen": len(mods),
        },
        "cohorts": cohorts,
        "modules": mods,
        "gatekeepers": gks,
        "decisions": [
            {"id": "H-INTAKE", "text": "Intake year = a student's earliest result "
             "year. Articulation entrants carry their augmented entry year here."},
            {"id": "H-REGYEARS", "text": f"Regulation length taken as {reg_years} "
             "years (programme.regulation_years). Augmented Civil should set 5."},
            {"id": "H-MINENROL", "text": f"Gatekeeper ranking floors at {min_enrol} "
             "settled results, so tiny-n modules do not float to the top."},
        ],
    }
