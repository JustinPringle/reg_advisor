#!/usr/bin/env python3
"""
remove_programme.py -- delete one programme's data from the store.

An alternative to deleting the whole database and re-uploading every ERS. It
clears a single programme -- students, results, term decisions, ingest log, and
document records -- and drops its registration, leaving every other programme
untouched. The deletion is destructive, so it previews the counts and does
nothing until you pass --yes.

    # see what is on file
    python remove_programme.py --db ../data/advisor.db

    # preview one programme (no change)
    python remove_programme.py --programme ENG-CIVIL-AUG --db ../data/advisor.db

    # remove it
    python remove_programme.py --programme ENG-CIVIL-AUG --db ../data/advisor.db --yes

    # clear its data but keep the code registered, ready for a clean re-ingest
    python remove_programme.py --programme ENG-CIVIL-AUG --db ../data/advisor.db --yes --keep

The captured ERS PDFs a programme points to stay on disk. Their paths are listed
so you can remove them yourself; pass --files to unlink them in the same run.
"""
from __future__ import annotations
import argparse
from pathlib import Path

from store import Store


def _counts(db: Store, code: str) -> dict[str, int]:
    """Row counts a removal would clear, read before anything is deleted."""
    tables = ("students", "results", "term_decisions", "ingests", "documents")
    return {t: db.db.execute(
        f"SELECT COUNT(*) FROM {t} WHERE programme=?", (code,)).fetchone()[0]
        for t in tables}


def list_programmes(db: Store) -> None:
    rows = db.programmes()
    if not rows:
        print("no programmes registered")
        return
    print("registered programmes:")
    for r in rows:
        print(f"  {r['code']:16} {r['students']:>5} students   {r['name']}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Remove one programme from the store.")
    ap.add_argument("--programme", help="programme code to remove, e.g. ENG-CIVIL-AUG")
    ap.add_argument("--db", default="../data/student_data.db", help="SQLite file")
    ap.add_argument("--yes", action="store_true", help="perform the deletion (else preview)")
    ap.add_argument("--keep", action="store_true",
                    help="keep the programme registered; clear its data only")
    ap.add_argument("--files", action="store_true",
                    help="also unlink the programme's stored ERS PDFs")
    args = ap.parse_args()

    db = Store(args.db)

    if not args.programme:
        list_programmes(db)
        db.close()
        return

    if db.programme(args.programme) is None:
        print(f"no such programme: {args.programme}")
        list_programmes(db)
        db.close()
        return

    before = _counts(db, args.programme)
    total = sum(before.values())
    print(f"{args.programme}: "
          f"{before['students']} students, {before['results']} results, "
          f"{before['term_decisions']} decisions, {before['ingests']} ingest log rows, "
          f"{before['documents']} documents")

    if not args.yes:
        print("preview only -- re-run with --yes to remove "
              f"({total} rows across the programme).")
        db.close()
        return

    result = db.remove_programme(args.programme, keep_registration=args.keep)
    removed = result["removed"]
    kept = " (registration kept)" if args.keep else ""
    print(f"removed {args.programme}{kept}: "
          + ", ".join(f"{n} {t}" for t, n in removed.items()))

    files = result["document_files"]
    if files and args.files:
        for f in files:
            try:
                Path(f).unlink()
                print(f"  unlinked {f}")
            except OSError as e:
                print(f"  could not unlink {f}: {e}")
    elif files:
        print("  stored ERS files left on disk (pass --files to unlink):")
        for f in files:
            print(f"    {f}")

    db.close()


if __name__ == "__main__":
    main()
