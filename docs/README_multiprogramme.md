# Multi-programme ingest

Any programme uploads its ERS, clicks one button, and its students land in a
local database. The advisor then lets you pick a programme, pull its students,
and advise them against that programme's rules. Nothing leaves the machine.

## What was built

Six files, standard library only. The database is one SQLite file — `sqlite3`
ships with Python, so this adds no dependency and keeps the data on disk, POPIA-contained.

| File | Does |
|---|---|
| `ers_ingest.py` | Reads any programme's ERS (PDF, text, or a saved dump) into three record lists: students, module results, and the registrar's term decisions. Programme-agnostic — the plan code is read from each section line, not hard-coded. |
| `store.py` | The SQLite store. Every table is keyed by programme. Ingesting a semester upserts its rows, so re-uploading a file is a no-op and each new semester grows the history. |
| `ingest_ers.py` | The "click a button" step. Takes a file and a programme label, parses, and upserts. Accepts a PDF/text ERS or an existing results CSV. |
| `datasource_sqlite.py` | A programme-scoped source with the same three methods the old CSV source had, so the server and page did not change shape. Adds `list_programmes()`. |
| `multipart.py` | A small form-data reader for the upload (Python 3.13 dropped `cgi`). |
| `server.py` | The advisor server, now programme-aware. Every student endpoint is scoped by `?programme=`, plus `GET /api/programmes` and `POST /api/ingest`. |

The page (`web/advisor.html`) gains a programme picker and an **Upload ERS** button.

## The heart of the advisor, unchanged

Your flow still drives every module:

```
prescribed course
    -> already passed?            -> passed
    -> prereqs met?               -> can register
    -> GPA >= 55, <= 1 missing    -> concession possible
    otherwise                     -> blocked / review
```

The store feeds this flow; the engines that run it were not touched.

## Columns added

Each result row now carries `plan_code` (ENG-CV, ENGEAP, ENG-ME, ...) beside the
programme label, and the store holds a `term_decisions` table with the
registrar's code per (student, year, semester) — RISK, RSK2, PROB, FPRR, XNFA,
FPMA, CO. Those decisions were in the ERS PDF and the earlier parser dropped them.

## Run it

```
# from scripts/
python ingest_ers.py ../data/ers_civil_2026jul.txt \
    --programme ENG-CIVIL --name "Civil Engineering" --yaml ../programmes/civil.yaml

python ingest_ers.py ../data/ERS_ENGEAP-1_Jul_2026.pdf \
    --programme ENG-CIVIL-AUG --name "Augmented Civil Engineering" \
    --yaml ../programmes/augmented_civil.yaml

python server.py            # http://127.0.0.1:8000
python test_ingest.py       # six checks, all against real data
```

## Tested on both programmes

- **Civil** ingests from the real ERS. The parser reproduces every module record
  in `ers_data.csv` exactly (10 587 rows), and adds 172 real term decisions.
  Advice runs against `civil.yaml`; the registrar's standing and the engine's
  cross-check agree for 192 of 308 students, and every disagreement is shown, not hidden.
- **Augmented Civil** ingests from its ERS PDF as its own programme (358 students,
  9 733 results, **zero blank periods**, 328 real term decisions). Registrar standing
  flows from those decisions — 30 green, 277 orange, 36 red, 15 excluded — and each
  flagged student shows the registrar's own wording. With `augmented_civil.yaml` in
  place, prerequisite advice runs too, and the augmented twin rule was verified on
  real transcripts (see below): no mis-clears, no false blocks.

## Both programmes now advise on real data

With `augmented_civil.yaml` in `programmes/`, augmented prerequisite advice runs.
The two programmes sit side by side:

| | Civil | Augmented Civil |
|---|---|---|
| Students | 308 | 358 |
| Standing (registrar) | 141 green · 102 orange · 51 red · 14 excl. | 30 green · 277 orange · 36 red · 15 excl. |
| Passed / can register / concession | 5 751 / 2 265 / 29 | 3 372 / 2 873 / 18 |

## The augmented twin rule, checked on real transcripts

I stress-tested the `{any: [augmented, mainstream]}` substitution and the fail-safe
against the real cohort, using ENCV2SA (prereq: a first-semester maths — MATH141 or
its augmented twin MATH161 — *and* MATH142):

- **No mis-clears.** 61 students clear ENCV2SA to *can register*; every one holds
  the maths passes the rule requires. None clears without them.
- **No false blocks.** 47 students who passed a first-semester maths are still held.
  Every one is missing MATH142 — they are taking it this term, so it is an in-progress
  registration, not a pass. The engine correctly waits.
- **The twin OR resolves against the transcript.** The held students' unmet list reads
  `(MATH161 | MATH141)`, proving the augmented twin satisfies the mainstream requirement.

## About the loader warnings

Loading `augmented_civil.yaml` prints lines like "ENCV2SA: prereq names MATH141, not
in this programme". These are metadata notes, not decisions. The augmented module list
does not declare the first-year twin codes (MATH141, PHYS151, ENME1EM, ...) as its own
modules, but those codes live in the students' transcripts, and the engine resolves
prerequisites against the transcript. As shown above, not one warning caused a wrong
decision. To quiet them, declare the first-year twins as known external codes in the
programme; leaving them is harmless. The warning wording ("routes to review/blocked")
overstates the effect for any code that appears in a transcript.

## Notes on correctness

- **No row is dropped.** An ERS lists an in-progress row beside its later result,
  and a supp beside the first sitting. Numbering them within their period keeps
  both. This preserved 16 such Civil rows and 2 227 Augmented rows that a plain
  period key would have collapsed, and it stays idempotent because an ERS export
  is cumulative.
- **The parser was the right place to profile first.** Reading the real ERS before
  writing store logic surfaced the mis-stamped cross-registration periods and the
  same-`F`-different-meaning risk your notes already flag.

`test_four_buckets` in `test_engines.py` fails on the current repo before any of
this change — a pre-existing engine-test drift, untouched here.
