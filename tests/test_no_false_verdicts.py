"""Evidence the tool never collected must not turn into a pass or a fail.

Covers the discovery-error and no-token paths found in the first live run
(2026-10-07): a crashed run instead of an AUTH-001 fail, FHIR-001..003/005
failing on 401s, and FHIR-004 passing with nothing inspected.
"""
from __future__ import annotations

import httpx
import pytest

from epic_compliance import pipeline as pipeline_mod
from epic_compliance.config import AppConfig
from epic_compliance.pipeline import TOKEN_UNAVAILABLE_REASON, collect_evidence
from epic_compliance.rules_engine.automated_checks import AUTOMATED_CHECKS
from epic_compliance.rules_engine.catalog import load_rules
from epic_compliance.smart.discovery import MOCK_SMART_CONFIG, REQUIRED_CAPABILITIES
from epic_compliance.viewmodel import SUBJECTS

RULES = {r.id: r for r in load_rules()}
FHIR_RULES = ("FHIR-001", "FHIR-002", "FHIR-003", "FHIR-004", "FHIR-005")


def _finding(rule_id, evidence):
    return AUTOMATED_CHECKS[rule_id](evidence, RULES[rule_id])


class _NoReadClient:
    """Fails the test if the pipeline reads FHIR without a token."""

    def __init__(self, **kw):
        pass

    def fetch_all_us_core(self, patient_id="mock-patient-id"):
        raise AssertionError("FHIR read attempted without an access token")


def test_live_without_token_skips_fhir_reads_and_rules_say_needs_human(monkeypatch):
    monkeypatch.setattr(pipeline_mod, "fetch_smart_configuration", lambda url, mock: MOCK_SMART_CONFIG)
    monkeypatch.setattr(pipeline_mod, "FhirClient", _NoReadClient)
    pipeline_mod.clear_live_token()

    ev = collect_evidence(AppConfig(RUN_MODE="live"))

    assert ev["fhir_resources"] == {"_unavailable": TOKEN_UNAVAILABLE_REASON}
    for rule_id in FHIR_RULES:
        assert _finding(rule_id, ev).verdict == "needs_human", rule_id


def test_discovery_error_is_an_auth_001_fail_not_a_crash(monkeypatch):
    def _boom(url, mock):
        raise httpx.ConnectError("name resolution failed")

    monkeypatch.setattr(pipeline_mod, "fetch_smart_configuration", _boom)
    monkeypatch.setattr(pipeline_mod, "FhirClient", _NoReadClient)
    pipeline_mod.clear_live_token()

    ev = collect_evidence(AppConfig(RUN_MODE="live", FHIR_BASE_URL="https://wrong.example/R4"))

    auth_001 = _finding("AUTH-001", ev)
    assert auth_001.verdict == "fail"
    assert "name resolution failed" in auth_001.evidence
    for rule_id in ("AUTH-002", "AUTH-004", "AUTH-010"):
        finding = _finding(rule_id, ev)
        assert finding.verdict == "needs_human", rule_id
        assert "AUTH-001" in finding.citation


def test_auth_001_names_missing_required_capabilities():
    caps = [c for c in REQUIRED_CAPABILITIES if c != "context-standalone-patient"]
    ev = {"smart_configuration": {"capabilities": caps}}

    finding = _finding("AUTH-001", ev)

    assert finding.verdict == "fail"
    assert "context-standalone-patient" in finding.evidence


def test_auth_001_passes_on_full_capability_set():
    ev = {"smart_configuration": MOCK_SMART_CONFIG.model_dump()}
    assert _finding("AUTH-001", ev).verdict == "pass"


@pytest.mark.parametrize("rule_id,resource_type", [
    ("FHIR-001", "Patient"),
    ("FHIR-002", "Observation"),
    ("FHIR-003", "Condition"),
])
def test_unreadable_resource_is_needs_human_not_fail(rule_id, resource_type):
    ev = {"fhir_resources": {resource_type: {"resourceType": resource_type, "_error": "403 Forbidden"}}}

    finding = _finding(rule_id, ev)

    assert finding.verdict == "needs_human"
    assert "403 Forbidden" in finding.evidence


def test_fhir_004_with_nothing_readable_is_not_a_pass():
    ev = {"fhir_resources": {
        "Patient": {"resourceType": "Patient", "_error": "401 Unauthorized"},
        "Observation": {"resourceType": "Observation", "_empty": True},
    }}
    assert _finding("FHIR-004", ev).verdict == "needs_human"


def test_fhir_005_still_fails_when_token_present_but_resources_missing():
    ev = {"fhir_resources": {"Patient": {"resourceType": "Patient", "_error": "403 Forbidden"}}}
    assert _finding("FHIR-005", ev).verdict == "fail"


def test_every_rule_states_what_it_tests():
    for rule in RULES.values():
        assert rule.subject in SUBJECTS, f"{rule.id} has unknown subject {rule.subject!r}"
