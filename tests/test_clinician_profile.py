"""LAUNCH_PROFILE=clinician_standalone (ADR-010).

A clinician signs in through Epic with no patient selected, searches for and
confirms the patient, files a photo and a consult note. The core guard is
CLIN-007: what Epic stored must reference the patient the clinician confirmed.
"""
from __future__ import annotations

import base64
import json

import pytest

from epic_compliance.config import AppConfig, config_with_overrides
from epic_compliance.fhir.client import FhirClient
from epic_compliance.pipeline import _ref_id, collect_clinician_evidence, collect_evidence
from epic_compliance.rules_engine.automated_checks import AUTOMATED_CHECKS
from epic_compliance.rules_engine.catalog import load_rules
from epic_compliance.rules_engine.engine import run_pipeline
from epic_compliance.smart.pkce import id_token_claims
from epic_compliance.validator.models import ValidationResult

RULES = {r.id: r for r in load_rules()}
CLIN_RULES = sorted(r for r in RULES if r.startswith("CLIN-"))
PATIENT_ONLY = ("AUTH-009", "WRITE-001", "WRITE-002", "WRITE-003", "WRITE-004")


class _AlwaysValidValidator:
    def validate(self, resource, profile=""):
        return ValidationResult(resource_type=resource["resourceType"], profile=profile, available=True, messages=[])


def _finding(rule_id, evidence):
    return AUTOMATED_CHECKS[rule_id](evidence, RULES[rule_id])


def _clinician_config(**kw):
    base = dict(RUN_MODE="mock", LAUNCH_PROFILE="clinician_standalone", VALIDATOR_MODE="off",
                TEST_PATIENT_QUERY="identifier=12345")
    base.update(kw)
    return AppConfig(**base)


def _flow(client, **kw):
    return collect_clinician_evidence(client, _AlwaysValidValidator(), _clinician_config(**kw),
                                      "Practitioner/mock-practitioner-id")


# ─── profile selects the rule set ───────────────────────────────────────────


def test_every_clin_rule_has_a_check_and_is_clinician_only():
    assert len(CLIN_RULES) == 12
    for rule_id in CLIN_RULES:
        assert rule_id in AUTOMATED_CHECKS
        assert RULES[rule_id].profiles == ["clinician_standalone"]


def test_clinician_run_skips_patient_only_rules():
    ev = collect_evidence(_clinician_config())
    ids = {f.rule_id for f in run_pipeline(ev, launch_profile="clinician_standalone").findings}

    assert set(CLIN_RULES) <= ids
    assert not ids & set(PATIENT_ONLY)


def test_patient_launch_run_is_unchanged():
    ev = collect_evidence(AppConfig(RUN_MODE="mock", VALIDATOR_MODE="off"))
    ids = {f.rule_id for f in run_pipeline(ev).findings}

    assert not any(i.startswith("CLIN-") for i in ids)
    assert set(PATIENT_ONLY) <= ids


def test_mock_clinician_flow_passes_end_to_end():
    ev = {"clinician": _flow(FhirClient("https://x/R4", "t", mock=True)),
          "app_registration": {"client_type": "public", "redirect_uri": "yourapp://callback"}}

    verdicts = {r: _finding(r, ev).verdict for r in CLIN_RULES}

    assert set(verdicts.values()) == {"pass"}, verdicts


# ─── auth rules read the profile ────────────────────────────────────────────


def test_auth_005_clinician_needs_practitioner_not_patient():
    ev = {"launch_profile": "clinician_standalone",
          "token_response": {"id_token": "[REDACTED]", "fhir_user": "Practitioner/abc", "patient": ""}}
    assert _finding("AUTH-005", ev).verdict == "pass"

    ev["token_response"]["fhir_user"] = "Patient/xyz"
    assert _finding("AUTH-005", ev).verdict == "fail"


def test_auth_003_clinician_requires_user_scopes():
    ev = {"launch_profile": "clinician_standalone", "token_response": {"scope": "openid patient/Patient.read"}}
    assert _finding("AUTH-003", ev).verdict == "fail"

    ev["token_response"]["scope"] = "openid user/Patient.read"
    assert _finding("AUTH-003", ev).verdict == "pass"


# ─── wrong-patient guard ────────────────────────────────────────────────────


class _MisfilingClient(FhirClient):
    """Stores every write against a different patient than it was sent for."""

    def post_resource(self, resource):
        resource = dict(resource, subject={"reference": "Patient/someone-else"})
        return super().post_resource(resource)


def test_clin_007_fails_when_epic_stored_the_photo_on_another_patient():
    ev = {"clinician": _flow(_MisfilingClient("https://x/R4", "t", mock=True))}

    finding = _finding("CLIN-007", ev)

    assert finding.verdict == "fail"
    assert "someone-else" in finding.evidence


def test_clin_007_fails_on_wrong_encounter():
    ev = {"clinician": {
        "selected_patient_id": "p1",
        "encounter": {"id": "e1"},
        "image_write": {"read_back": {"status_code": 200, "patient": "p1", "encounter": "e2"}},
        "note_write": {"read_back": {"status_code": 200, "patient": "p1", "encounter": "e1"}},
    }}
    assert _finding("CLIN-007", ev).verdict == "fail"


def test_ambiguous_patient_search_is_not_a_pass():
    ev = {"clinician": {"patient_search": {"query": "family=Smith", "status_code": 200, "match_count": 3}}}
    assert _finding("CLIN-001", ev).verdict == "needs_human"


# ─── honest verdicts on evidence never collected ────────────────────────────


def test_no_patient_query_leaves_downstream_rules_needs_human():
    # A live client with no query must stop before any patient request; no
    # fhirUser keeps the Practitioner read offline too.
    client = FhirClient("https://x/R4", "t", mock=False)
    ev = {"clinician": collect_clinician_evidence(
        client, _AlwaysValidValidator(), _clinician_config(RUN_MODE="live", TEST_PATIENT_QUERY=""), "")}

    for rule_id in ("CLIN-001", "CLIN-002", "CLIN-004", "CLIN-005", "CLIN-006",
                    "CLIN-007", "CLIN-008", "CLIN-009", "CLIN-010"):
        assert _finding(rule_id, ev).verdict == "needs_human", rule_id


def test_live_clinician_without_token_is_needs_human(monkeypatch):
    from epic_compliance import pipeline as pipeline_mod
    from epic_compliance.smart.discovery import MOCK_SMART_CONFIG

    monkeypatch.setattr(pipeline_mod, "fetch_smart_configuration", lambda url, mock: MOCK_SMART_CONFIG)
    pipeline_mod.clear_live_token()

    ev = collect_evidence(_clinician_config(RUN_MODE="live"))

    for rule_id in CLIN_RULES[:10]:
        assert _finding(rule_id, ev).verdict == "needs_human", rule_id


def test_encounter_required_with_no_visit_fails():
    ev = {"clinician": {"encounter": {"status_code": 200, "count": 0, "id": "", "required": True}}}
    assert _finding("CLIN-004", ev).verdict == "fail"

    ev["clinician"]["encounter"]["required"] = False
    assert _finding("CLIN-004", ev).verdict == "pass"


# ─── attested registration facts ────────────────────────────────────────────


@pytest.mark.parametrize("client_type,secret,verdict", [
    ("public", False, "pass"),
    ("confidential", False, "fail"),
    ("", False, "needs_human"),
    ("public", True, "needs_human"),
])
def test_clin_011_public_client(client_type, secret, verdict):
    ev = {"app_registration": {"client_type": client_type, "tool_sends_client_secret": secret}}
    assert _finding("CLIN-011", ev).verdict == verdict


@pytest.mark.parametrize("uri,verdict", [
    ("com.example.derm:/callback", "pass"),
    ("https://app.example.com/callback", "pass"),
    ("http://app.example.com/callback", "fail"),
    ("", "needs_human"),
])
def test_clin_012_redirect_uri(uri, verdict):
    assert _finding("CLIN-012", {"app_registration": {"redirect_uri": uri}}).verdict == verdict


# ─── plumbing ───────────────────────────────────────────────────────────────


def test_created_id_from_epic_location_header():
    loc = "https://fhir.epic.com/api/FHIR/R4/Media/eAbc123/_history/1"
    assert _ref_id(loc, "Media") == "eAbc123"
    assert _ref_id("Practitioner/x1", "Practitioner") == "x1"
    assert _ref_id("Patient/x1", "Practitioner") == ""


def test_fhir_user_read_from_id_token():
    payload = base64.urlsafe_b64encode(json.dumps({"fhirUser": "Practitioner/p9"}).encode()).rstrip(b"=")
    assert id_token_claims(f"h.{payload.decode()}.s")["fhirUser"] == "Practitioner/p9"
    assert id_token_claims("not-a-jwt") == {}


def test_form_override_encounter_required_false_is_false():
    assert config_with_overrides({"encounter_required": "false"}).encounter_required is False
