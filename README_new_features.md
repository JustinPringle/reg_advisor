# Three additions to the registration advisor

All standard-library Python plus the existing vanilla-JS page, in the project's
style: policy is data, the checks are pure functions, nothing auto-clears.

## 1. Programme picker reads the folder

`programme_catalogue.py` reads every `*.yaml` in `programmes/` and returns each
file's own `programme.code` and `programme.name`. On upload the user picks a
programme from that list; the server resolves the rule file itself, so nothing
but the code and the file is typed.

- `GET /api/programme_files` — the list the picker shows.
- The **Upload ERS** dialog now has a programme dropdown (with an "Other (enter
  code)…" fallback) instead of three text fields.

## 2. Degree-complete (DG) and vac-work-only (DGOR)

`completion.py` builds two admin lists from the transcript against the
programme's own module list. No new thresholds.

- **DG** — every prescribed module passed (a pass code, or a mark ≥ 50).
- **DGOR** — everything passed except practical vacation work.

Vac-work modules are read from the programme file (`vac_work: true`, or by
default a 0-credit DP module whose name mentions "vacation" — catches `ENCV4VW`).
Elective slots need coordinator sign-off and cannot be verified from a
transcript, so they never hide a completed student; the gap shows as a flag.

- New **Completion** tab. `GET /api/completion?programme=`,
  `POST /api/completion/export` → `degree_complete_<p>.csv`, `vac_only_<p>.csv`.

## 3. ERS checker: proposed vs calculated

`ers_check.py` lines up the ERS's proposed term code against the engine's, at the
green/orange/red/exclude level. A row is a **match** when they agree, a
**mismatch** otherwise; mismatches carry the engine's reasons and export for the
manual pass. Nothing is auto-changed. The engine reads the prior term's code
(second-latest decision) as its history input.

**Store the final, check the initial.** Upload carries a `kind`:
`final` (default) is ingested as the record; `initial` is stored and re-parsed on
demand but never written to the tables. So the database holds only the final, and
the initial is still there to check.

- New **ERS check** tab with a Final / Initial source toggle.
  `GET /api/erscheck?programme=&source=final|initial`,
  `POST /api/erscheck/export`, `GET /api/documents?programme=`.

### Caveat, stated plainly

The "calculated code" is the current `ers_engine`, which does not yet implement
the full three-tree, credit-threshold logic in `robot_system_logic.md`. On the
real Civil ERS it agrees with the registrar for 74 of 170 students. The checker's
plumbing is complete; shortening the mismatch list means driving the calculated
code from the §3 threshold tables and §3a probation regime — gated on your
sign-off of the three open questions in §5, so not guessed here.

## New / changed files

| File | Status |
|---|---|
| `scripts/programme_catalogue.py` | new |
| `scripts/completion.py` | new |
| `scripts/ers_check.py` | new |
| `scripts/checks_service.py` | new |
| `scripts/test_new_features.py` | new (8 checks) |
| `scripts/store.py` | edited — `documents` table + `add_document`/`documents`/`current_document`/`decisions` |
| `scripts/server.py` | edited — new endpoints; upload resolves programme + takes `kind` |
| `web/advisor.html` | edited — programme picker + initial/final on upload; Completion and ERS-check tabs |

## Run

```
python ingest_ers.py ../data/<ers> --programme ENG-CIVIL --name "Civil Engineering" --yaml ../programmes/civil.yaml
python server.py            # http://127.0.0.1:8000
python test_new_features.py # 8 checks
python test_ingest.py       # existing 6 still pass
```
