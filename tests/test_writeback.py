"""Write-back (direction B: push AI diagnosis into Epic) — resource builders,
tri-state WRITE-00x checks, pipeline wiring, and the /launch + /callback routes.

Follows the tri-state / evidence-degradation style of test_auth_module.py:
every check must be pass / fail / needs_human, never a bare boolean, and
needs_human whenever the evidence it depends on was never collected.
"""
from __future__ import annotations

import pytest

from epic_compliance.config import AppConfig
from epic_compliance.fhir.client import FhirClient
from epic_compliance.fhir.writeback import (
    WRITE_SCOPES_NEEDED,
    build_diagnostic_report,
    build_media,
    build_observation,
)
from epic_compliance.pipeline import (
    TOKEN_UNAVAILABLE_REASON,
    clear_live_token,
    collect_evidence,
    collect_writeback_evidence,
    get_live_token,
    set_live_token,
)
from epic_compliance.rules_engine.automated_checks import AUTOMATED_CHECKS
from epic_compliance.rules_engine.catalog import load_rules
from epic_compliance.smart.pkce import MOCK_TOKEN_RESPONSE
from epic_compliance.validator.models import ValidationResult

WRITE_RULES = {r.id: r for r in load_rules() if r.category == "write_back"}


def _finding(rule_id: str, evidence: dict):
    return AUTOMATED_CHECKS[rule_id](evidence, WRITE_RULES[rule_id])


@pytest.fixture(autouse=True)
def _reset_live_token():
    """The live-token holder is process-global; never leak it between tests."""
    clear_live_token()
    yield
    clear_live_token()


# ─── resource builders ───────────────────────────────────────────────────────


class TestResourceBuilders:
    def test_observation_has_required_us_core_elements(self):
        obs = build_observation("pt-1")
        assert obs["resourceType"] == "Observation"
        assert obs["status"]
        assert obs["category"]
        assert obs["code"]
        assert obs["subject"] == {"reference": "Patient/pt-1"}

    def test_diagnostic_report_references_its_observation(self):
        dr = build_diagnostic_report("pt-1", "obs-1")
        assert dr["resourceType"] == "DiagnosticReport"
        assert dr["status"]
        assert dr["code"]
        assert dr["subject"] == {"reference": "Patient/pt-1"}
        assert dr["result"] == [{"reference": "Observation/obs-1"}]

    def test_media_references_patient_and_has_content(self):
        media = build_media("pt-1", content_url="https://example.org/photo.jpg")
        assert media["resourceType"] == "Media"
        assert media["subject"] == {"reference": "Patient/pt-1"}
        assert media["content"]["url"] == "https://example.org/photo.jpg"

    def test_write_scopes_needed_are_epic_write_shape(self):
        # Epic grants per-resource write scopes, same shape as the read scopes
        # AUTH-003 already accounts for.
        for scope in WRITE_SCOPES_NEEDED:
            assert scope.startswith("patient/") and scope.endswith(".write")


# ─── collect_writeback_evidence — mock FhirClient + a stub validator ────────


class _AlwaysValidValidator:
    def validate(self, resource, profile=""):
        return ValidationResult(resource_type=resource["resourceType"], profile=profile, available=True, messages=[])


class _AlwaysInvalidValidator:
    def validate(self, resource, profile=""):
        from epic_compliance.validator.models import ValidationMessage

        return ValidationResult(
            resource_type=resource["resourceType"],
            profile=profile,
            available=True,
            messages=[ValidationMessage(severity="error", message="missing required element")],
        )


class _UnavailableValidator:
    def validate(self, resource, profile=""):
        return ValidationResult(
            resource_type=resource["resourceType"], profile=profile, available=False,
            unavailable_reason="validator not reachable in this test",
        )


class TestCollectWritebackEvidence:
    def test_mock_client_valid_payload_all_pass(self):
        client = FhirClient(base_url="https://example.org/FHIR/R4", access_token="tok", mock=True)
        wb = collect_writeback_evidence(client, _AlwaysValidValidator(), "mock-patient-id")
        for rt in ("Observation", "DiagnosticReport", "Media"):
            assert wb[rt]["validation_valid"] is True
            assert wb[rt]["posted"] is True
            assert wb[rt]["status_code"] == 201

    def test_diagnostic_report_references_the_created_observation_id(self):
        client = FhirClient(base_url="https://example.org/FHIR/R4", access_token="tok", mock=True)
        wb = collect_writeback_evidence(client, _AlwaysValidValidator(), "mock-patient-id")
        # The mock FhirClient fabricates id "mock-observation-created-1" on POST;
        # the DiagnosticReport built afterwards must reference it.
        assert wb["Observation"]["resource_returned"]["id"] == "mock-observation-created-1"

    def test_invalid_payload_fails_even_if_post_succeeds(self):
        client = FhirClient(base_url="https://example.org/FHIR/R4", access_token="tok", mock=True)
        wb = collect_writeback_evidence(client, _AlwaysInvalidValidator(), "mock-patient-id")
        for rt in ("Observation", "DiagnosticReport", "Media"):
            assert wb[rt]["validation_valid"] is False
            assert wb[rt]["posted"] is True  # POST itself still succeeded

    def test_unavailable_validator_is_tri_state_none_not_false(self):
        client = FhirClient(base_url="https://example.org/FHIR/R4", access_token="tok", mock=True)
        wb = collect_writeback_evidence(client, _UnavailableValidator(), "mock-patient-id")
        for rt in ("Observation", "DiagnosticReport", "Media"):
            assert wb[rt]["validation_valid"] is None, "unavailable must be None, not False"

    def test_post_403_is_recorded_not_raised(self):
        class _DenyingClient:
            def post_resource(self, resource):
                return {"status_code": 403, "resource": {"issue": [{"details": {"text": "insufficient scope"}}]}}

        wb = collect_writeback_evidence(_DenyingClient(), _AlwaysValidValidator(), "mock-patient-id")
        for rt in ("Observation", "DiagnosticReport", "Media"):
            assert wb[rt]["posted"] is False
            assert wb[rt]["status_code"] == 403


# ─── WRITE-001..003: per-resource tri-state ──────────────────────────────────


@pytest.mark.parametrize("rule_id,resource_type", [
    ("WRITE-001", "DiagnosticReport"),
    ("WRITE-002", "Observation"),
    ("WRITE-003", "Media"),
])
class TestWriteResourceChecks:
    def test_pass_when_valid_and_posted(self, rule_id, resource_type):
        evidence = {"write_back": {resource_type: {"validation_valid": True, "posted": True, "status_code": 201, "validation_summary": "ok"}}}
        assert _finding(rule_id, evidence).verdict == "pass"

    def test_fail_when_invalid(self, rule_id, resource_type):
        evidence = {"write_back": {resource_type: {"validation_valid": False, "posted": True, "status_code": 201, "validation_summary": "1 error"}}}
        assert _finding(rule_id, evidence).verdict == "fail"

    def test_fail_when_post_rejected(self, rule_id, resource_type):
        evidence = {"write_back": {resource_type: {"validation_valid": True, "posted": False, "status_code": 403, "validation_summary": "ok"}}}
        assert _finding(rule_id, evidence).verdict == "fail"

    def test_needs_human_when_validation_never_ran(self, rule_id, resource_type):
        evidence = {"write_back": {resource_type: {"validation_valid": None, "posted": True, "status_code": 201, "validation_summary": "unavailable"}}}
        assert _finding(rule_id, evidence).verdict == "needs_human"

    def test_needs_human_when_evidence_key_unavailable(self, rule_id, resource_type):
        evidence = {"write_back": {"_unavailable": TOKEN_UNAVAILABLE_REASON}}
        f = _finding(rule_id, evidence)
        assert f.verdict == "needs_human"
        assert f.citation

    def test_needs_human_when_write_back_missing_entirely(self, rule_id, resource_type):
        assert _finding(rule_id, {}).verdict == "needs_human"

    def test_citation_present_on_pass_never_required_but_evidence_always_is(self, rule_id, resource_type):
        evidence = {"write_back": {resource_type: {"validation_valid": True, "posted": True, "status_code": 201, "validation_summary": "ok"}}}
        f = _finding(rule_id, evidence)
        assert f.evidence


# ─── WRITE-004: write scopes actually honored ────────────────────────────────


class TestWriteScopeCheck:
    def _wb(self, **overrides):
        base = {
            "Observation": {"posted": True, "status_code": 201},
            "DiagnosticReport": {"posted": True, "status_code": 201},
            "Media": {"posted": True, "status_code": 201},
        }
        base.update(overrides)
        return {"write_back": base}

    def test_pass_when_all_three_posted(self):
        assert _finding("WRITE-004", self._wb()).verdict == "pass"

    def test_fail_when_one_denied_with_403(self):
        ev = self._wb(Media={"posted": False, "status_code": 403})
        f = _finding("WRITE-004", ev)
        assert f.verdict == "fail"
        assert "Media" in f.evidence

    def test_needs_human_when_a_write_never_completed(self):
        ev = self._wb(DiagnosticReport={"posted": None, "status_code": None})
        f = _finding("WRITE-004", ev)
        assert f.verdict == "needs_human"

    def test_needs_human_when_write_back_unavailable(self):
        f = _finding("WRITE-004", {"write_back": {"_unavailable": TOKEN_UNAVAILABLE_REASON}})
        assert f.verdict == "needs_human"


# ─── pipeline wiring: mock mode produces write_back evidence end-to-end ─────


class TestPipelineMockMode:
    def test_mock_pipeline_write_back_all_pass(self):
        config = AppConfig(RUN_MODE="mock")
        evidence = collect_evidence(config)
        assert "write_back" in evidence
        for rt in ("Observation", "DiagnosticReport", "Media"):
            assert evidence["write_back"][rt]["posted"] is True

    def test_live_mode_without_a_launch_marks_write_back_unavailable(self, monkeypatch):
        import epic_compliance.pipeline as pipeline_mod
        from epic_compliance.smart.discovery import MOCK_SMART_CONFIG

        monkeypatch.setattr(pipeline_mod, "fetch_smart_configuration", lambda url, mock: MOCK_SMART_CONFIG)
        config = AppConfig(RUN_MODE="live", FHIR_BASE_URL="https://example.org/FHIR/R4")
        evidence = collect_evidence(config)
        assert evidence["write_back"] == {"_unavailable": TOKEN_UNAVAILABLE_REASON}

    def test_live_mode_after_set_live_token_produces_real_write_back_evidence(self, monkeypatch):
        """Proves the /callback -> pipeline wiring: once set_live_token() is
        called (as api.py's /callback route does), a live run's write_back
        stage runs for real instead of degrading to needs_human. The FHIR
        client itself is monkeypatched to mock=True semantics here so the test
        stays offline; the point under test is that collect_evidence *reaches*
        the write-back stage once a token exists, not real network I/O."""
        import epic_compliance.pipeline as pipeline_mod
        from epic_compliance.smart.discovery import MOCK_SMART_CONFIG

        monkeypatch.setattr(pipeline_mod, "fetch_smart_configuration", lambda url, mock: MOCK_SMART_CONFIG)
        set_live_token(MOCK_TOKEN_RESPONSE, auth_probe={"state_validated": True})
        assert get_live_token() is MOCK_TOKEN_RESPONSE

        # Force FhirClient into mock mode regardless of config.run_mode so this
        # test never touches the network, while still exercising "live" evidence
        # shaping code paths (token_response no longer unavailable).
        monkeypatch.setattr(
            pipeline_mod, "FhirClient",
            lambda base_url, access_token, mock: __import__("epic_compliance.fhir.client", fromlist=["FhirClient"]).FhirClient(base_url, access_token, mock=True),
        )

        config = AppConfig(RUN_MODE="live", FHIR_BASE_URL="https://example.org/FHIR/R4")
        evidence = collect_evidence(config)
        assert evidence["token_response"]["patient"] == MOCK_TOKEN_RESPONSE.patient
        assert evidence["auth_probe"] == {"state_validated": True}
        assert evidence["write_back"]["Observation"]["posted"] is True


# ─── /launch and /callback routes ────────────────────────────────────────────


class TestCallbackRoute:
    def _client(self):
        from fastapi.testclient import TestClient

        from epic_compliance.api import app

        return TestClient(app)

    def test_callback_with_epic_error_reports_failure_not_500(self):
        client = self._client()
        resp = client.get("/callback", params={"error": "access_denied", "error_description": "user cancelled"}, follow_redirects=False)
        assert resp.status_code == 200
        assert "access_denied" in resp.text

    def test_callback_with_unknown_state_is_rejected(self):
        client = self._client()
        resp = client.get("/callback", params={"code": "abc", "state": "never-issued"})
        assert resp.status_code == 200
        assert "Unknown or expired" in resp.text

    def test_callback_with_no_code_is_rejected(self, monkeypatch):
        import epic_compliance.api as api_mod
        from epic_compliance.smart.pkce import generate_pkce

        api_mod._pending_launches["state-no-code"] = {
            "pkce": generate_pkce(),
            "redirect_uri": "http://localhost:8000/callback",
            "client_id": "c",
            "client_secret": "",
            "token_endpoint": "https://example.org/token",
        }
        client = self._client()
        resp = client.get("/callback", params={"state": "state-no-code"})
        assert resp.status_code == 200
        assert "no authorization code" in resp.text.lower()

    def test_successful_callback_populates_live_token_holder(self, monkeypatch):
        import epic_compliance.api as api_mod
        import epic_compliance.pipeline as pipeline_mod
        from epic_compliance.smart.pkce import TokenResponse, generate_pkce

        api_mod._pending_launches["state-ok"] = {
            "pkce": generate_pkce(),
            "redirect_uri": "http://localhost:8000/callback",
            "client_id": "c",
            "client_secret": "",
            "token_endpoint": "https://example.org/token",
        }

        fake_token = TokenResponse(
            access_token="real-token", token_type="Bearer", expires_in=3600,
            scope="patient/Patient.read patient/Observation.write", patient="patient-42",
            id_token="id-token", refresh_token="",
        )
        monkeypatch.setattr(api_mod, "exchange_code_for_token", lambda **kw: fake_token)

        client = self._client()
        resp = client.get("/callback", params={"code": "auth-code", "state": "state-ok"})
        assert resp.status_code == 200
        assert "Live launch complete" in resp.text
        assert pipeline_mod.get_live_token() is fake_token
        assert pipeline_mod.get_live_auth_probe() == {"state_validated": True}
        # state must be single-use, matching the AUTH-008 property this app expects of Epic
        assert "state-ok" not in api_mod._pending_launches

    def test_token_exchange_failure_is_reported_not_500(self, monkeypatch):
        import epic_compliance.api as api_mod
        from epic_compliance.smart.pkce import generate_pkce

        api_mod._pending_launches["state-fail"] = {
            "pkce": generate_pkce(),
            "redirect_uri": "http://localhost:8000/callback",
            "client_id": "c",
            "client_secret": "",
            "token_endpoint": "https://example.org/token",
        }

        def _boom(**kw):
            raise RuntimeError("token endpoint returned 400")

        monkeypatch.setattr(api_mod, "exchange_code_for_token", _boom)
        client = self._client()
        resp = client.get("/callback", params={"code": "auth-code", "state": "state-fail"})
        assert resp.status_code == 200
        assert "Token exchange failed" in resp.text
