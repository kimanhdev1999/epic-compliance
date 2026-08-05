"""Parse a FHIR OperationOutcome into ValidationMessages.

Shared by every backend: the HTTP service returns an OperationOutcome, and
validator_cli.jar emits one too with `-output`. Parsing it once keeps the
backends thin.
"""
from __future__ import annotations

from typing import Any

from .models import SEVERITY_ORDER, ValidationMessage

# The validator emits a few issues that are noise for our purposes: they fire on
# every resource fetched from a live server and say nothing about conformance.
IGNORABLE_SUBSTRINGS = (
    "Unable to connect to terminology server",
    "has not been checked because it is unknown",
)


def _location_of(issue: dict[str, Any]) -> str:
    """R4 uses `expression`; DSTU2/R3 and some tools still emit `location`."""
    for key in ("expression", "location"):
        value = issue.get(key)
        if isinstance(value, list) and value:
            return str(value[0])
        if isinstance(value, str) and value:
            return value
    return ""


def _text_of(issue: dict[str, Any]) -> str:
    details = issue.get("details") or {}
    for candidate in (details.get("text"), issue.get("diagnostics")):
        if candidate:
            return str(candidate)
    coding = details.get("coding") or []
    if coding and coding[0].get("display"):
        return str(coding[0]["display"])
    return "(no message)"


def parse_operation_outcome(
    outcome: dict[str, Any],
    *,
    drop_noise: bool = True,
) -> list[ValidationMessage]:
    """Convert an OperationOutcome dict into ValidationMessages, most severe first.

    Unknown severities are promoted to "error" rather than dropped — an issue we
    cannot classify must not silently disappear from the report.
    """
    if not isinstance(outcome, dict):
        return []

    messages: list[ValidationMessage] = []
    for issue in outcome.get("issue", []) or []:
        if not isinstance(issue, dict):
            continue
        text = _text_of(issue)
        if drop_noise and any(s in text for s in IGNORABLE_SUBSTRINGS):
            continue
        severity = issue.get("severity", "")
        if severity not in SEVERITY_ORDER:
            severity = "error"
        messages.append(
            ValidationMessage(
                severity=severity,
                message=text,
                location=_location_of(issue),
                code=str(issue.get("code", "")),
                diagnostics=str(issue.get("diagnostics", "")),
            )
        )

    messages.sort(key=lambda m: SEVERITY_ORDER[m.severity])
    return messages
