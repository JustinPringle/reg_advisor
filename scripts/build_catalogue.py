#!/usr/bin/env python3
"""Build modules.yaml (and an ITS reference file) from the UKZN ITS extracts.

Inputs (the two workbooks the Faculty office circulates each August):
    <year>__August_GroupSetups_<y1>_to_<y2>.xlsx     sheet "Data"
    <year>__August_Prerequisite_<y1>_to_<y2>.xlsx    sheet "Data"

Outputs:
    catalogue/modules.yaml        module facts only: name, credits, level, blocks
    catalogue/its_reference.yaml  --audit only. ITS group setups, prerequisites and
                                  substitutions for ENG-CV / ENGEAP. Never loaded at
                                  runtime; it exists to diff against civil.yaml and
                                  augmented_civil.yaml.

Standard library only: xlsx is read directly as a zip of XML.

Usage:
    python scripts/build_catalogue.py GroupSetups.xlsx Prerequisite.xlsx \
        --year 2027 --out catalogue/ [--audit]
"""

import argparse
import datetime as _dt
import os
import re
import zipfile
import xml.etree.ElementTree as ET

NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
RNS = "{http://schemas.openxmlformats.org/package/2006/relationships}"
ONS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"

# Qualification scope. ENGEAP rows with no Major are the shared access groups.
MAINSTREAM_QUAL = "ENG-CV"
AUGMENTED_QUAL = "ENGEAP"
AUGMENTED_MAJOR = "ENCV"


# --------------------------------------------------------------------------
# minimal xlsx reader
# --------------------------------------------------------------------------

def _shared_strings(z):
    if "xl/sharedStrings.xml" not in z.namelist():
        return []
    out = []
    for si in ET.fromstring(z.read("xl/sharedStrings.xml")):
        out.append("".join(t.text or "" for t in si.iter(NS + "t")))
    return out


def _sheet_path(z, sheet_name):
    wb = ET.fromstring(z.read("xl/workbook.xml"))
    rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
    target = {r.get("Id"): r.get("Target") for r in rels}
    for sh in wb.iter(NS + "sheet"):
        if sh.get("name") == sheet_name:
            t = target[sh.get(ONS + "id")]
            return t if t.startswith("xl/") else "xl/" + t.lstrip("/")
    raise KeyError("sheet %r not found" % sheet_name)


def _col_index(ref):
    letters = re.match(r"([A-Z]+)", ref).group(1)
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def read_sheet(path, sheet_name):
    """Yield dicts keyed by the header row. Values are str, int, float or None."""
    with zipfile.ZipFile(path) as z:
        strings = _shared_strings(z)
        target = _sheet_path(z, sheet_name)
        header = None
        with z.open(target) as fh:
            for _, row in ET.iterparse(fh, events=("end",)):
                if row.tag != NS + "row":
                    continue
                cells = {}
                for c in row.iter(NS + "c"):
                    v = c.find(NS + "v")
                    if v is None or v.text is None:
                        continue
                    if c.get("t") == "s":
                        val = strings[int(v.text)]
                    elif c.get("t") == "inlineStr":
                        val = "".join(t.text or "" for t in c.iter(NS + "t"))
                    else:
                        txt = v.text
                        try:
                            val = int(txt)
                        except ValueError:
                            try:
                                val = float(txt)
                            except ValueError:
                                val = txt
                    cells[_col_index(c.get("r"))] = val
                row.clear()
                if header is None:
                    header = [cells.get(i) for i in range(max(cells) + 1)] if cells else None
                    continue
                if not cells:
                    continue
                yield {h: cells.get(i) for i, h in enumerate(header) if h is not None}


# --------------------------------------------------------------------------
# derivation
# --------------------------------------------------------------------------

# Codes whose digit does not give the programme level. Confirmed by Justin,
# 14 Sep 2026: the ENPD7 professional-practice modules sit at level 4 in Civil.
LEVEL_OVERRIDES = {
    "ENPD7PP": 4,
    "ENPD7CL": 4,
}


def derive_level(code):
    """UKZN codes carry the level as the first digit. Returns (level, flagged)."""
    if code in LEVEL_OVERRIDES:
        return LEVEL_OVERRIDES[code], False
    m = re.search(r"\d", code or "")
    if not m:
        return None, True
    lvl = int(m.group(0))
    return lvl, not (1 <= lvl <= 4)


def in_scope(row):
    if row.get("Qual") == MAINSTREAM_QUAL:
        return True
    if row.get("Qual") == AUGMENTED_QUAL:
        return row.get("Major") in (AUGMENTED_MAJOR, None)
    return False


# --------------------------------------------------------------------------
# yaml emitter (stdlib only; scope is small and known)
# --------------------------------------------------------------------------

_SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ()&,./'+-]*$")


def q(s):
    if s is None:
        return "null"
    s = str(s)
    if _SAFE.match(s) and not s.endswith(" "):
        return s if not re.match(r"^(y|n|yes|no|true|false|on|off|null)$", s, re.I) else '"%s"' % s
    return '"%s"' % s.replace("\\", "\\\\").replace('"', '\\"')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("groupsetups")
    ap.add_argument("prerequisites")
    ap.add_argument("--year", type=int, required=True)
    ap.add_argument("--out", default="catalogue")
    ap.add_argument("--audit", action="store_true", help="also write its_reference.yaml")
    ap.add_argument("--source-label", default=None)
    args = ap.parse_args()

    groups = [r for r in read_sheet(args.groupsetups, "Data") if r.get("Yr") == args.year]
    prereqs = [r for r in read_sheet(args.prerequisites, "Data")
               if r.get("Year") == args.year
               and r.get("Qual") in (None, MAINSTREAM_QUAL, AUGMENTED_QUAL)]

    # facts: every module the university offers, so lookups never miss
    facts = {}
    for r in groups:
        code = r.get("Module")
        if not code:
            continue
        # ITS names carry stray runs of spaces (ENCH160 has a double space before
        # "Engineers"). Collapse them, or a programme file that spells the name
        # correctly reads as a disagreement forever.
        name = r.get("ModuleName")
        facts.setdefault(code, {"name": " ".join(str(name).split()) if name else name,
                                "credits": r.get("Subj Cred")})

    # scope: modules reachable from the Civil mainstream and augmented Civil curricula
    scope_rows = [r for r in groups if in_scope(r)]
    core = {r["Module"] for r in scope_rows if r.get("Module")}
    related = set()
    for r in prereqs:
        main_m, inv = r.get("Main Module"), r.get("Involved Module")
        # what a Civil module depends on, and anything ITS treats as equivalent to
        # a Civil module. Not every module elsewhere that happens to need one.
        if main_m in core and inv in facts:
            related.add(inv)
        if r.get("Relation") == "S" and inv in core and main_m in facts:
            related.add(main_m)

    blocks = {}
    for r in scope_rows:
        b = r.get("Block")
        if b is None:
            continue
        blocks.setdefault(r["Module"], set()).add(int(b))

    flagged = []
    lines = [
        "# modules.yaml — shared module catalogue for the Civil Engineering programmes.",
        "#",
        "# Facts about modules, nothing about curricula. Which slot a module fills, which",
        "# year it sits in and what a student must pass first are programme questions and",
        "# live in programmes/civil.yaml and programmes/augmented_civil.yaml.",
        "#",
        "# Membership is a programme question too: a module appearing here does not mean",
        "# a Civil student may take it. The programme files name the modules that count.",
        "#",
        "# GENERATED FILE — do not hand-edit. Regenerate with:",
        "#   python scripts/build_catalogue.py <GroupSetups.xlsx> <Prerequisite.xlsx> \\",
        "#       --year %d --out catalogue/" % args.year,
        "",
        "meta:",
        "  version: %s" % q("%d.1" % args.year),
        "  calendar_year: %d" % args.year,
        "  generated: %s" % _dt.date.today().isoformat(),
        "  source: %s" % q(args.source_label or os.path.basename(args.groupsetups)),
        "  scope: %s" % q("modules reachable from ENG-CV and ENGEAP (ENCV), plus their "
                          "prerequisite and substitute modules"),
        "  level_basis: %s" % q("first digit of the module code, except the codes in "
                                "LEVEL_OVERRIDES in scripts/build_catalogue.py"),
    ]

    body = ["", "modules:"]
    for code in sorted(core | related):
        f = facts[code]
        lvl, flag = derive_level(code)
        if flag:
            flagged.append(code)
        body.append("  %s:" % code)
        body.append("    name: %s" % q(f["name"]))
        body.append("    credits: %s" % (f["credits"] if f["credits"] is not None else "null"))
        body.append("    level: %s" % (lvl if lvl is not None else "null"))
        if code in blocks:
            body.append("    blocks: [%s]" % ", ".join(str(b) for b in sorted(blocks[code])))

    lines.append("  level_unverified: [%s]" % ", ".join(sorted(flagged)))
    lines.append("  module_count: %d" % len(core | related))
    lines += body
    lines.append("")

    os.makedirs(args.out, exist_ok=True)
    path = os.path.join(args.out, "modules.yaml")
    with open(path, "w") as fh:
        fh.write("\n".join(lines))
    print("wrote %s (%d modules, %d flagged levels)" % (path, len(core | related), len(flagged)))

    if args.audit:
        write_reference(args, scope_rows, prereqs, core, facts)


def write_reference(args, scope_rows, prereqs, core, facts):
    """ITS ground truth for ENG-CV / ENGEAP, for diffing against the programme files."""
    rel_name = {"P": "prerequisite", "C": "corequisite", "S": "substitute", "E": "excluded"}
    lines = [
        "# its_reference.yaml — ITS ground truth, REFERENCE ONLY.",
        "# Not loaded at runtime. Diff the programme files against this after each",
        "# August extract; anything that differs is either a handbook change or a bug.",
        "",
        "meta:",
        "  calendar_year: %d" % args.year,
        "  generated: %s" % _dt.date.today().isoformat(),
        "  relations: {P: prerequisite, C: corequisite, S: substitute, E: excluded}",
        "  and_or_note: >-",
        "    ITS 'And/Or Indicator' is inconsistently captured. A blank means AND.",
        "    A number groups alternatives, but the data uses both one-group-per-",
        "    alternative and one-group-holding-all-alternatives. Treat every numbered",
        "    row as unverified and confirm against the handbook.",
        "",
        "group_setups:",
    ]
    seen = {}
    for r in scope_rows:
        key = (r.get("Qual"), r.get("Major"), r.get("GrpCode"))
        seen.setdefault(key, {"desc": r.get("Grp Desc"), "sp": set(), "block": set(),
                              "min": r.get("Min Credits"), "max": r.get("Max Credits"),
                              "ot": set(), "modules": set()})
        e = seen[key]
        e["sp"].add(r.get("Study Period"))
        if r.get("Block") is not None:
            e["block"].add(int(r["Block"]))
        e["ot"].add(r.get("OT"))
        e["modules"].add(r.get("Module"))
    for (qual, major, grp), e in sorted(seen.items(), key=lambda kv: (kv[0][0], str(kv[0][1]), kv[0][2])):
        lines.append("  - group: %s" % q(grp))
        lines.append("    qual: %s" % q(qual))
        lines.append("    major: %s" % q(major))
        lines.append("    description: %s" % q(e["desc"]))
        lines.append("    study_periods: [%s]" % ", ".join(str(s) for s in sorted(x for x in e["sp"] if x is not None)))
        lines.append("    blocks: [%s]" % ", ".join(str(b) for b in sorted(e["block"])))
        lines.append("    offering_types: [%s]" % ", ".join(q(o) for o in sorted(x for x in e["ot"] if x)))
        lines.append("    min_credits: %s" % (e["min"] if e["min"] is not None else "null"))
        lines.append("    max_credits: %s" % (e["max"] if e["max"] is not None else "null"))
        lines.append("    modules: [%s]" % ", ".join(sorted(m for m in e["modules"] if m)))

    lines += ["", "prerequisites:"]
    for r in sorted(prereqs, key=lambda x: (str(x.get("Main Module")), str(x.get("Relation")),
                                            str(x.get("Involved Module")))):
        if r.get("Main Module") not in core:
            continue
        lines.append("  - module: %s" % q(r["Main Module"]))
        lines.append("    relation: %s" % q(rel_name.get(r.get("Relation"), r.get("Relation"))))
        lines.append("    involves: %s" % q(r.get("Involved Module")))
        if r.get("And/Or Indicator") is not None:
            lines.append("    group: %s" % q(r.get("And/Or Indicator")))
        if r.get("Min Mark") is not None:
            lines.append("    min_mark: %s" % int(r["Min Mark"]))
        if r.get("Qual") is not None:
            lines.append("    qual: %s" % q(r.get("Qual")))

    pairs = set()
    for r in prereqs:
        if r.get("Relation") != "S":
            continue
        a, b = r.get("Main Module"), r.get("Involved Module")
        if a in core or b in core:
            pairs.add(tuple(sorted((a, b))))
    lines += ["", "# Symmetric 'substitute' pairs: ITS's own equivalence table. Cross-check the",
              "# augmented twin pairs in augmented_civil.yaml against this list.",
              "substitutes:"]
    for a, b in sorted(pairs):
        lines.append("  - [%s, %s]" % (q(a), q(b)))
    lines.append("")

    path = os.path.join(args.out, "its_reference.yaml")
    with open(path, "w") as fh:
        fh.write("\n".join(lines))
    print("wrote %s" % path)


if __name__ == "__main__":
    main()
