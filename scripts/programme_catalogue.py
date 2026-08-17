"""
programme_catalogue.py -- discover the programmes on offer from a folder.

A coordinator drops a programme definition (civil.yaml, augmented_civil.yaml,
mechanical.yaml, ...) into `programmes/` and it becomes selectable. Nothing is
typed at upload time: the catalogue reads each file's own `programme.code` and
`programme.name`, so the person uploading an ERS picks a programme from a list
and the server already knows which rule file to bind.

    cat = catalogue("programmes")
    #  [{"code": "ENG-CIVIL", "name": "Civil Engineering",
    #    "yaml_path": "programmes/civil.yaml", "modules": 43, "error": None}, ...]

The read is deliberately light -- a bare yaml.safe_load, not a full validated
load -- so a half-authored file still lists (with its parse error surfaced)
rather than hiding the whole folder behind one bad document. A file that fails
to parse is returned with `error` set and `code`/`name` filled from its stem,
never silently dropped.

Pure apart from reading the folder. Standard library plus PyYAML, matching the
rest of the tool.
"""
from __future__ import annotations
from typing import Any
from pathlib import Path
import yaml


def read_header(path: Path) -> dict[str, Any]:
    """Read one programme file's identity: code, name, module count.

    Never raises: a broken file returns its stem as the code and the parse
    error in `error`, so the catalogue stays complete and the fault is visible.
    """
    stem = path.stem
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        return {"code": stem, "name": stem, "yaml_path": str(path),
                "modules": 0, "error": f"parse error: {exc}"}
    prog = raw.get("programme") or {}
    modules = raw.get("modules") or []
    prescribed = sum(1 for m in modules
                     if (m or {}).get("type", "prescribed") == "prescribed")
    return {"code": str(prog.get("code") or stem),
            "name": str(prog.get("name") or prog.get("code") or stem),
            "yaml_path": str(path), "modules": prescribed, "error": None}


def catalogue(folder: str | Path = "programmes") -> list[dict[str, Any]]:
    """Every programme definition in `folder`, sorted by name.

    Looks for *.yaml and *.yml. A duplicate programme code keeps the
    first-seen file and marks the later one, so two files claiming ENG-CIVIL
    never both bind -- the ambiguity is reported, not resolved at random.
    """
    root = Path(folder)
    if not root.is_dir():
        return []
    seen: dict[str, str] = {}
    out: list[dict[str, Any]] = []
    for path in sorted(root.glob("*.y*ml")):
        if not path.is_file():
            continue
        entry = read_header(path)
        code = entry["code"]
        if code in seen:
            entry["error"] = (entry["error"] + "; " if entry["error"] else "") + \
                f"duplicate code, already defined by {seen[code]}"
        else:
            seen[code] = path.name
        out.append(entry)
    out.sort(key=lambda e: e["name"].lower())
    return out


def resolve(code: str, folder: str | Path = "programmes") -> dict[str, Any] | None:
    """Find one catalogued programme by its code (the value the picker sends)."""
    for entry in catalogue(folder):
        if entry["code"] == code:
            return entry
    return None


def main() -> None:
    import sys
    folder = sys.argv[1] if len(sys.argv) > 1 else "../programmes"
    rows = catalogue(folder)
    print(f"{len(rows)} programme(s) in {folder}")
    for r in rows:
        flag = f"  !! {r['error']}" if r["error"] else ""
        print(f"  {r['code']:16} {r['name']:32} {r['modules']:>3} modules{flag}")


if __name__ == "__main__":
    main()
