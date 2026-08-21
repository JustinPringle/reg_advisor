#!/usr/bin/env python3
"""
ingest_ers.py -- read one programme's ERS into the database.

This is the "click a button" step, run from the command line or called by the
server's upload handler. It accepts either an ERS export (PDF or its text dump)
or an already-parsed results CSV, tags every row with the programme, and upserts
them into the SQLite store. Re-running it on the same file is a no-op; running it
each semester grows the history.

    python ingest_ers.py ../data/ENG-CV_ERS.pdf --programme ENG-CIVIL \
           --name "Civil Engineering" --yaml ../programmes/civil.yaml

    python ingest_ers.py ../data/ers_data_aug.csv --programme ENG-CIVIL-AUG \
           --name "Augmented Civil Engineering" --yaml ../programmes/augmented_civil.yaml
"""
from __future__ import annotations
from typing import Any
import argparse
import csv
from pathlib import Path

import yaml

from ers_ingest import parse_file
from store import Store


def parse_csv(path: str, programme: str) -> dict[str, list[dict]]:
    """Read an existing ers_data CSV into the parser's record shape.

    A pre-parsed CSV carries module results only -- no term decisions -- so those
    stay empty until the programme's PDF is ingested.
    """
    results: list[dict] = []
    students: dict[str, dict] = {}
    with open(path, newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            sn = (r.get("student_number") or "").strip()
            if not sn:
                continue
            results.append({
                "student_number": sn, "programme": programme,
                "surname": (r.get("surname") or "").strip(),
                "name": (r.get("name") or "").strip(),
                "calendar_year": r.get("calendar_year") or "",
                "block": (r.get("block") or "").strip(),
                "plan_code": (r.get("plan_code") or "").strip(),
                "study_period": r.get("study_period"),
                "module_code": (r.get("module_code") or "").strip(),
                "module_name": (r.get("module_name") or "").strip(),
                "credits": r.get("credits"), "grade": r.get("grade"),
                "result_code": (r.get("result_code") or "").strip(),
                "result_text": (r.get("result_text") or "").strip(),
                "year_of_study": r.get("year_of_study"),
                "total_credits": r.get("total_credits")})
            students.setdefault(sn, {
                "student_number": sn, "programme": programme,
                "surname": (r.get("surname") or "").strip(),
                "name": (r.get("name") or "").strip(),
                "plan_code": (r.get("plan_code") or "").strip(),
                "year_of_study": r.get("year_of_study"),
                "total_credits": r.get("total_credits")})
    return {"students": list(students.values()), "results": results, "decisions": []}



def streams_from_yaml(yaml_path: str) -> "set[str] | None":
    """Read ingest.stream_codes from a programme file, or None if absent.

    When a programme's YAML names the streams it owns, a shared feed (the
    augmented ERS carries every engineering stream) is filtered to just those.
    Absent the key, nothing is filtered -- a single-stream feed needs no filter.
    """
    if not yaml_path or not Path(yaml_path).exists():
        return None
    doc = yaml.safe_load(Path(yaml_path).read_text(encoding="utf-8")) or {}
    codes = (doc.get("ingest") or {}).get("stream_codes")
    return set(codes) if codes else None


def ingest_path(db: Store, path: str, programme: str, name: str = "",
                yaml_path: str = "") -> dict[str, int]:
    """Register the programme and ingest one file (PDF/text or CSV)."""
    db.register_programme(programme, name, yaml_path)
    suffix = Path(path).suffix.lower()
    if suffix == ".csv":
        parsed = parse_csv(path, programme)
    else:
        parsed = parse_file(path, programme, keep_streams=streams_from_yaml(yaml_path))
    counts = db.ingest(parsed, source=Path(path).name)
    counts["n_skipped"] = len(parsed.get("skipped", []))
    counts["skipped"] = parsed.get("skipped", [])
    return counts


def main() -> None:
    ap = argparse.ArgumentParser(description="Ingest an ERS export into the store.")
    ap.add_argument("path", help="ERS export (PDF or text) or an ers_data CSV")
    ap.add_argument("--programme", required=True, help="programme code, e.g. ENG-CIVIL")
    ap.add_argument("--name", default="", help="human-readable programme name")
    ap.add_argument("--yaml", default="", help="path to the programme's rule file")
    ap.add_argument("--db", default="../data/advisor.db", help="SQLite file")
    args = ap.parse_args()

    db = Store(args.db)
    counts = ingest_path(db, args.path, args.programme, args.name, args.yaml)
    print(f"ingested {args.path} -> {args.programme}: "
          f"{counts['n_students']} students, {counts['n_results']} results, "
          f"{counts['n_decisions']} decisions")
    skipped = counts.get("skipped", [])
    if skipped:
        placed = [r for r in skipped if r["stream_codes"]]
        unplaced = [r for r in skipped if not r["stream_codes"]]
        print(f"  skipped {len(skipped)} students not in this programme's streams "
              f"({len(placed)} other-stream, {len(unplaced)} with no stream code)")
        for r in unplaced:
            print(f"    no stream code -> review: {r['student_number']}")
    db.close()


if __name__ == "__main__":
    main()
