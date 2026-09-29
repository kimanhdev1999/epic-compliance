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


def _unavailable(evidence: dict[str, Any], key: str, rule: Rule) -> Finding | None:
    """Return a needs_human Finding if `key` evidence was never collected.

    A missing token or un-run probe must never be read as a pass. Every check
    that depends on such evidence calls this first.
    """
    blob = evidence.get(key)
    if isinstance(blob, dict) and "_unavailable" in blob:
        return Finding(
            rule_id=rule.id,
            verdict="needs_human",
            evidence=f"{key} unavailable: {blob['_unavailable']}",
            remediation_hint=rule.remediation_hint,
            severity=rule.severity,
            category=rule.category,
            citation="Evidence not collected — rule not evaluated.",
        )
    if not blob:
        return Finding(
            rule_id=rule.id,
            verdict="needs_human",
            evidence=f"{key} missing from collected evidence.",
            remediation_hint=rule.remediation_hint,
            severity=rule.severity,
            category=rule.category,
            citation="Evidence not collected — rule not evaluated.",
        )
    return None


def _probe_check(evidence: dict[str, Any], rule: Rule, field: str, label: str) -> Finding:
    """Judge one tri-state interactive probe result: True / False / not run."""
    unavailable = _unavailable(evidence, "auth_probe", rule)
    if unavailable is not None:
        return unavailable

    result = evidence.get("auth_probe", {}).get(field)
    if result is None:
        return Finding(
            rule_id=rule.id,
            verdict="needs_human",
            evidence=f"{label}: probe '{field}' was not run against this server.",
            remediation_hint=rule.remediation_hint,
            severity=rule.severity,
            category=rule.category,
            citation="Requires an interactive SMART launch — run pytest -m launch.",
        )
    return Finding(
        rule_id=rule.id,
        verdict="pass" if result else "fail",
        evidence=f"{label}: {field}={result}",
        remediation_hint=rule.remediation_hint,
        severity=rule.severity,
        category=rule.category,
    )


def _check_auth_003(evidence: dict[str, Any], rule: Rule) -> Finding:
    """Scopes granted include patient read access."""
    unavailable = _unavailable(evidence, "token_response", rule)
    if unavailable is not None:
        return unavailable
    token = evidence.get("token_response", {})
    scope_str = token.get("scope", "")
    scopes = scope_str.split()
    # Epic grants per-resource scopes (patient/Patient.read) rather than the
    # wildcard, so accepting only patient/*.read would fail a conformant server.
    wildcard = [s for s in scopes if s in ("patient/*.read", "patient/*.*")]
    per_resource = [
        s for s in scopes
        if s.startswith("patient/") and s.split(".")[-1] in ("read", "*")
    ]
    ok = bool(wildcard or per_resource)
    return Finding(
        rule_id=rule.id,
        verdict="pass" if ok else "fail",
        evidence=f"granted scopes={scope_str!r}; patient read scopes={wildcard or per_resource}",
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
    unavailable = _unavailable(evidence, "token_response", rule)
    if unavailable is not None:
        return unavailable
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


def _check_auth_006(evidence: dict[str, Any], rule: Rule) -> Finding:
    """OAuth state is round-tripped and validated (CSRF)."""
    return _probe_check(evidence, rule, "state_validated", "state round-trip validated")


def _check_auth_007(evidence: dict[str, Any], rule: Rule) -> Finding:
    """PKCE is enforced: a wrong code_verifier is rejected at the token endpoint."""
    return _probe_check(evidence, rule, "wrong_verifier_rejected", "wrong verifier rejected")


def _check_auth_008(evidence: dict[str, Any], rule: Rule) -> Finding:
    """Authorization code is single-use: a replay is rejected."""
    return _probe_check(evidence, rule, "code_single_use", "replayed code rejected")


def _check_auth_009(evidence: dict[str, Any], rule: Rule) -> Finding:
    """Token is scoped to its patient: cross-patient reads are refused."""
    return _probe_check(evidence, rule, "cross_patient_refused", "cross-patient read refused")


def _check_auth_010(evidence: dict[str, Any], rule: Rule) -> Finding:
    """id_token signature is verifiable against the advertised jwks_uri."""
    sc = evidence.get("smart_configuration", {})
    jwks_uri = sc.get("raw", {}).get("jwks_uri", "")
    if not jwks_uri:
        return Finding(
            rule_id=rule.id,
            verdict="fail",
            evidence="No jwks_uri in .well-known/smart-configuration — id_token signature cannot be verified.",
            remediation_hint=rule.remediation_hint,
            severity=rule.severity,
            category=rule.category,
        )
    return _probe_check(evidence, rule, "id_token_verifiable", f"jwks_uri={jwks_uri}")


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


def _write_entry(evidence: dict[str, Any], resource_type: str) -> dict[str, Any]:
    return evidence.get("write_back", {}).get(resource_type, {})


def _check_write_resource(evidence: dict[str, Any], rule: Rule, resource_type: str) -> Finding:
    """Shared logic for WRITE-001/002/003: construct + validate + POST one
    write-back resource type. Tri-state: pass only if the payload validated
    clean against US Core AND the POST returned 2xx; needs_human if either the
    validator or the POST never ran; fail otherwise."""
    unavailable = _unavailable(evidence, "write_back", rule)
    if unavailable is not None:
        return unavailable

    entry = _write_entry(evidence, resource_type)
    if not entry:
        return Finding(
            rule_id=rule.id,
            verdict="needs_human",
            evidence=f"No write-back evidence collected for {resource_type}.",
            remediation_hint=rule.remediation_hint,
            severity=rule.severity,
            category=rule.category,
            citation="Evidence not collected — rule not evaluated.",
        )

    valid = entry.get("validation_valid")
    posted = entry.get("posted")
    status = entry.get("status_code")
    summary = entry.get("validation_summary", "")

    if valid is None or posted is None:
        return Finding(
            rule_id=rule.id,
            verdict="needs_human",
            evidence=(
                f"{resource_type}: validation_valid={valid} ({summary}); "
                f"posted={posted} (status={status}, error={entry.get('error', '')})"
            ),
            remediation_hint=rule.remediation_hint,
            severity=rule.severity,
            category=rule.category,
            citation="Validator or POST result unavailable — cannot judge conformance.",
        )

    ok = bool(valid) and bool(posted)
    return Finding(
        rule_id=rule.id,
        verdict="pass" if ok else "fail",
        evidence=f"{resource_type}: validation={summary}; POST status={status}",
        remediation_hint=rule.remediation_hint,
        severity=rule.severity,
        category=rule.category,
    )


def _check_write_001(evidence: dict[str, Any], rule: Rule) -> Finding:
    """DiagnosticReport write-back: valid US Core payload, POST succeeds."""
    return _check_write_resource(evidence, rule, "DiagnosticReport")


def _check_write_002(evidence: dict[str, Any], rule: Rule) -> Finding:
    """Observation write-back: valid US Core payload, POST succeeds."""
    return _check_write_resource(evidence, rule, "Observation")


def _check_write_003(evidence: dict[str, Any], rule: Rule) -> Finding:
    """Media write-back: valid payload, POST succeeds."""
    return _check_write_resource(evidence, rule, "Media")


def _check_write_004(evidence: dict[str, Any], rule: Rule) -> Finding:
    """Write scopes actually granted — proven by the write attempts succeeding,
    not by inspecting the requested/granted scope string. A 403/401 on any of
    the three POSTs means that write scope was not actually honored."""
    unavailable = _unavailable(evidence, "write_back", rule)
    if unavailable is not None:
        return unavailable

    resource_types = ("Observation", "DiagnosticReport", "Media")
    posted = {rt: _write_entry(evidence, rt).get("posted") for rt in resource_types}
    status = {rt: _write_entry(evidence, rt).get("status_code") for rt in resource_types}

    if any(v is None for v in posted.values()):
        return Finding(
            rule_id=rule.id,
            verdict="needs_human",
            evidence=f"One or more write attempts did not complete: status_codes={status}",
            remediation_hint=rule.remediation_hint,
            severity=rule.severity,
            category=rule.category,
            citation="A write attempt never completed — cannot judge whether the scope was granted.",
        )

    denied = [rt for rt, ok in posted.items() if not ok and status[rt] in (401, 403)]
    ok = all(posted.values())
    return Finding(
        rule_id=rule.id,
        verdict="pass" if ok else "fail",
        evidence=f"write_scopes_effective={ok}; status_codes={status}; denied={denied}",
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
    "AUTH-006": _check_auth_006,
    "AUTH-007": _check_auth_007,
    "AUTH-008": _check_auth_008,
    "AUTH-009": _check_auth_009,
    "AUTH-010": _check_auth_010,
    "FHIR-001": _check_fhir_001,
    "FHIR-002": _check_fhir_002,
    "FHIR-003": _check_fhir_003,
    "FHIR-004": _check_fhir_004,
    "FHIR-005": _check_fhir_005,
    "WRITE-001": _check_write_001,
    "WRITE-002": _check_write_002,
    "WRITE-003": _check_write_003,
    "WRITE-004": _check_write_004,
    # SEAM: HL7 FHIR Validator wrapping will add rule IDs here (e.g. FHIR-V-*)
    # SEAM: ONC g10 Test Kit invocation will add rule IDs here (e.g. ONC-G10-*)
}
