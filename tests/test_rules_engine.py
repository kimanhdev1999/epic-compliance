"""Tests for the rule engine and findings aggregation."""
from __future__ import annotations

import pytest

from epic_compliance.models import Finding, Report
from epic_compliance.rules_engine.catalog import load_rules
from epic_compliance.rules_engine.engine import run_pipeline
from epic_compliance.rules_engine.automated_checks import AUTOMATED_CHECKS
from epic_compliance.smart.discovery import MOCK_SMART_CONFIG
from epic_compliance.smart.pkce import MOCK_TOKEN_RESPONSE
from epic_compliance.fhir.client import MOCK_FHIR_RESOURCES


@pytest.fixture
def mock_evidence():
    return {
        "smart_configuration": MOCK_SMART_CONFIG.model_dump(),
        "token_response": {
            "access_token": "[REDACTED]",
            "token_type": "Bearer",
            "expires_in": 3600,
            "scope": MOCK_TOKEN_RESPONSE.scope,
            "patient": MOCK_TOKEN_RESPONSE.patient,
            "id_token": "mock-id-token",
            "refresh_token": "mock-refresh-token",
        },
        "fhir_resources": MOCK_FHIR_RESOURCES,
        "run_mode": "mock",
    }


class TestRuleCatalog:
    def test_loads_rules(self):
        rules = load_rules()
        assert len(rules) >= 8, "Expected at least 8 seeded rules"

    def test_all_rules_have_required_fields(self):
        rules = load_rules()
        for rule in rules:
            assert rule.id, f"Rule missing id: {rule}"
            assert rule.category
            assert rule.check_type in ("automated", "llm"), f"Bad check_type: {rule.id}"
            assert rule.severity
            assert rule.description

    def test_rule_ids_unique(self):
        rules = load_rules()
        ids = [r.id for r in rules]
        assert len(ids) == len(set(ids)), "Duplicate rule IDs found"


class TestAutomatedChecks:
    def test_auth_001_pass(self, mock_evidence):
        rules = {r.id: r for r in load_rules()}
        finding = AUTOMATED_CHECKS["AUTH-001"](mock_evidence, rules["AUTH-001"])
        assert finding.verdict == "pass"
        assert finding.rule_id == "AUTH-001"
        assert finding.evidence

    def test_auth_002_pkce_s256(self, mock_evidence):
        rules = {r.id: r for r in load_rules()}
        finding = AUTOMATED_CHECKS["AUTH-002"](mock_evidence, rules["AUTH-002"])
        assert finding.verdict == "pass"
        assert "S256" in finding.evidence

    def test_auth_002_fail_when_no_s256(self, mock_evidence):
        rules = {r.id: r for r in load_rules()}
        evidence = dict(mock_evidence)
        sc = dict(evidence["smart_configuration"])
        sc["code_challenge_methods_supported"] = []
        evidence["smart_configuration"] = sc
        finding = AUTOMATED_CHECKS["AUTH-002"](evidence, rules["AUTH-002"])
        assert finding.verdict == "fail"

    def test_auth_003_scope_check(self, mock_evidence):
        rules = {r.id: r for r in load_rules()}
        finding = AUTOMATED_CHECKS["AUTH-003"](mock_evidence, rules["AUTH-003"])
        assert finding.verdict == "pass"

    def test_fhir_001_patient_pass(self, mock_evidence):
        rules = {r.id: r for r in load_rules()}
        finding = AUTOMATED_CHECKS["FHIR-001"](mock_evidence, rules["FHIR-001"])
        assert finding.verdict == "pass"

    def test_fhir_001_patient_fail_missing_dob(self, mock_evidence):
        rules = {r.id: r for r in load_rules()}
        evidence = dict(mock_evidence)
        resources = dict(evidence["fhir_resources"])
        patient = dict(resources["Patient"])
        del patient["birthDate"]
        resources["Patient"] = patient
        evidence["fhir_resources"] = resources
        finding = AUTOMATED_CHECKS["FHIR-001"](evidence, rules["FHIR-001"])
        assert finding.verdict == "fail"

    def test_fhir_005_all_resources_present(self, mock_evidence):
        rules = {r.id: r for r in load_rules()}
        finding = AUTOMATED_CHECKS["FHIR-005"](mock_evidence, rules["FHIR-005"])
        assert finding.verdict == "pass"


class TestFindingsAggregation:
    def test_finding_model_fields(self):
        f = Finding(
            rule_id="TEST-001",
            verdict="pass",
            evidence="test evidence",
            remediation_hint="do something",
        )
        assert f.rule_id == "TEST-001"
        assert f.verdict == "pass"
        assert f.evidence == "test evidence"

    def test_report_summary_counts(self):
        findings = [
            Finding(rule_id="A", verdict="pass", evidence="e1", category="auth"),
            Finding(rule_id="B", verdict="fail", evidence="e2", category="auth"),
            Finding(rule_id="C", verdict="needs_human", evidence="e3", category="security"),
        ]
        report = Report(run_id="test-run-id", findings=findings)
        summary = report.summary()
        assert summary["counts"]["pass"] == 1
        assert summary["counts"]["fail"] == 1
        assert summary["counts"]["needs_human"] == 1
        assert "auth" in summary["by_category"]
        assert "security" in summary["by_category"]

    def test_full_pipeline_mock_mode(self, mock_evidence):
        report = run_pipeline(evidence=mock_evidence, api_key="", mock_llm=True)
        assert report.run_id
        assert len(report.findings) >= 8
        # All findings are traceable
        for f in report.findings:
            assert f.rule_id
            assert f.verdict in ("pass", "fail", "needs_human")
            assert f.evidence

    def test_llm_findings_always_have_citation(self, mock_evidence):
        report = run_pipeline(evidence=mock_evidence, api_key="", mock_llm=True)
        llm_findings = [f for f in report.findings if f.check_type == "llm"]
        assert len(llm_findings) > 0, "Expected some LLM findings"
        for f in llm_findings:
            assert f.citation, f"LLM finding {f.rule_id} missing citation"

    def test_no_bare_boolean_verdicts(self, mock_evidence):
        """Every finding must carry rule_id, verdict, and evidence — no bare booleans."""
        report = run_pipeline(evidence=mock_evidence, api_key="", mock_llm=True)
        for f in report.findings:
            assert isinstance(f.rule_id, str) and f.rule_id
            assert isinstance(f.verdict, str) and f.verdict in ("pass", "fail", "needs_human")
            assert isinstance(f.evidence, str) and f.evidence


class TestPKCE:
    def test_pkce_generation(self):
        from epic_compliance.smart.pkce import generate_pkce
        pkce = generate_pkce()
        assert len(pkce.code_verifier) >= 43
        assert len(pkce.code_challenge) >= 43
        assert pkce.code_challenge_method == "S256"
        assert pkce.code_verifier != pkce.code_challenge

    def test_pkce_s256_correctness(self):
        import base64
        import hashlib
        from epic_compliance.smart.pkce import generate_pkce
        pkce = generate_pkce()
        digest = hashlib.sha256(pkce.code_verifier.encode("ascii")).digest()
        expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
        assert pkce.code_challenge == expected

    def test_build_authorization_url(self):
        from epic_compliance.smart.pkce import generate_pkce, build_authorization_url
        pkce = generate_pkce()
        url = build_authorization_url(
            authorization_endpoint="https://example.com/auth",
            client_id="my-client",
            redirect_uri="http://localhost/callback",
            scopes=["openid", "patient/*.read"],
            pkce=pkce,
            state="abc123",
        )
        assert "response_type=code" in url
        assert "code_challenge_method=S256" in url
        assert pkce.code_challenge in url
        assert "state=abc123" in url
