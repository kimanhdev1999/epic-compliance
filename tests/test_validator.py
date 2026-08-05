"""Day 15 — HL7 FHIR validator integration.

No test here runs a real validator: the point is that the *wiring* is correct and
that an unreachable validator degrades to "unknown" rather than "valid".
"""
from __future__ import annotations

import json

import httpx
import pytest

from epic_compliance.config import AppConfig
from epic_compliance.validator import (
    US_CORE_PROFILES,
    HL7ValidatorService,
    JavaCliValidator,
    NullValidator,
    get_validator,
    parse_operation_outcome,
    profile_for,
)
from epic_compliance.validator.backends import build_validation_request

PATIENT = {"resourceType": "Patient", "id": "p1"}

# Shape the HL7 validator actually returns.
OUTCOME = {
    "resourceType": "OperationOutcome",
    "issue": [
        {
            "severity": "information",
            "code": "informational",
            "details": {"text": "All OK"},
            "expression": ["Patient"],
        },
        {
            "severity": "error",
            "code": "structure",
            "details": {"text": "Patient.name: minimum required = 1, but only found 0"},
            "expression": ["Patient.name"],
        },
        {
            "severity": "warning",
            "code": "code-invalid",
            "details": {"text": "Code not verified"},
            "location": ["Patient.gender"],
        },
    ],
}


# ─── OperationOutcome parsing ────────────────────────────────────────────────


def test_parse_orders_most_severe_first():
    msgs = parse_operation_outcome(OUTCOME)
    assert [m.severity for m in msgs] == ["error", "warning", "information"]


def test_parse_extracts_location_from_expression_or_location():
    by_sev = {m.severity: m for m in parse_operation_outcome(OUTCOME)}
    assert by_sev["error"].location == "Patient.name"      # expression
    assert by_sev["warning"].location == "Patient.gender"  # legacy location


def test_unknown_severity_is_promoted_to_error_not_dropped():
    msgs = parse_operation_outcome(
        {"issue": [{"severity": "bogus", "details": {"text": "?"}}]}
    )
    assert len(msgs) == 1 and msgs[0].severity == "error"


def test_terminology_noise_is_dropped():
    noisy = {"issue": [{"severity": "warning",
                        "details": {"text": "Unable to connect to terminology server"}}]}
    assert parse_operation_outcome(noisy) == []
    assert len(parse_operation_outcome(noisy, drop_noise=False)) == 1


@pytest.mark.parametrize("junk", [{}, {"issue": []}, {"issue": None}, "not-a-dict"])
def test_parse_survives_junk(junk):
    assert parse_operation_outcome(junk) == []


# ─── ValidationResult semantics ──────────────────────────────────────────────


def _service_returning(monkeypatch, body, status=200):
    def fake_post(url, params=None, json=None, timeout=None):
        return httpx.Response(status, json=body, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)
    return HL7ValidatorService("http://localhost:3500")


def test_errors_make_the_resource_invalid(monkeypatch):
    result = _service_returning(monkeypatch, OUTCOME).validate(PATIENT, US_CORE_PROFILES["Patient"])
    assert result.is_valid() is False
    assert len(result.errors) == 1
    assert len(result.warnings) == 1
    assert result.backend == "hl7-validator-service"


def test_warnings_alone_still_conform(monkeypatch):
    body = {"issue": [{"severity": "warning", "details": {"text": "style nit"}}]}
    assert _service_returning(monkeypatch, body).validate(PATIENT).is_valid() is True


def test_unreachable_validator_is_unknown_not_valid(monkeypatch):
    """The whole point of Day 15's error handling."""
    def boom(*a, **kw):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "post", boom)
    result = HL7ValidatorService("http://localhost:3500").validate(PATIENT)
    assert result.available is False
    assert result.is_valid() is None, "unreachable must never read as valid"
    assert "connection refused" in result.unavailable_reason


def test_http_error_is_unknown_not_valid(monkeypatch):
    result = _service_returning(monkeypatch, {"x": 1}, status=500).validate(PATIENT)
    assert result.is_valid() is None


def test_null_validator_is_always_unknown():
    result = NullValidator("switched off").validate(PATIENT)
    assert result.is_valid() is None
    assert "switched off" in result.summary()


def test_health_reports_unreachable(monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda *a, **kw: (_ for _ in ()).throw(httpx.ConnectError("nope")))
    ok, detail = HL7ValidatorService("http://localhost:3500").health()
    assert ok is False and "cannot reach validator" in detail


# ─── Java CLI backend ────────────────────────────────────────────────────────


def test_java_backend_reports_missing_jar():
    ok, detail = JavaCliValidator("").health()
    assert ok is False and "not set" in detail


def test_java_backend_reports_missing_file():
    ok, detail = JavaCliValidator("/nope/validator_cli.jar").health()
    assert ok is False and "not found" in detail


def test_java_backend_validate_degrades_when_unusable():
    result = JavaCliValidator("/nope/validator_cli.jar").validate(PATIENT)
    assert result.is_valid() is None and result.available is False


# ─── Backend selection ───────────────────────────────────────────────────────


def test_mode_off_selects_null():
    assert isinstance(get_validator(AppConfig(VALIDATOR_MODE="off")), NullValidator)


def test_mode_service_selects_service_without_probing():
    v = get_validator(AppConfig(VALIDATOR_MODE="service", VALIDATOR_SERVICE_URL="http://x:3500"))
    assert isinstance(v, HL7ValidatorService) and v.base_url == "http://x:3500"


def test_mode_java_selects_java():
    assert isinstance(get_validator(AppConfig(VALIDATOR_MODE="java")), JavaCliValidator)


def test_auto_falls_back_to_null_with_an_actionable_reason(monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda *a, **kw: (_ for _ in ()).throw(httpx.ConnectError("down")))
    v = get_validator(AppConfig(VALIDATOR_MODE="auto", VALIDATOR_JAR_PATH=""))
    assert isinstance(v, NullValidator)
    assert "fhir-validator.compose.yml" in v.reason, "tell the user how to fix it"


def test_auto_prefers_the_service_when_it_is_up(monkeypatch):
    monkeypatch.setattr(
        httpx, "get",
        lambda url, **kw: httpx.Response(200, text="6.3.11", request=httpx.Request("GET", url)),
    )
    assert isinstance(get_validator(AppConfig(VALIDATOR_MODE="auto")), HL7ValidatorService)


# ─── Profile selection ───────────────────────────────────────────────────────


def test_profile_defaults_by_resource_type():
    assert profile_for({"resourceType": "Condition"}) == US_CORE_PROFILES["Condition"]


def test_profile_prefers_what_the_server_declares():
    vital = "http://hl7.org/fhir/us/core/StructureDefinition/us-core-vital-signs"
    resource = {"resourceType": "Observation", "meta": {"profile": [vital]}}
    assert profile_for(resource) == vital, "validate against the profile the server claims"


def test_profile_ignores_non_us_core_declarations():
    resource = {"resourceType": "Patient", "meta": {"profile": ["http://example.org/custom"]}}
    assert profile_for(resource) == US_CORE_PROFILES["Patient"]


def test_unknown_resource_type_has_no_profile():
    assert profile_for({"resourceType": "Nonsense"}) == ""


# ─── Request shape ───────────────────────────────────────────────────────────


def test_request_uses_the_validation_request_envelope():
    """Posting a bare resource gets HTTP 500 from the real service (verified
    against inferno-resource-validator 1.0.78): it needs a ValidationRequest."""
    req = build_validation_request(PATIENT, US_CORE_PROFILES["Patient"])

    assert req["validationContext"]["sv"] == "4.0.1"
    assert req["validationContext"]["profiles"] == [US_CORE_PROFILES["Patient"]]

    files = req["filesToValidate"]
    assert len(files) == 1
    assert files[0]["fileType"] == "json"
    assert files[0]["fileName"] == "Patient.json"
    # content is an escaped JSON *string*, not a nested object
    assert isinstance(files[0]["fileContent"], str)
    assert json.loads(files[0]["fileContent"]) == PATIENT


def test_request_omits_profiles_when_none_given():
    req = build_validation_request(PATIENT)
    assert "profiles" not in req["validationContext"]
    assert "sessionId" not in req


def test_request_loads_the_us_core_ig():
    """Naming a profile without its IG gets 'Unable to resolve profile' (500)."""
    req = build_validation_request(PATIENT, US_CORE_PROFILES["Patient"])
    assert req["validationContext"]["igs"] == ["hl7.fhir.us.core#3.1.1"]


def test_ig_list_is_overridable():
    req = build_validation_request(PATIENT, igs=["hl7.fhir.us.core#6.1.0"])
    assert req["validationContext"]["igs"] == ["hl7.fhir.us.core#6.1.0"]


def test_post_target_normalises_a_trailing_slash(monkeypatch):
    seen: dict = {}

    def fake_post(url, json=None, timeout=None):
        seen.update(url=url, body=json)
        return httpx.Response(200, json={"issue": []}, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)
    HL7ValidatorService("http://localhost:3500/").validate(PATIENT, US_CORE_PROFILES["Patient"])

    assert seen["url"] == "http://localhost:3500/validate"
    assert "filesToValidate" in seen["body"]


@pytest.mark.parametrize(
    "payload",
    [
        OUTCOME,                                   # bare OperationOutcome
        [OUTCOME],                                 # list, one per file
        {"outcomes": [{"issues": OUTCOME}]},       # wrapped envelope
    ],
)
def test_response_shapes_all_parse(monkeypatch, payload):
    """The wrapper's response shape varies by version; all must yield findings."""
    monkeypatch.setattr(
        httpx, "post",
        lambda url, **kw: httpx.Response(200, json=payload, request=httpx.Request("POST", url)),
    )
    result = HL7ValidatorService("http://localhost:3500").validate(PATIENT)
    assert result.is_valid() is False
    assert result.errors[0].location == "Patient.name"


def test_health_parses_the_version_json(monkeypatch):
    body = {"validatorWrapperVersion": "1.0.78", "validatorVersion": "6.9.7"}
    monkeypatch.setattr(
        httpx, "get",
        lambda url, **kw: httpx.Response(200, json=body, request=httpx.Request("GET", url)),
    )
    ok, detail = HL7ValidatorService("http://localhost:3500").health()
    assert ok is True
    assert "6.9.7" in detail and "1.0.78" in detail
