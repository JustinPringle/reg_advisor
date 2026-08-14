# Advisor engines (Python port)

Two AutoScholar engines, ported to Python and run on UKZN ERS data and a
programme curriculum. Both engines are pure: they read plain dicts and return
plain dicts, so any programme is just different data, not different code.

## Files

- `ers_engine.py` — academic-standing classifier. Ordered `CRITERIA` as data,
  a generic evaluator, and `derive_metrics` to roll result rows into the
  figures the criteria read. Percentage thresholds live in `policy`.
- `regadvisor_engine.py` — prerequisite advice. Transcript index, a
  prerequisite-tree evaluator (`eval_term`), the four-bucket classifier
  (`eval_advice`), plus blocking factor, concession evidence, and the
  ERS→credit-cap table.
- `data_loaders.py` — `load_curriculum` (spreadsheet → modules, parsing the
  free-text prerequisite column) and `load_results` (CSV → transcript rows).
- `advise.py` — the driver: one-student report, the COC cleared/review check,
  and a whole-cohort sweep.
- `test_engines.py` — fast checks on the pure engines.

## Run

    python advise.py            # demo: one student, a COC check, a cohort sweep
    python advise.py 218028575  # report for one student
    python test_engines.py      # tests

## The read / compute boundary

The engines never touch a file. The loaders do all I/O and hand the engines
dicts. This is the AutoScholar separation kept intact: swap the loaders and the
same engines run on any feed.

## Any programme

Nothing is Civil-specific. A second programme supplies:

1. a curriculum spreadsheet in the same layout (year/semester headers, then
   `code | name | credits | ax | prereqs`), and
2. a `policy` dict — pass mark, passing codes, and the cut points
   (`cumulative_good`, `semester_good`, `min_progression_pct`).

The default policy is UKZN Engineering (`min_progression_pct = 48/72`). Pass the
same `policy` to `derive_metrics` and `classify`; `advise_student` does this for
you.

## Mapping to robot_system_logic.md

Ported directly:

- Trees A/B/C become one ordered criteria list (first true wins), reading
  `last_status` and `semesters_registered` rather than selecting a tree.
- The 70% semester and 75% cumulative tests are `ERS-ORANGE-SEM` and
  `ERS-ORANGE-CUMUL` / the green gate.
- The 48-based minimum-progression gate is `below_minimum`
  (`cumulative < min_progression_pct`).
- The CEACOM/AEACOM appeal branch is the `FLOW` graph.
- The 56/48/32 probation caps are the `ers_credit_cap` table, kept separate from
  classification: label the standing, then look up the cap.

Approximated or deferred, and therefore routed to a human by design:

- Aggregate prerequisites the handbook states in prose (`>=32cr core`,
  `passed all preceding core modules`) parse to a `review` term. The module can
  never auto-clear; it goes to the coordinator with the original wording.
- RAFC (final-year last chance), the completion override `min(56, remaining)`,
  and a BLUE / Dean's-commendation leaf are not yet criteria. Each is additive:
  one more entry in `CRITERIA` or the cap table.

## What the demo shows

On the bundled data: 43 prescribed modules, 528 credits; 6 modules carry a
`review` prerequisite. Across 308 students the classifier returns 208 green,
91 orange, 9 red, with 100 students on a credit cap this term — the backtest
figure for the academic-leader discussion.
