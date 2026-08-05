"""HL7 FHIR validator integration (Day 15).

Public surface:

    from epic_compliance.validator import get_validator, US_CORE_PROFILES
    v = get_validator(config)
    ok, detail = v.health()
    result = v.validate(patient_resource, US_CORE_PROFILES["Patient"])

`result.is_valid()` is tri-state: True, False, or None when the validator could
not run. None must become a needs_human finding — never a pass.
"""
from __future__ import annotations

from ..config import AppConfig
from .backends import (
    FhirValidator,
    HL7ValidatorService,
    JavaCliValidator,
    NullValidator,
)
from .models import (
    SEVERITY_ORDER,
    ValidationMessage,
    ValidationResult,
    ValidatorUnavailable,
)
from .outcome import parse_operation_outcome
from .profiles import US_CORE_PROFILES, profile_for

__all__ = [
    "FhirValidator",
    "HL7ValidatorService",
    "JavaCliValidator",
    "NullValidator",
    "SEVERITY_ORDER",
    "US_CORE_PROFILES",
    "ValidationMessage",
    "ValidationResult",
    "ValidatorUnavailable",
    "get_validator",
    "parse_operation_outcome",
    "profile_for",
]


def get_validator(config: AppConfig) -> FhirValidator:
    """Pick a backend from config.

    VALIDATOR_MODE:
      auto    (default) — try the HTTP service, then the jar, then NullValidator
      service — HTTP service only
      java    — validator_cli.jar only
      off     — NullValidator

    "auto" never silently downgrades to "no validation is fine": the resulting
    NullValidator still reports available=False, which surfaces as needs_human.
    """
    mode = config.validator_mode

    if mode == "off":
        return NullValidator()

    service = HL7ValidatorService(config.validator_service_url, config.validator_timeout_s)
    java = JavaCliValidator(config.validator_jar_path, config.validator_timeout_s)

    if mode == "service":
        return service
    if mode == "java":
        return java

    service_ok, service_detail = service.health()
    if service_ok:
        return service
    java_ok, java_detail = java.health()
    if java_ok:
        return java
    return NullValidator(
        "no FHIR validator available. "
        f"HTTP service: {service_detail}. validator_cli.jar: {java_detail}. "
        "Start one with: podman compose -f docker/fhir-validator.compose.yml up -d"
    )
