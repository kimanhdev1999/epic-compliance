"""Top-level pipeline orchestrator — wires all stages together."""
from __future__ import annotations

from typing import Any

from .config import AppConfig
from .smart.discovery import fetch_smart_configuration
from .smart.pkce import MOCK_AUTH_PROBE, MOCK_TOKEN_RESPONSE
from .fhir.client import FhirClient
from .rules_engine.engine import run_pipeline
from .models import Report


#: Why a live run has no token. Any evidence dict carrying this key means the
#: token-dependent rules MUST NOT return pass/fail — there is nothing to judge.
TOKEN_UNAVAILABLE_REASON = (
    "Live run: no access token was obtained. The authorization-code flow needs a "
    "browser redirect, which this pipeline does not yet drive (no /callback route). "
    "Token-dependent rules cannot be evaluated."
)


def collect_evidence(config: AppConfig) -> dict[str, Any]:
    """Stage 1-3: discovery, auth probe, FHIR fetch. Returns evidence dict.

    In live mode there is no real token yet, so the token and probe evidence are
    marked unavailable rather than silently substituted with the mock fixture.
    Faking them would make AUTH-003/005 structurally incapable of failing — a
    compliance tool that cannot fail is worse than no tool at all.
    """
    mock = config.run_mode == "mock"

    smart_config = fetch_smart_configuration(config.fhir_base_url, mock=mock)

    if mock:
        token = MOCK_TOKEN_RESPONSE
        token_evidence: dict[str, Any] = {
            "access_token": "[REDACTED]",
            "token_type": token.token_type,
            "expires_in": token.expires_in,
            "scope": token.scope,
            "patient": token.patient,
            "id_token": "[REDACTED]" if token.id_token else "",
            "refresh_token": "[REDACTED]" if token.refresh_token else "",
        }
        auth_probe: dict[str, Any] = dict(MOCK_AUTH_PROBE)
        access_token = token.access_token
    else:
        token_evidence = {"_unavailable": TOKEN_UNAVAILABLE_REASON}
        auth_probe = {"_unavailable": TOKEN_UNAVAILABLE_REASON}
        access_token = ""

    fhir_client = FhirClient(
        base_url=config.fhir_base_url,
        access_token=access_token,
        mock=mock,
    )
    fhir_resources = fhir_client.fetch_all_us_core()

    return {
        "smart_configuration": smart_config.model_dump(),
        "token_response": token_evidence,
        "auth_probe": auth_probe,
        "fhir_resources": fhir_resources,
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
