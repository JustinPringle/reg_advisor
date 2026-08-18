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
import yaml

from regadvisor_engine import prereq_codes

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


# --- Load -------------------------------------------------------------------
def load_programme(path: str, validate: bool = True,
                   strict: bool = True) -> dict[str, Any]:
    """Read an authored programme file into the engine's curriculum shape.

    validate: run validate_programme and print its warnings.
    strict:   raise ValueError if validation reports any errors.
    """
    with open(path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}

    prog = dict(raw.get("programme") or {})
    modules: list[dict[str, Any]] = []
    total = 0.0
    for m in raw.get("modules") or []:
        mod = _normalise_module(m)
        if mod["type"] == "prescribed" and isinstance(mod.get("credits"), (int, float)):
            total += float(mod["credits"])
        modules.append(mod)

    prog.setdefault("code", "PROG")
    prog.setdefault("name", "")
    prog.setdefault("total_credits", total)   # a declared value wins if present
    external = [str(c).strip() for c in (raw.get("external_prereqs") or [])]
    for pair in (raw.get("equivalences") or []):
        if isinstance(pair, dict):
            external += [str(v).strip() for v in pair.values() if isinstance(v, str)]
    cur = {"programme": prog, "modules": modules, "elective_groups": {},
           "rules": merge_rules(raw.get("rules")),
           "external_prereqs": sorted(set(external))}
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


def _normalise_module(m: dict[str, Any]) -> dict[str, Any]:
    """Fill defaults and derive review_notes from the prereq tree."""
    mod = dict(m)
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
        for f in ("year", "sem"):
            if not isinstance(m.get(f), int):
                warnings.append(f"{code}: {f} should be an integer")

    catalogue = set(codes)
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
