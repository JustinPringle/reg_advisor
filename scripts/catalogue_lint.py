"""
catalogue_lint.py -- report what a programme file restates from the catalogue.

A programme file should say where a module sits and what must be passed first.
What a module IS -- its name, credits and level -- belongs to catalogue/
modules.yaml, and the loader already fills it in. A restated fact is dead
weight at best and a second source of truth at worst: the day ITS changes a
credit value, the programme file quietly overrides it.

    python catalogue_lint.py ../programmes/civil.yaml [--fix]

Without --fix it prints three lists: facts safely removable (they agree with
the catalogue), facts that DISAGREE (never removed automatically -- an
academic authored that override deliberately, or ITS is wrong), and codes the
catalogue does not carry at all.

--fix rewrites the file, deleting only the agreeing lines. Comments and layout
elsewhere survive; the deleted lines are printed so the diff is reviewable.
"""
from __future__ import annotations
import os
import re
import sys
import yaml

FACTS = ("name", "credits", "level")
DEFAULT_CATALOGUE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "catalogue", "modules.yaml")


def load_catalogue(path: str = DEFAULT_CATALOGUE) -> dict:
    with open(path, encoding="utf-8") as fh:
        return (yaml.safe_load(fh) or {}).get("modules") or {}


def audit(programme_path: str, catalogue: dict) -> dict[str, list]:
    with open(programme_path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    declared = {(str(o.get("code")), str(o.get("field"))): o
                for o in (raw.get("catalogue_overrides") or []) if isinstance(o, dict)}
    same, differ, declared_hits, unknown = [], [], [], []
    for m in raw.get("modules") or []:
        code = str(m.get("code"))
        fact = catalogue.get(code)
        if not fact:
            if m.get("type") not in ("elective", "free_elective", "core_elective"):
                unknown.append(code)
            continue
        for key in FACTS:
            if m.get(key) is None or fact.get(key) is None:
                continue
            if m[key] == fact[key]:
                same.append((code, key, m[key]))
            elif (code, key) in declared:
                declared_hits.append((code, key, m[key], fact[key]))
            else:
                differ.append((code, key, m[key], fact[key]))
    # A declared override in the block but no restated line on the module is the
    # target state, so it is not reported at all -- only a restatement is.
    return {"same": same, "differ": differ, "declared": declared_hits,
            "unknown": unknown}


def _split_flow(body: str) -> list[str]:
    """Split `a: 1, b: "x, y"` on top-level commas only."""
    parts, buf, quote, depth = [], "", None, 0
    for ch in body:
        if quote:
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append(buf)
            buf = ""
            continue
        buf += ch
    if buf.strip():
        parts.append(buf)
    return parts


def strip(programme_path: str, removable: list[tuple]) -> list[str]:
    """Delete agreeing `key: value` lines from the module they belong to.

    Line-based on purpose: yaml.dump would reflow the whole file and throw away
    every comment in it. Only block-style entries are touched; a one-line
    `- {code: X, ...}` flow mapping is left alone and reported.
    """
    want: dict[str, set[str]] = {}
    for code, key, _ in removable:
        want.setdefault(code, set()).add(key)
    lines = open(programme_path, encoding="utf-8").read().splitlines(keepends=True)
    out, dropped, current = [], [], None
    for line in lines:
        flow = re.match(r"(\s*-\s+)\{(.*)\}(\s*(?:#.*)?)$", line.rstrip("\n"))
        if flow:
            kept, code, removed = [], None, []
            for part in _split_flow(flow.group(2)):
                k, _, v = part.partition(":")
                k, v = k.strip().strip('"'), v.strip()
                if k == "code":
                    code = v.strip('"')
                kept.append((k, part))
            if code and code in want:
                keys = want[code]
                out_parts = []
                for k, part in kept:
                    if k in keys:
                        removed.append(f"{code}: {part.strip()}")
                    else:
                        out_parts.append(part.strip())
                if removed:
                    dropped.extend(removed)
                    nl = "\n" if line.endswith("\n") else ""
                    out.append(f"{flow.group(1)}{{{', '.join(out_parts)}}}{flow.group(3)}{nl}")
                    current = None
                    continue
            current = None
            out.append(line)
            continue
        m = re.match(r"\s*-\s+code:\s*\"?([A-Za-z0-9_]+)\"?\s*$", line)
        if m:
            current = m.group(1)
            out.append(line)
            continue
        if re.match(r"\s*-\s", line):          # any other list item ends the block
            current = None
        if current and current in want:
            k = re.match(r"\s+(\w+):", line)
            if k and k.group(1) in want[current]:
                dropped.append(f"{current}: {line.strip()}")
                continue
        out.append(line)
    open(programme_path, "w", encoding="utf-8").write("".join(out))
    return dropped


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    path, fix = argv[0], "--fix" in argv
    rep = audit(path, load_catalogue())
    print(f"{path}: {len(rep['same'])} restated facts, "
          f"{len(rep['differ'])} undeclared disagreements, "
          f"{len(rep['declared'])} declared overrides restated, "
          f"{len(rep['unknown'])} codes not in the catalogue")
    for code, key, val in rep["same"]:
        print(f"  restated  {code}.{key} = {val!r}")
    for code, key, mine, theirs in rep["differ"]:
        print(f"  DISAGREES {code}.{key}: programme {mine!r} vs catalogue {theirs!r}")
    for code, key, mine, theirs in rep["declared"]:
        print(f"  declared  {code}.{key}: {mine!r} overrides catalogue {theirs!r} "
              f"-- delete the line, catalogue_overrides already carries it")
    for code in rep["unknown"]:
        print(f"  unknown   {code}: not in the catalogue")
    if fix:
        dropped = strip(path, rep["same"])
        print(f"\nremoved {len(dropped)} lines:")
        for d in dropped:
            print("  " + d)
        left = len(rep["same"]) - len(dropped)
        if left:
            print(f"\n{left} restated facts sit in flow mappings "
                  f"(- {{code: X, ...}}) and were left for hand editing.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
