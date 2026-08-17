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


def completion(store: Any, programme: str) -> dict[str, Any]:
    cur = _load_cur(store, programme)
    if cur is None:
        return {"ready": False, "DC": [], "DGOR": [],
                "summary": {"DC": 0, "DGOR": 0}}
    bio = {r["student_number"]: r for r in store.students(programme)}
    lists = C.completion_lists(cur, store.results(programme), bio)
    return {"ready": True, **lists,
            "summary": {"DC": len(lists["DC"]), "DGOR": len(lists["DGOR"])}}


def ers_check(store: Any, programme: str, source: str = "final") -> dict[str, Any]:
    """Compare registrar codes with the engine's, on the chosen source."""
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

    report = X.check_parsed(parsed, cur)
    return {"ready": True, "source": source, **report}
