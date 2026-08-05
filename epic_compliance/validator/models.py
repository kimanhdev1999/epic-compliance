"""Validator-agnostic result types.

Both backends (HTTP service, Java CLI) return a FHIR OperationOutcome. These
models are the normalised form the rest of the tool consumes, so a backend swap
never reaches the rules engine.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

#: OperationOutcome issue severities, most severe first.
Severity = Literal["fatal", "error", "warning", "information"]

SEVERITY_ORDER: dict[str, int] = {"fatal": 0, "error": 1, "warning": 2, "information": 3}


class ValidationMessage(BaseModel):
    """One issue reported by the FHIR validator."""

    severity: Severity
    message: str
    location: str = ""       # FHIRPath / element path, e.g. Patient.identifier[0]
    code: str = ""           # OperationOutcome.issue.code
    diagnostics: str = ""

    def is_blocking(self) -> bool:
        """fatal/error break conformance; warning/information do not."""
        return self.severity in ("fatal", "error")


class ValidationResult(BaseModel):
    """Outcome of validating one resource against one profile.

    ``available=False`` means the validator could not be reached or run at all.
    That is NOT a pass and NOT a failure — callers must surface it as
    needs_human, the same rule the auth evidence follows.
    """

    resource_type: str
    profile: str = ""
    available: bool = True
    unavailable_reason: str = ""
    messages: list[ValidationMessage] = Field(default_factory=list)
    backend: str = ""        # which validator produced this
    raw: dict = Field(default_factory=dict)

    @property
    def errors(self) -> list[ValidationMessage]:
        return [m for m in self.messages if m.is_blocking()]

    @property
    def warnings(self) -> list[ValidationMessage]:
        return [m for m in self.messages if m.severity == "warning"]

    def is_valid(self) -> bool | None:
        """True / False, or None when the validator never ran."""
        if not self.available:
            return None
        return not self.errors

    def summary(self) -> str:
        if not self.available:
            return f"validator unavailable: {self.unavailable_reason}"
        if not self.messages:
            return f"{self.resource_type} conforms to {self.profile or 'base spec'} (0 issues)"
        counts: dict[str, int] = {}
        for m in self.messages:
            counts[m.severity] = counts.get(m.severity, 0) + 1
        parts = [f"{counts[s]} {s}" for s in sorted(counts, key=lambda s: SEVERITY_ORDER[s])]
        return f"{self.resource_type} vs {self.profile or 'base spec'}: " + ", ".join(parts)


class ValidatorUnavailable(RuntimeError):
    """Raised by a backend that cannot run. Never let this mean "valid"."""
