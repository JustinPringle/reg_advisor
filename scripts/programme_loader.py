"""
programme_loader.py -- load and validate an authored programme file.

Replaces data_loaders.load_curriculum's free-text prerequisite parser with a
structured source. Prerequisites are authored directly in the engine's term
grammar (regadvisor_engine.eval_term), so loading is validation, not guessing.

load_programme returns the SAME curriculum shape the engines already consume:
  {"programme": {code, name, total_credits}, "modules": [...], "elective_groups": {}}
so advise.py switches loaders by changing one import line.

validate_programme is a pure function returning a report; load_programme calls
it and, when strict, refuses to hand back a curriculum that fails -- bad
authoring crashes loud rather than mis-clearing a student.

Pure apart from reading the YAML file. No pandas, no DOM.
"""
from __future__ import annotations
from typing import Any
import os
import yaml

from regadvisor_engine import prereq_codes

# Shared module catalogue: name, credits and level for every module a Civil
# student can touch, generated from the ITS extract by scripts/build_catalogue.py.
# A programme file names codes and structure; the facts come from here, so the
# two programme files can no longer disagree about what a module is worth.
DEFAULT_CATALOGUE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "catalogue", "modules.yaml")

# Each prereq DICT must match exactly one of these key-sets. Anything else is an
# authoring slip (a typo like `min_creditz`) the validator reports as an error.
_TERM_SHAPES: set[frozenset[str]] = {
    frozenset({"code"}),
    frozenset({"code", "min_mark"}),
    frozenset({"code", "soft"}),
    frozenset({"code", "min_mark", "soft"}),
    frozenset({"all"}),
    frozenset({"any"}),
    frozenset({"any_n", "of"}),
    frozenset({"min_year"}),
    frozenset({"min_sem"}),
    frozenset({"min_credits"}),
    frozenset({"min_credits", "level"}),
    frozenset({"review"}),
    frozenset({"preceding_core"}),
}

ELECTIVE_TYPES = {"elective", "free_elective", "core_elective"}
_NUMERIC_KEYS = ("min_mark", "min_year", "min_sem", "min_credits", "level", "any_n")

# --- Policy defaults --------------------------------------------------------
# Every rule the engines apply lives here as data. A programme file may carry a
# `rules:` block to override any of these, key by key; anything it omits falls
# back to the default below. Missing or malformed rules never crash -- they use
# the safe default, so a new programme works before its rules are authored.
DEFAULT_RULES: dict[str, Any] = {
    "concession": {          # regadvisor_engine.eval_advice
        "min_gpa": 55,       # credit-weighted average floor
        "max_missing": 1,    # requirements short still eligible
        "prereq_floor": 45,  # must have scored ABOVE this in the failed prereq
    },
    "autoclear": {           # triage.py -- an academic-authored waiver rule
        "enabled": True,
        "min_wam": 55,
        "carry_band": [46, 49],
        "single_miss_only": True,
        "allowed_standings": ["green", "orange"],
        "rule_id": "CAC-v1",
    },
    "credit_cap": {          # regadvisor_engine.ers_credit_cap (status or code)
        "green": None, "orange": 48, "red": 32, "exclude": 0,
        "ERS-ORANGE-FIRSTSEM": 48, "ERS-ORANGE-CUMUL": 48, "ERS-ORANGE-SEM": 56,
        "ERS-RED-FIRST": 32, "ERS-RED-SECOND": 24, "ERS-EXCLUDE": 0,
    },
}


def merge_rules(authored: dict[str, Any] | None) -> dict[str, Any]:
    """Layer a programme's rules over the defaults, one block at a time, so a
    file may override a single number and inherit the rest."""
    authored = authored or {}
    out: dict[str, Any] = {}
    for block, default in DEFAULT_RULES.items():
        out[block] = {**default, **(authored.get(block) or {})}
    for block in authored:            # keep unknown blocks, but they do nothing
        out.setdefault(block, authored[block])
    return out


# --- Catalogue --------------------------------------------------------------
def load_catalogue(path: str | None = None) -> dict[str, dict[str, Any]]:
    """Read modules.yaml. A missing file is not an error: the loader then falls
    back to whatever the programme file states, exactly as it did before."""
    path = path or DEFAULT_CATALOGUE
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    return {str(k): dict(v or {}) for k, v in (raw.get("modules") or {}).items()}


def _read_overrides(raw: Any) -> dict[tuple[str, str], dict[str, Any]]:
    """Index a catalogue_overrides block by (code, field). Malformed entries are
    dropped here and reported by validate_programme, never applied blind."""
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for o in raw or []:
        if not isinstance(o, dict):
            continue
        code, field = str(o.get("code") or "").strip(), str(o.get("field") or "").strip()
        if code and field and "value" in o:
            out[(code, field)] = {"value": o["value"], "reason": str(o.get("reason") or "")}
    return out


# --- Load -------------------------------------------------------------------
def load_programme(path: str, validate: bool = True, strict: bool = True,
                   catalogue: dict[str, dict[str, Any]] | str | None = None
                   ) -> dict[str, Any]:
    """Read an authored programme file into the engine's curriculum shape.

    validate:  run validate_programme and print its warnings.
    strict:    raise ValueError if validation reports any errors.
    catalogue: a loaded catalogue, a path to one, or None for the default.
    """
    if catalogue is None or isinstance(catalogue, str):
        catalogue = load_catalogue(catalogue)
    with open(path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}

    prog = dict(raw.get("programme") or {})
    # Declared divergences from the catalogue, applied as if the catalogue said
    # so. A programme that must value a module differently says it once, here,
    # with a reason -- so the module entries stay bare and the disagreement is a
    # decision on the record rather than a restated fact drifting quietly.
    overrides = _read_overrides(raw.get("catalogue_overrides"))
    if overrides:
        catalogue = {c: dict(f) for c, f in catalogue.items()}
        for (code, field), ov in overrides.items():
            catalogue.setdefault(code, {})[field] = ov["value"]
    modules: list[dict[str, Any]] = []
    total = 0.0
    seen_groups: set[str] = set()
    for m in raw.get("modules") or []:
        mod = _normalise_module(m, catalogue)
        # A choice group is one slot however many ways it can be taken, so its
        # credits count once. Without this every subtotal double-counts the slot.
        grp = mod.get("choice")
        counts = not (grp and grp in seen_groups)
        if grp:
            seen_groups.add(grp)
        if counts and mod["type"] == "prescribed" and isinstance(mod.get("credits"), (int, float)):
            total += float(mod["credits"])
        modules.append(mod)

    # for m in raw.get("modules") or []:
    #     mod = _normalise_module(m)
    #     if mod["type"] == "prescribed" and isinstance(mod.get("credits"), (int, float)):
    #         total += float(mod["credits"])
    #     modules.append(mod)

    prog.setdefault("code", "PROG")
    prog.setdefault("name", "")
    prog.setdefault("total_credits", total)   # a declared value wins if present
    external = [str(c).strip() for c in (raw.get("external_prereqs") or [])]
    equivalences: list[tuple[str, str]] = []
    # Directional twin map (augmented -> mainstream), read from the labelled
    # keys rather than tuple order, so the advisor can route a failed augmented
    # module to the mainstream module the student actually repeats.
    twins: dict[str, str] = {}
    for pair in (raw.get("equivalences") or []):
        if isinstance(pair, dict):
            codes = [str(v).strip() for v in pair.values() if isinstance(v, str)]
            external += codes
            if len(codes) == 2:
                equivalences.append((codes[0], codes[1]))
            aug, main = str(pair.get("augmented", "")).strip(), str(pair.get("mainstream", "")).strip()
            if aug and main:
                twins[aug] = main
    # {preceding_core: true} is shorthand, not a new engine term: expand it here,
    # once the whole module list is known, into the plain {all: [...]} the
    # engines and the JS mirror already evaluate.
    _expand_preceding_core(modules)

    year_credits = {int(k): float(v) for k, v in (raw.get("year_credits") or {}).items()}
    cur = {"programme": prog, "modules": modules, "elective_groups": {},
           "year_credits": year_credits,
           "rules": merge_rules(raw.get("rules")),
           "external_prereqs": sorted(set(external)),
           "equivalences": equivalences, "twins": twins,
           "catalogue": catalogue, "catalogue_overrides": overrides}
    # cur = {"programme": prog, "modules": modules, "elective_groups": {},
    #        "rules": merge_rules(raw.get("rules"))}

    if validate:
        report = validate_programme(cur)
        for w in report["warnings"]:
            print(f"  warn: {w}")
        if report["errors"]:
            msg = "programme validation failed:\n  " + "\n  ".join(report["errors"])
            if strict:
                raise ValueError(msg)
            print(msg)
    return cur


def _normalise_module(m: dict[str, Any],
                     catalogue: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """Fill defaults and derive review_notes from the prereq tree.

    Facts (name, credits, level) come from the catalogue when the programme file
    omits them. An authored value still wins -- validate_programme reports the
    disagreement rather than silently overriding an academic's authoring.
    """
    mod = dict(m)
    fact = (catalogue or {}).get(mod.get("code")) or {}
    for key in ("name", "credits", "level"):
        if mod.get(key) is None and fact.get(key) is not None:
            mod[key] = fact[key]
    if fact:
        mod["catalogue_credits"] = fact.get("credits")
        mod["catalogue_name"] = fact.get("name")
    mod.setdefault("prereqs", [])
    mod.setdefault("coreqs", [])
    mtype = mod.get("type") or "prescribed"
    mod["type"] = mtype
    # Compute defensively: a non-numeric credit is left in place for the
    # validator to report, rather than crashing normalisation here.
    raw_credits = mod.get("credits")
    credits = float(raw_credits) if isinstance(raw_credits, (int, float)) else None
    # A 0-credit prescribed module is a DP (duly-performed) workshop.
    mod["is_dp"] = bool(mod.get("is_dp") or (credits == 0 and mtype == "prescribed"))
    # The author writes each review reason once, inside its {review: ...} term;
    # the advice engine looks for module.review_notes, so surface them here.
    mod["review_notes"] = _collect_reviews(mod["prereqs"])
    # A choice group id, if any, normalised to a string so downstream grouping is
    # a plain dict key. Absent for the ordinary case of a module in its own slot.
    ch = mod.get("choice")
    mod["choice"] = str(ch) if ch else None
    return mod


def _collect_reviews(terms: Any) -> list[str]:
    out: list[str] = []

    def walk(t: Any) -> None:
        if isinstance(t, list):
            for x in t:
                walk(x)
        elif isinstance(t, dict):
            if "review" in t:
                out.append(str(t["review"]))
            for k in ("all", "any", "of"):
                if k in t:
                    walk(t[k])

    walk(terms)
    return out


# --- preceding_core ---------------------------------------------------------
def _expand_preceding_core(modules: list[dict[str, Any]]) -> None:
    """Rewrite every {preceding_core: true} term in place.

    The handbook's capstone gate reads "passed all preceding core modules in
    programme". Written out by hand it is a 40-line list that has to be edited
    every time the curriculum moves, and one stale line put ENCV4DE and ENCV4DS
    in each other's prerequisites. Derive it instead: every prescribed module
    sitting in a STRICTLY earlier (year, sem) slot than the module asking. A
    module can therefore never depend on its own semester, so the gate cannot
    manufacture a cycle.

    Electives are excluded -- a slot, not a named module. Twins are NOT widened
    here: regadvisor_engine.index_transcript already aliases an equivalent pair
    into the passed set, so an augmented code satisfies its mainstream twin
    everywhere, not just in this gate. Widening here would duplicate that logic
    and clutter every label and missing-list with an "or" the student never
    needs to read.
    """
    slot: dict[str, tuple[int, int]] = {}
    for m in modules:
        y, s = m.get("year"), m.get("sem")
        if isinstance(y, int) and isinstance(s, int):
            slot[str(m.get("code"))] = (y, s)

    def gate(code: str) -> dict[str, Any]:
        here = slot.get(code)
        terms: list[Any] = []
        if here is None:
            return {"all": terms}
        for m in modules:
            other = str(m.get("code"))
            if other == code or m.get("type") in ELECTIVE_TYPES:
                continue
            there = slot.get(other)
            if there is None or there >= here:
                continue
            terms.append(other)
        return {"all": terms}

    def walk(term: Any, code: str) -> Any:
        if isinstance(term, list):
            return [walk(t, code) for t in term]
        if isinstance(term, dict):
            if "preceding_core" in term:
                return gate(code) if term["preceding_core"] else {"all": []}
            return {k: (walk(v, code) if k in ("all", "any", "of") else v)
                    for k, v in term.items()}
        return term

    for m in modules:
        code = str(m.get("code"))
        for key in ("prereqs", "coreqs"):
            if m.get(key):
                m[key] = walk(m[key], code)
        m["review_notes"] = _collect_reviews(m.get("prereqs") or [])


# --- Validate ---------------------------------------------------------------
def validate_programme(cur: dict[str, Any]) -> dict[str, list[str]]:
    """Check a loaded curriculum. Returns {'errors': [...], 'warnings': [...]}.

    Errors (block a strict load): missing/duplicate codes, non-numeric credits,
    unrecognised prereq shapes, and prerequisite cycles.
    Warnings (surface, still load): a prereq naming a code outside this
    programme -- safe, because an unknown code is simply never-passed, which
    routes the module toward review rather than mis-clearing it.
    """
    errors: list[str] = []
    warnings: list[str] = []

    codes: dict[str, dict[str, Any]] = {}
    for i, m in enumerate(cur.get("modules", [])):
        code = m.get("code")
        if not code:
            errors.append(f"module #{i}: missing code")
            continue
        if code in codes:
            errors.append(f"duplicate module code {code}")
        codes[code] = m
        if not m.get("name"):
            warnings.append(f"{code}: missing name")
        if not isinstance(m.get("credits"), (int, float)):
            errors.append(f"{code}: credits must be a number, got {m.get('credits')!r}")
        if cur.get("catalogue"):
            if m.get("type") not in ELECTIVE_TYPES and code not in cur["catalogue"]:
                warnings.append(
                    f"{code}: not in the module catalogue -- a typo, a module ITS "
                    f"no longer offers, or a catalogue that needs regenerating.")
            cat_cr = m.get("catalogue_credits")
            if (isinstance(cat_cr, (int, float))
                    and isinstance(m.get("credits"), (int, float))
                    and float(cat_cr) != float(m["credits"])
                    and (code, "credits") not in (cur.get("catalogue_overrides") or {})):
                warnings.append(
                    f"{code}: programme file says {m['credits']} credits, "
                    f"catalogue (ITS) says {cat_cr}, and no catalogue_overrides "
                    f"entry declares why. Either delete the credits line and let "
                    f"the catalogue stand, or declare the override with a reason.")
        for f in ("year", "sem"):
            if not isinstance(m.get(f), int):
                warnings.append(f"{code}: {f} should be an integer")

    catalogue = set(codes)

    # Choice groups: two or more modules occupying ONE slot, of which the student
    # takes exactly one (mainstream ZULN101 vs a 16-credit elective). Members must
    # agree on credits and on (year, sem), or the slot's credit value is ambiguous
    # and every subtotal downstream is wrong.
    groups: dict[str, list[dict[str, Any]]] = {}
    for code, m in codes.items():
        g = m.get("choice")
        if g:
            groups.setdefault(str(g), []).append(m)
    for g, members in groups.items():
        if len(members) < 2:
            warnings.append(f"choice group {g}: only one member ({members[0]['code']}) "
                            f"— a choice of one is not a choice")
        if len({mm.get("credits") for mm in members}) > 1:
            errors.append(f"choice group {g}: members disagree on credits "
                            + ", ".join(f"{mm['code']}={mm.get('credits')}" for mm in members))
        if len({(mm.get("year"), mm.get("sem")) for mm in members}) > 1:
            errors.append(f"choice group {g}: members sit in different semesters "
                            + ", ".join(f"{mm['code']}=Y{mm.get('year')}S{mm.get('sem')}"
                                        for mm in members))
                
    allowed = {c.strip() for c in (cur.get("external_prereqs") or [])}
    known = catalogue | allowed
                    
    # catalogue = set(codes)
    for code, m in codes.items():
        _validate_terms(m.get("prereqs", []), f"{code}.prereqs", errors)
        _validate_terms(m.get("coreqs", []), f"{code}.coreqs", errors)
        for ref in prereq_codes(m):
            if ref not in known:
                warnings.append(
                    f"{code}: prereq {ref} is neither a module in this programme "
                    f"nor a declared external_prereqs code -- likely a typo, or add "
                    f"it to external_prereqs if it names another programme's course.")
            # if ref not in catalogue:
            #     warnings.append(
            #         f"{code}: prereq names {ref}, not in this programme "
            #         f"(treated as never-passed \u2192 routes to review/blocked)")

    for (code, field), ov in (cur.get("catalogue_overrides") or {}).items():
        if code not in codes:
            warnings.append(f"catalogue_overrides: {code}.{field} overrides a module "
                            f"this programme does not carry")
        elif not ov["reason"]:
            errors.append(f"catalogue_overrides: {code}.{field} has no reason -- "
                          f"an undocumented override is a second source of truth")

    # The handbook prints degree credits per year of study. If the module list
    # no longer adds up to it, one of the two is wrong and a student is being
    # advised against a curriculum that does not exist. A warning, not an error:
    # a disagreeing year total is a question for the handbook, and blocking every
    # load until it is answered helps nobody.
    want = cur.get("year_credits") or {}
    if want:
        got: dict[int, float] = {}
        counted: set[str] = set()
        for m in cur.get("modules", []):
            g = m.get("choice")            # one slot, counted once
            if g:
                if g in counted:
                    continue
                counted.add(g)
            cr = m.get("credits")
            if isinstance(cr, (int, float)) and isinstance(m.get("year"), int):
                got[m["year"]] = got.get(m["year"], 0.0) + float(cr)
        for yr in sorted(set(want) | set(got)):
            a, b = want.get(yr), got.get(yr, 0.0)
            if a is None:
                warnings.append(f"year {yr}: modules award {b:.0f} credits but "
                                f"year_credits does not list the year")
            elif a != b:
                warnings.append(f"year {yr}: handbook says {a:.0f} degree credits, "
                                f"the module list awards {b:.0f} ({b - a:+.0f})")
        prog_total = float(cur["programme"].get("total_credits") or 0)
        if sum(want.values()) != prog_total:
            warnings.append(f"year_credits sums to {sum(want.values()):.0f}, "
                            f"programme.total_credits says {prog_total:.0f}")

    cyc = _find_cycle(codes)
    if cyc:
        errors.append("prerequisite cycle among " + ", ".join(cyc))

    return {"errors": errors, "warnings": warnings}


def _validate_terms(term: Any, path: str, errors: list[str]) -> None:
    """Recursively check one term (or list of terms) against the grammar."""
    if term is None or isinstance(term, str):
        return
    if isinstance(term, list):
        for i, t in enumerate(term):
            _validate_terms(t, f"{path}[{i}]", errors)
        return
    if not isinstance(term, dict):
        errors.append(f"{path}: term must be a string, list or mapping, got {type(term).__name__}")
        return
    keys = frozenset(term.keys())
    if keys not in _TERM_SHAPES:
        errors.append(f"{path}: unrecognised prereq shape {sorted(term.keys())}")
        return
    for k in ("all", "any", "of"):
        if k in term:
            if not isinstance(term[k], list):
                errors.append(f"{path}.{k}: expected a list")
            else:
                for i, t in enumerate(term[k]):
                    _validate_terms(t, f"{path}.{k}[{i}]", errors)
    for k in _NUMERIC_KEYS:
        if k in term and not isinstance(term[k], (int, float)):
            errors.append(f"{path}.{k}: expected a number, got {term[k]!r}")


def _find_cycle(codes: dict[str, dict[str, Any]]) -> list[str]:
    """Kahn topological sort over internal prereq edges; report any cycle.

    Edge prereq \u2192 dependent, restricted to codes in this programme
    (cross-programme references are not structure here). Mirrors the cycle
    check in ProgrammeEngine.dagMetrics -- bad authoring must crash, not hang.
    """
    catalogue = set(codes)
    succ: dict[str, list[str]] = {c: [] for c in codes}
    indeg: dict[str, int] = {c: 0 for c in codes}
    for c, m in codes.items():
        for p in prereq_codes(m):
            if p in catalogue and p != c:
                succ[p].append(c)
                indeg[c] += 1
    queue = [c for c in codes if indeg[c] == 0]
    seen = 0
    while queue:
        c = queue.pop()
        seen += 1
        for d in succ[c]:
            indeg[d] -= 1
            if indeg[d] == 0:
                queue.append(d)
    if seen != len(codes):
        return sorted(c for c in codes if indeg[c] > 0)
    return []
