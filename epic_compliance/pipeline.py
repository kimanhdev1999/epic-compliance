"""Top-level pipeline orchestrator — wires all stages together."""
from __future__ import annotations

from typing import Any

from .config import AppConfig
from .smart.discovery import fetch_smart_configuration
from .smart.pkce import MOCK_AUTH_PROBE, MOCK_TOKEN_RESPONSE, TokenResponse
from .fhir.client import FhirClient
from .fhir.writeback import build_diagnostic_report, build_media, build_observation
from .validator import get_validator
from .rules_engine.engine import run_pipeline
from .models import Report


#: Why a live run has no token. Any evidence dict carrying this key means the
#: token-dependent rules MUST NOT return pass/fail — there is nothing to judge.
TOKEN_UNAVAILABLE_REASON = (
    "Live run: no access token was obtained. Start a launch at GET /launch and "
    "complete it at GET /callback (a browser + Epic login is required) before "
    "running the pipeline. Token-dependent rules cannot be evaluated until then."
)


# --------------------------------------------------------------------------- #
# Live-token holder
#
# api.py's /callback route populates this after a real SMART launch completes.
# Single-tenant, in-process, module-level — deliberately not a database table:
# this holds exactly one "current" live token for the one app this tool is
# configured against, the same single-tenant scope as config.py. It is NOT
# multi-organization state.
# --------------------------------------------------------------------------- #
_live_token_holder: dict[str, Any] = {"token": None, "auth_probe": None}


def set_live_token(token: TokenResponse, auth_probe: dict[str, Any] | None = None) -> None:
    """Called by api.py's /callback after a real authorization-code exchange."""
    _live_token_holder["token"] = token
    _live_token_holder["auth_probe"] = auth_probe or {}


def get_live_token() -> TokenResponse | None:
    return _live_token_holder["token"]


def get_live_auth_probe() -> dict[str, Any]:
    return dict(_live_token_holder["auth_probe"] or {})


def clear_live_token() -> None:
    _live_token_holder["token"] = None
    _live_token_holder["auth_probe"] = None


def collect_writeback_evidence(
    fhir_client: FhirClient,
    validator: Any,
    patient_id: str,
) -> dict[str, Any]:
    """Stage: construct, validate, and POST the write-back resources.

    Mirrors the "direction B" flow this app implements: after an AI diagnosis,
    it POSTs an Observation (the finding), a DiagnosticReport (referencing that
    Observation), and a Media resource (the source photo) into Epic. This
    function performs exactly that — as an outbound client — against whatever
    FhirClient it is given (mock or live), and validates each payload against
    US Core with the existing HL7 validator before/after the POST.

    Returns one entry per resource type:
        {
          "validation_valid": True | False | None,   # tri-state, see ValidationResult.is_valid()
          "validation_summary": str,
          "status_code": int | None,
          "posted": True | False | None,              # None = request itself never completed
          "error": str,
        }
    """
    from .fhir.writeback import WRITE_PROFILES

    def _entry(resource: dict[str, Any]) -> dict[str, Any]:
        rt = resource["resourceType"]
        validation = validator.validate(resource, WRITE_PROFILES.get(rt, ""))
        post = fhir_client.post_resource(resource)
        status_code = post.get("status_code")
        posted = (200 <= status_code < 300) if isinstance(status_code, int) else None
        return {
            "validation_valid": validation.is_valid(),
            "validation_summary": validation.summary(),
            "status_code": status_code,
            "posted": posted,
            "error": post.get("_error", ""),
            "resource_returned": post.get("resource", {}),
        }, post

    observation = build_observation(patient_id)
    obs_entry, obs_post = _entry(observation)

    obs_id = (obs_post.get("resource") or {}).get("id", "") or "pending-observation"
    diagnostic_report = build_diagnostic_report(patient_id, obs_id)
    dr_entry, _ = _entry(diagnostic_report)

    media = build_media(patient_id)
    media_entry, _ = _entry(media)

    return {
        "Observation": obs_entry,
        "DiagnosticReport": dr_entry,
        "Media": media_entry,
    }


def collect_evidence(config: AppConfig) -> dict[str, Any]:
    """Stage 1-3: discovery, auth probe, FHIR fetch, write-back. Returns evidence dict.

    In live mode there is no token until a real launch has completed through
    api.py's /callback route (see set_live_token). Until then, token- and
    write-back-dependent evidence is marked unavailable rather than silently
    substituted with the mock fixture. Faking it would make AUTH-003/005 and
    the WRITE-* rules structurally incapable of failing — a compliance tool
    that cannot fail is worse than no tool at all.
    """
    mock = config.run_mode == "mock"

    smart_config = fetch_smart_configuration(config.fhir_base_url, mock=mock)

    if mock:
        token: TokenResponse | None = MOCK_TOKEN_RESPONSE
        auth_probe: dict[str, Any] | None = dict(MOCK_AUTH_PROBE)
    else:
        token = get_live_token()
        # Only state_validated is actually proven by a normal /callback launch —
        # the other AUTH-007..010 probes are deliberate negative paths (wrong
        # verifier, replayed code, cross-patient read) that a real launch never
        # exercises on purpose. Those stay needs_human until pytest -m launch
        # (or a future interactive "run negative probes" action) supplies them.
        auth_probe = dict(get_live_auth_probe()) if token is not None else None

    if token is not None:
        token_evidence: dict[str, Any] = {
            "access_token": "[REDACTED]",
            "token_type": token.token_type,
            "expires_in": token.expires_in,
            "scope": token.scope,
            "patient": token.patient,
            "id_token": "[REDACTED]" if token.id_token else "",
            "refresh_token": "[REDACTED]" if token.refresh_token else "",
        }
        access_token = token.access_token
        patient_id = token.patient or "mock-patient-id"
    else:
        token_evidence = {"_unavailable": TOKEN_UNAVAILABLE_REASON}
        auth_probe = {"_unavailable": TOKEN_UNAVAILABLE_REASON}
        access_token = ""
        patient_id = "mock-patient-id"

    fhir_client = FhirClient(
        base_url=config.fhir_base_url,
        access_token=access_token,
        mock=mock,
    )
    fhir_resources = fhir_client.fetch_all_us_core(patient_id)

    if token is not None:
        validator = get_validator(config)
        write_back = collect_writeback_evidence(fhir_client, validator, patient_id)
    else:
        write_back = {"_unavailable": TOKEN_UNAVAILABLE_REASON}

    app_config_fields = {
        "transport_tls_version": config.transport_tls_version,
        "audit_logging_enabled": config.audit_logging_enabled,
        "audit_log_retention_days": config.audit_log_retention_days,
        "baa_in_place": config.baa_in_place,
        "covered_entity_relationship": config.covered_entity_relationship,
    }
    attested = {k: v for k, v in app_config_fields.items() if v}
    app_config = attested if attested else {
        "_unavailable": "No app_config fields attested — set TRANSPORT_TLS_VERSION, "
        "AUDIT_LOGGING_ENABLED, AUDIT_LOG_RETENTION_DAYS, BAA_IN_PLACE, "
        "COVERED_ENTITY_RELATIONSHIP in .env or the dashboard form."
    }

    return {
        "smart_configuration": smart_config.model_dump(),
        "token_response": token_evidence,
        "auth_probe": auth_probe,
        "fhir_resources": fhir_resources,
        "write_back": write_back,
        "app_config": app_config,
        "run_mode": config.run_mode,
    }


def run_full_pipeline(config: AppConfig) -> Report:
    """Run the complete pipeline: evidence collection + validation."""
    evidence = collect_evidence(config)
    mock_llm = not bool(config.anthropic_api_key) or config.run_mode == "mock"
    return run_pipeline(
        evidence=evidence,
        api_key=config.anthropic_api_key,
        mock_llm=mock_llm,
    )
