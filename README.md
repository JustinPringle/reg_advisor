# reg_advisor

A local tool that reads a programme's ERS export, classifies each student, and
shows what they may register. Academics decide; the tool clears the routine and
routes the rest to review. It has one job: cut the coordinator's routine load so
only genuine waiver decisions reach a person.

![Python](https://img.shields.io/badge/python-3.13-blue)
![Dependencies](https://img.shields.io/badge/deps-PyYAML%20%C2%B7%20openpyxl-lightgrey)
![Data](https://img.shields.io/badge/data-stays%20on%20device-green)

---

## The problem

Each registration cycle, a programme coordinator hand-checks hundreds of module
and change-of-curriculum requests against student transcripts: has this student
passed the prerequisites, are they within their credit cap, do they qualify for
a concession. Most requests are routine and provable from the record. A few need
judgement. The manual process spends the coordinator's time on the many to reach
the few.

reg_advisor reads the record, decides the provable cases, and hands the
coordinator only what genuinely needs a decision.

## The heart of the advisor

Every prescribed module runs through one flow:

```
prescribed course
    -> already passed?              -> passed
    -> prereqs met?                 -> can register
    -> GPA >= 55, <= 1 prereq short -> concession possible
    otherwise                       -> blocked / review
```

Thresholds are not baked into this flow. They live in the programme's YAML file,
so an academic owns every number and the engine stays generic.

## Design principles

- **Policy is data, not logic.** Rules and thresholds live in YAML; the engines
  stay programme-agnostic. Academics own the numbers.
- **Fail-safe.** The tool never mis-clears. Uncatalogued modules, unclassifiable
  students, and every edge case route to human review.
- **Standard library first.** No web framework, no CDN, no external calls. The
  two dependencies (PyYAML, openpyxl) sit at the curriculum-authoring edge.
- **Data stays on device.** Everything runs on localhost against local files.
  Built to be POPIA-contained (see [Data and privacy](#data-and-privacy)).
- **Prove before extending.** New auto-clear rules run in observe-only mode for
  one cycle before they are trusted live.

## Repository layout

```
reg_advisor/
├── data/               ERS exports and the generated SQLite store (git-ignored)
├── programmes/         one YAML per programme: modules, prereq trees, thresholds
│   ├── civil.yaml
│   └── augmented_civil.yaml
├── scripts/            run everything from here
│   ├── regadvisor_engine.py    prereq tree -> four advice buckets
│   ├── ers_engine.py           academic-standing classifier
│   ├── data_loaders.py         prereq parser, result-text treatment
│   ├── programme_loader.py     loads YAML, merges rules over safe defaults
│   ├── advise.py               per-student report and cohort sweep
│   ├── ers_ingest.py           programme-agnostic ERS parser
│   ├── ingest_ers.py           CLI: parse a file, upsert to the store
│   ├── store.py                SQLite store, idempotent upserts
│   ├── datasource_sqlite.py    programme-scoped read interface
│   ├── multipart.py            upload reader (cgi is gone in 3.13)
│   ├── triage.py               splits admin vs academic queues
│   ├── server.py               stdlib HTTP server on :8000
│   ├── test_engines.py         engine tests
│   └── test_ingest.py          ingest tests, against real data
├── web/
│   └── advisor.html            single-file page: picker, profile, plan, upload
└── docs/                       design notes and workflow references
```

## Requirements

- Python 3.13 or later
- PyYAML and openpyxl

```
pip install pyyaml openpyxl
```

Nothing else. `sqlite3` and the HTTP server ship with Python.

## Quick start

Clone the repo and run from `scripts/`. Ingest a programme's ERS export, then
start the server:

```
git clone https://github.com/JustinPringle/reg_advisor.git
cd reg_advisor/scripts

# ingest a programme
python ingest_ers.py ../data/ENG-CV_ERS_1_Jul_2026.pdf \
    --programme ENG-CIVIL --name "Civil Engineering" \
    --yaml ../programmes/civil.yaml

# serve the advisor
python server.py
```

Open <http://127.0.0.1:8000>, pick a programme, and select a student. The page
shows the student's standing, what they may register, and any concession cases.
Uploading another programme's ERS through the page ingests it and adds it to the
picker.

## How it works

```
ERS export ──► ers_ingest ──► store (SQLite) ──► datasource ──► engines ──► page
                                                                    │
                                                              triage ─┴─► admin queue
                                                                          academic queue
```

1. **Ingest.** `ers_ingest` reads any programme's ERS (PDF, text, or a saved
   dump) into three record lists: students, module results, and the registrar's
   term decisions. The plan code is read from each section line, not hard-coded.
2. **Store.** `store` upserts those rows into one SQLite file, keyed by
   programme. Re-uploading a cumulative export is a no-op; each new semester
   grows the history.
3. **Advise.** The engines classify each student and run every prescribed module
   through the flow above, against that programme's YAML rules.
4. **Triage.** `triage` splits the cohort's requests into an admin worklist (the
   provable, routine cases) and an academic queue (the judgement calls).
5. **Page.** A single vanilla-JS page presents all of it. No build step.

## Data and privacy

**Real student data never belongs in this repository.** ERS exports and the
generated `advisor.db` contain student numbers and names, so `data/` is kept out
of version control by `.gitignore`. Clone the repo, drop your own ERS export into
`data/`, and it stays on your machine.

The processing is designed to stay POPIA-contained: everything runs on the
coordinator's machine, against locally held files, with no call to any external
service. Publish the code and synthetic samples only.

## Tests

```
cd scripts
python test_engines.py
python test_ingest.py
```

The engine tests cover the four advice buckets, the concession gate, and the
auto-clear branches. The ingest tests run against the real cohort.

## Status and roadmap

Functionally complete across engines, ingest, store, server, and page. Two
programmes advise on real data.

Planned in phases:

- **Phase 0** — MS Forms export to a filtered, dropdown-approval view.
- **Phase 1** — Power Automate routing, a SharePoint list, a live dashboard.
- **Phase 2** — the rule-engine auto-clear, live.

Open decisions, deferred for academic sign-off rather than settled in code:
whether supplementary marks carry the same as a first sitting; whether DP
prerequisites should be modelled as more than a plain pass; and the auto-clear
rule, which runs observe-only for one cycle first.

## Background and credit

The advising concept originates in **AutoScholar**, by **Prof Randhir Rawatlal**
(UKZN). reg_advisor ports and generalises that idea so any programme can be
analysed from its own ERS export and rules.

Built and maintained by **Justin Pringle**, Senior Lecturer and Civil
Engineering Programme Coordinator, University of KwaZulu-Natal.

## License

No license is set yet. Until one is added, all rights are reserved. MIT is a
reasonable choice for the code; note that student data is not covered by any
code license and must not be redistributed.
