"""Hand-written automated rule assertions.

Each check function receives the collected evidence dict and returns a tuple
(verdict, evidence_str, remediation_hint).

SEAM: HL7 FHIR Validator and ONC g10 Test Kit integration will plug in here.
Currently these are hand-written assertions against the collected data.
"""
from __future__ import annotations

from typing import Any

from ..models import Finding
from .catalog import Rule


def _check_auth_001(evidence: dict[str, Any], rule: Rule) -> Finding:
    """SMART configuration discoverable."""
    sc = evidence.get("smart_configuration", {})
    caps = sc.get("capabilities", [])
    has_config = bool(caps)
    return Finding(
        rule_id=rule.id,
        verdict="pass" if has_config else "fail",
        evidence=f"capabilities present: {has_config}; count={len(caps)}",
        remediation_hint=rule.remediation_hint,
        severity=rule.severity,
        category=rule.category,
    )


def _check_auth_002(evidence: dict[str, Any], rule: Rule) -> Finding:
    """PKCE S256 supported."""
    sc = evidence.get("smart_configuration", {})
    methods = sc.get("code_challenge_methods_supported", [])
    ok = "S256" in methods
    return Finding(
        rule_id=rule.id,
        verdict="pass" if ok else "fail",
        evidence=f"code_challenge_methods_supported={methods}",
        remediation_hint=rule.remediation_hint,
        severity=rule.severity,
        category=rule.category,
    )


def _check_auth_003(evidence: dict[str, Any], rule: Rule) -> Finding:
    """Scopes granted include patient/*.read."""
    token = evidence.get("token_response", {})
    scope_str = token.get("scope", "")
    scopes = scope_str.split()
    ok = any(s in scopes for s in ["patient/*.read", "patient/*.*"])
    return Finding(
        rule_id=rule.id,
        verdict="pass" if ok else "fail",
        evidence=f"granted scopes={scope_str!r}",
        remediation_hint=rule.remediation_hint,
        severity=rule.severity,
        category=rule.category,
    )


def _check_auth_004(evidence: dict[str, Any], rule: Rule) -> Finding:
    """launch-ehr and launch-standalone capabilities advertised."""
    sc = evidence.get("smart_configuration", {})
    caps = sc.get("capabilities", [])
    missing = [c for c in ["launch-ehr", "launch-standalone"] if c not in caps]
    ok = not missing
    return Finding(
        rule_id=rule.id,
        verdict="pass" if ok else "fail",
        evidence=f"capabilities={caps}; missing={missing}",
        remediation_hint=rule.remediation_hint,
        severity=rule.severity,
        category=rule.category,
    )


def _check_auth_005(evidence: dict[str, Any], rule: Rule) -> Finding:
    """Token response includes patient context and id_token."""
    token = evidence.get("token_response", {})
    has_patient = bool(token.get("patient"))
    has_id_token = bool(token.get("id_token"))
    ok = has_patient and has_id_token
    return Finding(
        rule_id=rule.id,
        verdict="pass" if ok else "fail",
        evidence=f"patient_field_present={has_patient}, id_token_present={has_id_token}",
        remediation_hint=rule.remediation_hint,
        severity=rule.severity,
        category=rule.category,
    )


def _check_fhir_001(evidence: dict[str, Any], rule: Rule) -> Finding:
    """Patient: name, gender, birthDate present."""
    pt = evidence.get("fhir_resources", {}).get("Patient", {})
    has_name = bool(pt.get("name"))
    has_gender = bool(pt.get("gender"))
    has_dob = bool(pt.get("birthDate"))
    ok = has_name and has_gender and has_dob
    return Finding(
        rule_id=rule.id,
        verdict="pass" if ok else "fail",
        evidence=f"name={has_name}, gender={has_gender}, birthDate={has_dob}",
        remediation_hint=rule.remediation_hint,
        severity=rule.severity,
        category=rule.category,
    )


def _check_fhir_002(evidence: dict[str, Any], rule: Rule) -> Finding:
    """Observation: status, category, code, subject present."""
    obs = evidence.get("fhir_resources", {}).get("Observation", {})
    checks = {
        "status": bool(obs.get("status")),
        "category": bool(obs.get("category")),
        "code": bool(obs.get("code")),
        "subject": bool(obs.get("subject")),
    }
    ok = all(checks.values())
    return Finding(
        rule_id=rule.id,
        verdict="pass" if ok else "fail",
        evidence=f"field_presence={checks}",
        remediation_hint=rule.remediation_hint,
        severity=rule.severity,
        category=rule.category,
    )


def _check_fhir_003(evidence: dict[str, Any], rule: Rule) -> Finding:
    """Condition: required elements present."""
    cond = evidence.get("fhir_resources", {}).get("Condition", {})
    checks = {
        "clinicalStatus": bool(cond.get("clinicalStatus")),
        "verificationStatus": bool(cond.get("verificationStatus")),
        "category": bool(cond.get("category")),
        "code": bool(cond.get("code")),
        "subject": bool(cond.get("subject")),
    }
    ok = all(checks.values())
    return Finding(
        rule_id=rule.id,
        verdict="pass" if ok else "fail",
        evidence=f"field_presence={checks}",
        remediation_hint=rule.remediation_hint,
        severity=rule.severity,
        category=rule.category,
    )


def _check_fhir_004(evidence: dict[str, Any], rule: Rule) -> Finding:
    """Resources declare meta.profile."""
    resources = evidence.get("fhir_resources", {})
    missing_profile: list[str] = []
    for rt, res in resources.items():
        if not isinstance(res, dict):
            continue
        if "_error" in res or "_empty" in res:
            continue
        meta = res.get("meta", {})
        if not meta.get("profile"):
            missing_profile.append(rt)
    ok = not missing_profile
    return Finding(
        rule_id=rule.id,
        verdict="pass" if ok else "fail",
        evidence=f"resources_missing_profile={missing_profile}",
        remediation_hint=rule.remediation_hint,
        severity=rule.severity,
        category=rule.category,
    )


def _check_fhir_005(evidence: dict[str, Any], rule: Rule) -> Finding:
    """Required USCDI resources accessible."""
    resources = evidence.get("fhir_resources", {})
    required = ["Patient", "Observation", "Condition", "MedicationRequest", "AllergyIntolerance", "Immunization"]
    missing: list[str] = []
    for rt in required:
        res = resources.get(rt, {})
        if "_error" in res or "_empty" in res or not res:
            missing.append(rt)
    ok = not missing
    return Finding(
        rule_id=rule.id,
        verdict="pass" if ok else "fail",
        evidence=f"missing_uscdi_resources={missing}",
        remediation_hint=rule.remediation_hint,
        severity=rule.severity,
        category=rule.category,
    )


# Registry: map rule_id -> check function
AUTOMATED_CHECKS = {
    "AUTH-001": _check_auth_001,
    "AUTH-002": _check_auth_002,
    "AUTH-003": _check_auth_003,
    "AUTH-004": _check_auth_004,
    "AUTH-005": _check_auth_005,
    "FHIR-001": _check_fhir_001,
    "FHIR-002": _check_fhir_002,
    "FHIR-003": _check_fhir_003,
    "FHIR-004": _check_fhir_004,
    "FHIR-005": _check_fhir_005,
    # SEAM: HL7 FHIR Validator wrapping will add rule IDs here (e.g. FHIR-V-*)
    # SEAM: ONC g10 Test Kit invocation will add rule IDs here (e.g. ONC-G10-*)
}
