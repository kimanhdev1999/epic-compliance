"""Day 14 — unit tests for the auth module and the auth rules.

Covers the gaps Day 13 left open:
  1. live mode must not substitute the mock token (false pass)
  2. token-dependent rules degrade to needs_human when evidence is absent
  3. state validation (CSRF)
  4. token exchange parsing and error paths
  5. the new AUTH-006..010 negative-path rules
"""
from __future__ import annotations

import httpx
import pytest

from epic_compliance.config import AppConfig
from epic_compliance.pipeline import TOKEN_UNAVAILABLE_REASON, collect_evidence
from epic_compliance.rules_engine.automated_checks import AUTOMATED_CHECKS
from epic_compliance.rules_engine.catalog import load_rules
from epic_compliance.smart import pkce as pkce_mod
from epic_compliance.smart.pkce import (
    AUTH_PROBE_FIELDS,
    MOCK_AUTH_PROBE,
    exchange_code_for_token,
    validate_state,
)

AUTH_RULES = {r.id: r for r in load_rules() if r.category == "auth"}
ALL_RULES = {r.id: r for r in load_rules()}


def _finding(rule_id: str, evidence: dict):
    rules = AUTH_RULES if rule_id in AUTH_RULES else ALL_RULES
    return AUTOMATED_CHECKS[rule_id](evidence, rules[rule_id])


# ─── state / CSRF ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "sent,returned,expected",
    [
        ("abc123", "abc123", True),
        ("abc123", "abc124", False),
        ("abc123", "", False),
        ("", "", False),          # a blank state is not a match, it is no state
        ("", "abc123", False),
    ],
)
def test_validate_state(sent, returned, expected):
    assert validate_state(sent, returned) is expected


# ─── live mode must not fake a token ─────────────────────────────────────────


def test_live_mode_marks_token_unavailable_instead_of_using_the_mock(monkeypatch):
    """The Day 13 bug: live runs reused MOCK_TOKEN_RESPONSE, so AUTH-003/005
    could never fail. Live evidence must say "unknown", not borrow the fixture."""
    import epic_compliance.pipeline as pipeline_mod
    from epic_compliance.smart.discovery import MOCK_SMART_CONFIG

    monkeypatch.setattr(pipeline_mod, "fetch_smart_configuration", lambda url, mock: MOCK_SMART_CONFIG)

    class _StubClient:
        def __init__(self, **kw):
            self.kw = kw

        def fetch_all_us_core(self, patient_id="mock-patient-id"):
            return {}

    monkeypatch.setattr(pipeline_mod, "FhirClient", _StubClient)

    ev = collect_evidence(AppConfig(RUN_MODE="live"))
    assert ev["token_response"] == {"_unavailable": TOKEN_UNAVAILABLE_REASON}
    assert ev["auth_probe"] == {"_unavailable": TOKEN_UNAVAILABLE_REASON}
    assert ev["write_back"] == {"_unavailable": TOKEN_UNAVAILABLE_REASON}
    assert pkce_mod.MOCK_TOKEN_RESPONSE.access_token not in str(ev)

    # and the rules built on it must not report a pass
    for rule_id in ("AUTH-003", "AUTH-005", "WRITE-001", "WRITE-002", "WRITE-003", "WRITE-004"):
        assert _finding(rule_id, ev).verdict == "needs_human"


def test_mock_mode_supplies_token_and_probe_evidence():
    ev = collect_evidence(AppConfig(RUN_MODE="mock"))
    assert "_unavailable" not in ev["token_response"]
    assert ev["token_response"]["access_token"] == "[REDACTED]"
    assert ev["auth_probe"] == MOCK_AUTH_PROBE
    assert set(MOCK_AUTH_PROBE) == set(AUTH_PROBE_FIELDS)


# ─── token-dependent rules degrade instead of passing ────────────────────────


UNAVAILABLE = {"_unavailable": TOKEN_UNAVAILABLE_REASON}


@pytest.mark.parametrize("rule_id", ["AUTH-003", "AUTH-005"])
def test_token_rules_are_needs_human_without_a_token(rule_id):
    f = _finding(rule_id, {"token_response": dict(UNAVAILABLE)})
    assert f.verdict == "needs_human", "a missing token must never read as pass"
    assert TOKEN_UNAVAILABLE_REASON in f.evidence


@pytest.mark.parametrize("rule_id", ["AUTH-006", "AUTH-007", "AUTH-008", "AUTH-009"])
def test_probe_rules_are_needs_human_without_a_probe(rule_id):
    f = _finding(rule_id, {"auth_probe": dict(UNAVAILABLE)})
    assert f.verdict == "needs_human"


@pytest.mark.parametrize("rule_id", ["AUTH-003", "AUTH-005", "AUTH-006", "AUTH-009"])
def test_rules_are_needs_human_on_completely_empty_evidence(rule_id):
    assert _finding(rule_id, {}).verdict == "needs_human"


# ─── AUTH-003: Epic grants per-resource scopes, not the wildcard ─────────────


@pytest.mark.parametrize(
    "scope,expected",
    [
        ("patient/*.read openid", "pass"),
        ("patient/*.* openid", "pass"),
        # Epic's real shape — must not be a false failure
        ("openid fhirUser patient/Patient.read patient/Observation.read", "pass"),
        ("openid fhirUser", "fail"),
        ("user/Patient.read", "fail"),
        ("", "fail"),
    ],
)
def test_auth_003_accepts_per_resource_patient_scopes(scope, expected):
    f = _finding("AUTH-003", {"token_response": {"scope": scope}})
    assert f.verdict == expected, f.evidence


# ─── AUTH-006..009: tri-state probe results ──────────────────────────────────


PROBE_RULES = {
    "AUTH-006": "state_validated",
    "AUTH-007": "wrong_verifier_rejected",
    "AUTH-008": "code_single_use",
    "AUTH-009": "cross_patient_refused",
}


@pytest.mark.parametrize("rule_id,field", PROBE_RULES.items())
def test_probe_rule_passes_when_server_enforced_the_control(rule_id, field):
    assert _finding(rule_id, {"auth_probe": {field: True}}).verdict == "pass"


@pytest.mark.parametrize("rule_id,field", PROBE_RULES.items())
def test_probe_rule_fails_when_server_did_not_enforce_the_control(rule_id, field):
    assert _finding(rule_id, {"auth_probe": {field: False}}).verdict == "fail"


@pytest.mark.parametrize("rule_id,field", PROBE_RULES.items())
def test_probe_rule_is_needs_human_when_probe_did_not_run(rule_id, field):
    """None means "not tested" — distinct from False, which means "broken"."""
    assert _finding(rule_id, {"auth_probe": {field: None}}).verdict == "needs_human"


# ─── AUTH-010: id_token verifiability ────────────────────────────────────────


def test_auth_010_fails_without_jwks_uri():
    ev = {"smart_configuration": {"raw": {}}, "auth_probe": {"id_token_verifiable": True}}
    f = _finding("AUTH-010", ev)
    assert f.verdict == "fail"
    assert "jwks_uri" in f.evidence


def test_auth_010_passes_with_jwks_uri_and_verified_signature():
    ev = {
        "smart_configuration": {"raw": {"jwks_uri": "https://fhir.epic.com/.../jwks"}},
        "auth_probe": {"id_token_verifiable": True},
    }
    assert _finding("AUTH-010", ev).verdict == "pass"


def test_auth_010_needs_human_when_signature_was_never_checked():
    ev = {
        "smart_configuration": {"raw": {"jwks_uri": "https://fhir.epic.com/.../jwks"}},
        "auth_probe": {},
    }
    assert _finding("AUTH-010", ev).verdict == "needs_human"


# ─── token exchange ──────────────────────────────────────────────────────────


def _stub_post(monkeypatch, *, json_body=None, status=200):
    captured: dict = {}

    def fake_post(url, data=None, timeout=None):
        captured["url"] = url
        captured["data"] = data
        request = httpx.Request("POST", url)
        return httpx.Response(status, json=json_body or {}, request=request)

    monkeypatch.setattr(httpx, "post", fake_post)
    return captured


def test_exchange_code_for_token_parses_epic_shaped_response(monkeypatch):
    captured = _stub_post(
        monkeypatch,
        json_body={
            "access_token": "at",
            "token_type": "Bearer",
            "expires_in": "3600",  # Epic may serialise this as a string
            "scope": "patient/Patient.read",
            "patient": "ePID",
            "id_token": "idt",
        },
    )
    tok = exchange_code_for_token(
        token_endpoint="https://example.org/token",
        code="the-code",
        client_id="cid",
        redirect_uri="http://localhost:8000/callback",
        code_verifier="ver",
    )
    assert tok.access_token == "at"
    assert tok.expires_in == 3600, "expires_in must be coerced to int"
    assert tok.patient == "ePID"
    assert tok.raw["scope"] == "patient/Patient.read"
    # the verifier must be sent, the secret omitted for a public client
    assert captured["data"]["code_verifier"] == "ver"
    assert "client_secret" not in captured["data"]


def test_exchange_code_for_token_sends_secret_for_confidential_client(monkeypatch):
    captured = _stub_post(monkeypatch, json_body={"access_token": "at"})
    exchange_code_for_token(
        token_endpoint="https://example.org/token",
        code="c",
        client_id="cid",
        redirect_uri="r",
        code_verifier="v",
        client_secret="s3cret",
    )
    assert captured["data"]["client_secret"] == "s3cret"
    assert captured["data"]["grant_type"] == "authorization_code"


def test_exchange_code_for_token_raises_on_error_response(monkeypatch):
    _stub_post(monkeypatch, json_body={"error": "invalid_grant"}, status=400)
    with pytest.raises(httpx.HTTPStatusError):
        exchange_code_for_token(
            token_endpoint="https://example.org/token",
            code="spent",
            client_id="cid",
            redirect_uri="r",
            code_verifier="v",
        )


def test_missing_fields_default_rather_than_crash(monkeypatch):
    _stub_post(monkeypatch, json_body={"access_token": "at"})
    tok = exchange_code_for_token(
        token_endpoint="https://example.org/token",
        code="c",
        client_id="cid",
        redirect_uri="r",
        code_verifier="v",
    )
    assert tok.token_type == "Bearer"
    assert tok.patient == "" and tok.refresh_token == ""


# ─── catalog integrity ───────────────────────────────────────────────────────


def test_every_automated_auth_rule_has_a_check_registered():
    """An unregistered rule silently becomes needs_human — catch it here instead."""
    missing = [
        r.id for r in AUTH_RULES.values()
        if r.check_type == "automated" and r.id not in AUTOMATED_CHECKS
    ]
    assert not missing, f"rules with no check function: {missing}"


def test_new_auth_rules_are_present():
    for rid in ["AUTH-006", "AUTH-007", "AUTH-008", "AUTH-009", "AUTH-010"]:
        assert rid in AUTH_RULES, f"{rid} missing from rules/auth.json"
        assert AUTH_RULES[rid].source, "every rule must cite its source"
