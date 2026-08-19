"""
standing_codes.py -- registrar term code -> shared standing.

The one canonical map from a registrar term code (RISK, PROB, RAPB, ...) to a
standing (green / orange / red / exclude). Both the badge path
(datasource_sqlite) and the checker (ers_check) import it, so the two can never
drift -- the drift between two hand-kept copies was the bug this replaces.

Policy, not logic: academics own the map. A programme may override any entry in
its YAML under rules.ers.status_of_code; status_of() lays that over the defaults.

Fail-safe. A code in neither the defaults nor the override resolves to REVIEW,
never green. An unrecognised standing must reach a person, not clear. This is the
correct direction of caution for this tool: the harm is clearing a readmit or
suspended student as good standing, not "inventing" probation for a good one.

Pure module: standard library only.
"""
from __future__ import annotations
from typing import Any

REVIEW = "review"   # sentinel: unmapped -> a person must classify by hand

# Grounded in robot_system_logic.md sections 3-4 and the ERS term-code list.
# The readmit / final-probation family (doc section C, lines 173-179) is RED.
# Entries marked (provisional) await Justin's sign-off -- see the decision table
# in README_new_features.md. Every provisional default is non-clearing, so an
# unconfirmed code is safe until confirmed.
DEFAULT_STATUS_OF_CODE: dict[str, str] = {
    # good standing
    "CO": "green", "GREEN": "green", "BLUE": "green",
    # completed -- not a risk standing; completion.py lists these separately
    "DC": "green", "DCCL": "green", "DCSL": "green", "DGOR": "green",
    # at risk
    "RISK": "orange", "RSK2": "orange",
    # RISU: first-year suspend-recommended, RISK-like. Orange as a current
    # standing (Justin, 2026-08-18); as an incoming code it aliases to RSK2 in
    # ers_check.INCOMING_ALIASES, which also resolves orange.
    "RISU": "orange",
    # probation / failed progression
    "PROB": "red", "FPRR": "red", "FPRD": "red", "FPMA": "red", "FPDS": "red",
    # readmit + final-probation family (robot_system_logic section C)
    "RAPB": "red", "RDPB": "red", "RASD": "red", "RDSD": "red",
    "RAAD": "red", "RDAD": "red", "RAFC": "red",
    # readmissions / suspensions (provisional; SUSP = out of progression)
    "RPSC": "red", "RA": "red", "SUSP": "red",
    # non-academic / administrative -> always a person
    "COND": REVIEW, "CALL": REVIEW,
    # exclusion
    "XNFA": "exclude", "XACA": "exclude", "XAC": "exclude",
}

EXCLUDE_CODES = {"XNFA", "XACA", "XAC"}


def status_of(code: str, policy: dict[str, Any] | None = None) -> str:
    """Standing for a registrar term code. A programme override wins; a code in
    neither map resolves to REVIEW, never green."""
    code = (code or "").strip().upper()
    if not code:
        return REVIEW
    override = ((policy or {}).get("status_of_code")) or {}
    if code in override:
        return str(override[code]).strip().lower()
    return DEFAULT_STATUS_OF_CODE.get(code, REVIEW)
