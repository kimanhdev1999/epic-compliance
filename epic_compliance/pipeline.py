"""Top-level pipeline orchestrator — wires all stages together."""
from __future__ import annotations

from typing import Any

from .config import AppConfig
from .smart.discovery import fetch_smart_configuration
from .smart.pkce import MOCK_TOKEN_RESPONSE
from .fhir.client import FhirClient
from .rules_engine.engine import run_pipeline
from .models import Report


def collect_evidence(config: AppConfig) -> dict[str, Any]:
    """Stage 1-3: discovery, auth probe, FHIR fetch. Returns evidence dict."""
    mock = config.run_mode == "mock"

    # Auth probe
    smart_config = fetch_smart_configuration(config.fhir_base_url, mock=mock)
    token = MOCK_TOKEN_RESPONSE  # STUB: live mode requires browser redirect for real token

    # FHIR fetch
    fhir_client = FhirClient(
        base_url=config.fhir_base_url,
        access_token=token.access_token,
        mock=mock,
    )
    fhir_resources = fhir_client.fetch_all_us_core()

    return {
        "smart_configuration": smart_config.model_dump(),
        "token_response": {
            "access_token": "[REDACTED]",
            "token_type": token.token_type,
            "expires_in": token.expires_in,
            "scope": token.scope,
            "patient": token.patient,
            "id_token": "[REDACTED]" if token.id_token else "",
            "refresh_token": "[REDACTED]" if token.refresh_token else "",
        },
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
