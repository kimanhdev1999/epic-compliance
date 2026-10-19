"""Hand-written automated rule assertions.

Each check function receives the collected evidence dict and returns a tuple
(verdict, evidence_str, remediation_hint).

SEAM: HL7 FHIR Validator and ONC g10 Test Kit integration will plug in here.
Currently these are hand-written assertions against the collected data.
"""
from __future__ import annotations

from typing import Any

from ..models import Finding
from ..smart.discovery import REQUIRED_CAPABILITIES
from .catalog import Rule


def _check_auth_001(evidence: dict[str, Any], rule: Rule) -> Finding:
    """SMART configuration discoverable and advertising the required capabilities."""
    sc = evidence.get("smart_configuration", {})
    if "_error" in sc:
        return Finding(
            rule_id=rule.id,
            verdict="fail",
            evidence=f"discovery request failed: {sc['_error']}",
            remediation_hint=rule.remediation_hint,
            severity=rule.severity,
            category=rule.category,
        )
    caps = sc.get("capabilities", [])
    missing = [c for c in REQUIRED_CAPABILITIES if c not in caps]
    ok = bool(caps) and not missing
    return Finding(
        rule_id=rule.id,
        verdict="pass" if ok else "fail",
        evidence=f"capabilities advertised={len(caps)}; missing_required={missing}",
        remediation_hint=rule.remediation_hint,
        severity=rule.severity,
        category=rule.category,
    )


def _discovery_failed(evidence: dict[str, Any], rule: Rule) -> Finding | None:
    """needs_human for discovery-dependent rules when discovery itself failed.

    The failure is reported once, as AUTH-001's fail; every other rule that
    reads the SMART configuration has nothing to judge.
    """
    sc = evidence.get("smart_configuration", {})
    if "_error" not in sc:
        return None
    return Finding(
        rule_id=rule.id,
        verdict="needs_human",
        evidence=f"smart_configuration unavailable: {sc['_error']}",
        remediation_hint=rule.remediation_hint,
        severity=rule.severity,
        category=rule.category,
        citation="Discovery failed — see AUTH-001. Rule not evaluated.",
    )


def _check_auth_002(evidence: dict[str, Any], rule: Rule) -> Finding:
    """PKCE S256 supported."""
    failed = _discovery_failed(evidence, rule)
    if failed is not None:
        return failed
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


def _profile(evidence: dict[str, Any]) -> str:
    return evidence.get("launch_profile") or "patient_launch"


def _check_auth_003(evidence: dict[str, Any], rule: Rule) -> Finding:
    """Scopes granted include patient read access."""
    unavailable = _unavailable(evidence, "token_response", rule)
    if unavailable is not None:
        return unavailable
    token = evidence.get("token_response", {})
    scope_str = token.get("scope", "")
    scopes = scope_str.split()
    # A clinician standalone login has no patient selected, so it reads with
    # user/* scopes; a patient launch reads with patient/* scopes.
    prefix = "user/" if _profile(evidence) == "clinician_standalone" else "patient/"
    # Epic grants per-resource scopes (patient/Patient.read) rather than the
    # wildcard, so accepting only patient/*.read would fail a conformant server.
    wildcard = [s for s in scopes if s in (f"{prefix}*.read", f"{prefix}*.*")]
    per_resource = [
        s for s in scopes
        if s.startswith(prefix) and s.split(".")[-1] in ("read", "*")
    ]
    ok = bool(wildcard or per_resource)
    return Finding(
        rule_id=rule.id,
        verdict="pass" if ok else "fail",
        evidence=f"granted scopes={scope_str!r}; {prefix}* read scopes={wildcard or per_resource}",
        remediation_hint=rule.remediation_hint,
        severity=rule.severity,
        category=rule.category,
    )


def _check_auth_004(evidence: dict[str, Any], rule: Rule) -> Finding:
    """launch-ehr and launch-standalone capabilities advertised."""
    failed = _discovery_failed(evidence, rule)
    if failed is not None:
        return failed
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
    has_id_token = bool(token.get("id_token"))
    if _profile(evidence) == "clinician_standalone":
        # No patient is chosen at login; who signed in is what matters.
        fhir_user = token.get("fhir_user", "")
        is_practitioner = "Practitioner/" in fhir_user
        ok = has_id_token and is_practitioner
        detail = f"fhir_user={fhir_user!r}, practitioner={is_practitioner}, id_token_present={has_id_token}"
    else:
        has_patient = bool(token.get("patient"))
        ok = has_patient and has_id_token
        detail = f"patient_field_present={has_patient}, id_token_present={has_id_token}"
    return Finding(
        rule_id=rule.id,
        verdict="pass" if ok else "fail",
        evidence=detail,
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
    failed = _discovery_failed(evidence, rule)
    if failed is not None:
        return failed
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


def _read_resource(
    evidence: dict[str, Any], rule: Rule, resource_type: str
) -> tuple[dict[str, Any], Finding | None]:
    """Return (resource, None), or ({}, needs_human) when it was never read.

    A resource the tool could not read (no token, HTTP error, empty search)
    says nothing about its shape. Access itself is judged by FHIR-005.
    """
    unavailable = _unavailable(evidence, "fhir_resources", rule)
    if unavailable is not None:
        return {}, unavailable
    res = evidence["fhir_resources"].get(resource_type) or {}
    if not res or "_error" in res or "_empty" in res:
        reason = res.get("_error") or ("search returned no entries" if "_empty" in res else "not fetched")
        return {}, Finding(
            rule_id=rule.id,
            verdict="needs_human",
            evidence=f"{resource_type} not readable: {reason}",
            remediation_hint=rule.remediation_hint,
            severity=rule.severity,
            category=rule.category,
            citation=f"No {resource_type} to inspect — rule not evaluated (access is judged by FHIR-005).",
        )
    return res, None


def _check_fhir_001(evidence: dict[str, Any], rule: Rule) -> Finding:
    """Patient: name, gender, birthDate present."""
    pt, not_read = _read_resource(evidence, rule, "Patient")
    if not_read is not None:
        return not_read
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
    obs, not_read = _read_resource(evidence, rule, "Observation")
    if not_read is not None:
        return not_read
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
    cond, not_read = _read_resource(evidence, rule, "Condition")
    if not_read is not None:
        return not_read
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
    unavailable = _unavailable(evidence, "fhir_resources", rule)
    if unavailable is not None:
        return unavailable
    resources = evidence.get("fhir_resources", {})
    readable = {
        rt: res for rt, res in resources.items()
        if isinstance(res, dict) and "_error" not in res and "_empty" not in res
    }
    if not readable:
        # Zero inspected resources must not read as "none were missing a profile".
        return Finding(
            rule_id=rule.id,
            verdict="needs_human",
            evidence="No readable resources to inspect for meta.profile.",
            remediation_hint=rule.remediation_hint,
            severity=rule.severity,
            category=rule.category,
            citation="Nothing to inspect — rule not evaluated.",
        )
    missing_profile = [rt for rt, res in readable.items() if not res.get("meta", {}).get("profile")]
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
    unavailable = _unavailable(evidence, "fhir_resources", rule)
    if unavailable is not None:
        return unavailable
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


# --------------------------------------------------------------------------- #
# clinician_standalone (ADR-010) — rules/clinician_workflow.json
# --------------------------------------------------------------------------- #
def _finding(rule: Rule, verdict: str, evidence: str, citation: str = "") -> Finding:
    return Finding(
        rule_id=rule.id,
        verdict=verdict,
        evidence=evidence,
        remediation_hint=rule.remediation_hint,
        severity=rule.severity,
        category=rule.category,
        citation=citation,
    )


def _clinician_step(evidence: dict[str, Any], rule: Rule, step: str) -> tuple[dict[str, Any], Finding | None]:
    """Return (step evidence, None), or ({}, needs_human) when the step never
    ran — no token, no TEST_PATIENT_QUERY, or an earlier step failed."""
    unavailable = _unavailable(evidence, "clinician", rule)
    if unavailable is not None:
        return {}, unavailable
    blob = evidence["clinician"].get(step)
    if not isinstance(blob, dict) or not blob or "_unavailable" in blob:
        reason = (blob or {}).get("_unavailable") if isinstance(blob, dict) else None
        search = evidence["clinician"].get("patient_search")
        if not reason and isinstance(search, dict) and "_unavailable" in search:
            # Nothing went wrong in Epic: there was no patient to work with.
            reason = ("Skipped because no test patient was searched for (see CLIN-001). Add a Test "
                      "patient search on the test plan page, then run again.")
        return {}, _finding(
            rule, "needs_human",
            reason or "Skipped because an earlier step didn't find or confirm the patient (see CLIN-001).",
            "Evidence not collected — rule not evaluated.",
        )
    return blob, None


def _ok_status(status: Any) -> bool:
    return isinstance(status, int) and 200 <= status < 300


def _check_clin_001(evidence: dict[str, Any], rule: Rule) -> Finding:
    """Patient.Search finds exactly one patient for the test query."""
    search, missing = _clinician_step(evidence, rule, "patient_search")
    if missing is not None:
        return missing
    status, count = search.get("status_code"), search.get("match_count", 0)
    detail = f"query={search.get('query')!r}; status={status}; matches={count}"
    if status is None:
        return _finding(rule, "needs_human", f"{detail}; error={search.get('error')}",
                        "Search request never completed.")
    if not _ok_status(status) or count == 0:
        return _finding(rule, "fail", f"{detail}; error={search.get('error', '')}")
    if count > 1:
        return _finding(rule, "needs_human", detail,
                        "Query matched several patients — use an identifier (MRN) query to show the lookup is unambiguous.")
    return _finding(rule, "pass", detail)


def _check_clin_002(evidence: dict[str, Any], rule: Rule) -> Finding:
    """Patient.Read returns what the confirmation screen needs."""
    pt, missing = _clinician_step(evidence, rule, "patient")
    if missing is not None:
        return missing
    fields = {k: pt.get(k, False) for k in ("has_name", "has_birthDate", "has_identifier")}
    ok = _ok_status(pt.get("status_code")) and all(fields.values())
    return _finding(rule, "pass" if ok else "fail",
                    f"Patient/{pt.get('id')} status={pt.get('status_code')}; {fields}; error={pt.get('error', '')}")


def _check_clin_003(evidence: dict[str, Any], rule: Rule) -> Finding:
    """fhirUser resolves to a named Practitioner."""
    unavailable = _unavailable(evidence, "clinician", rule)
    if unavailable is not None:
        return unavailable
    pr = evidence["clinician"].get("practitioner") or {}
    if "_unavailable" in pr:
        # The token not naming a Practitioner is itself the finding.
        return _finding(rule, "fail", f"fhir_user={pr.get('fhir_user')!r}: {pr['_unavailable']}")
    ok = _ok_status(pr.get("status_code")) and pr.get("has_name")
    return _finding(rule, "pass" if ok else "fail",
                    f"fhir_user={pr.get('fhir_user')!r}; Practitioner.Read status={pr.get('status_code')}; "
                    f"has_name={pr.get('has_name')}; error={pr.get('error', '')}")


def _check_clin_004(evidence: dict[str, Any], rule: Rule) -> Finding:
    """Encounter.Search works; returns a visit when one is required."""
    enc, missing = _clinician_step(evidence, rule, "encounter")
    if missing is not None:
        return missing
    detail = (f"status={enc.get('status_code')}; encounters={enc.get('count')}; "
              f"required={enc.get('required')}; error={enc.get('error', '')}")
    ok = _ok_status(enc.get("status_code")) and (enc.get("count", 0) > 0 or not enc.get("required"))
    return _finding(rule, "pass" if ok else "fail", detail)


def _judge_write(rule: Rule, entry: dict[str, Any]) -> Finding:
    """Tri-state write verdict, as WRITE-00x: validation and POST must both run."""
    valid, posted = entry.get("validation_valid"), entry.get("posted")
    detail = (f"{entry.get('resource_type')}: validation={entry.get('validation_summary', '')}; "
              f"POST status={entry.get('status_code')}; id={entry.get('id')!r}; error={entry.get('error', '')}")
    if valid is None or posted is None:
        return _finding(rule, "needs_human", detail,
                        "Validator or POST result unavailable — cannot judge conformance.")
    return _finding(rule, "pass" if valid and posted else "fail", detail)


def _check_clin_005(evidence: dict[str, Any], rule: Rule) -> Finding:
    """Clinical photo write accepted."""
    entry, missing = _clinician_step(evidence, rule, "image_write")
    if missing is not None:
        return missing
    return _judge_write(rule, entry)


def _check_clin_006(evidence: dict[str, Any], rule: Rule) -> Finding:
    """Photo reads back by id and its bytes are retrievable."""
    entry, missing = _clinician_step(evidence, rule, "image_write")
    if missing is not None:
        return missing
    rb = entry.get("read_back") or {}
    if "_unavailable" in rb:
        return _finding(rule, "needs_human", f"photo read-back not attempted: {rb['_unavailable']}",
                        "The write did not succeed — judged by CLIN-005.")
    attachment = entry.get("attachment") or {}
    ok = _ok_status(rb.get("status_code")) and attachment.get("ok") is True
    return _finding(rule, "pass" if ok else "fail",
                    f"{entry.get('resource_type')}.Read status={rb.get('status_code')}; "
                    f"image bytes: {attachment.get('how', 'not checked')}; error={rb.get('error', '')}")


def _check_clin_007(evidence: dict[str, Any], rule: Rule) -> Finding:
    """Every stored write references the confirmed patient and visit."""
    unavailable = _unavailable(evidence, "clinician", rule)
    if unavailable is not None:
        return unavailable
    ev = evidence["clinician"]
    patient_id = ev.get("selected_patient_id", "")
    encounter_id = (ev.get("encounter") or {}).get("id", "")
    stored = {
        name: (ev.get(name) or {}).get("read_back") or {}
        for name in ("image_write", "note_write")
    }
    checked = {name: rb for name, rb in stored.items() if rb and "_unavailable" not in rb
               and _ok_status(rb.get("status_code"))}
    if not patient_id or not checked:
        return _finding(rule, "needs_human",
                        f"confirmed patient={patient_id!r}; nothing written was read back",
                        "No stored write to inspect — rule not evaluated (writes judged by CLIN-005/009).")
    problems = []
    for name, rb in checked.items():
        if rb.get("patient") != patient_id:
            problems.append(f"{name} stored for Patient/{rb.get('patient')!r}, confirmed Patient/{patient_id}")
        if encounter_id and rb.get("encounter") != encounter_id:
            problems.append(f"{name} stored on Encounter/{rb.get('encounter')!r}, selected Encounter/{encounter_id}")
    detail = (f"confirmed Patient/{patient_id}; selected Encounter/{encounter_id or '(none)'}; "
              f"inspected={sorted(checked)}")
    if len(checked) < len(stored):
        detail += f"; not read back={sorted(set(stored) - set(checked))}"
    return _finding(rule, "fail" if problems else "pass",
                    detail + (f"; mismatches={problems}" if problems else ""))


def _check_clin_008(evidence: dict[str, Any], rule: Rule) -> Finding:
    """Derm review searches are allowed."""
    reads, missing = _clinician_step(evidence, rule, "review_reads")
    if missing is not None:
        return missing
    statuses = {rt: r.get("status_code") for rt, r in reads.items()}
    if any(v is None for v in statuses.values()):
        return _finding(rule, "needs_human", f"status_codes={statuses}",
                        "A search never completed — cannot judge access.")
    denied = [rt for rt, v in statuses.items() if not _ok_status(v)]
    return _finding(rule, "fail" if denied else "pass",
                    f"status_codes={statuses}; counts={ {rt: r.get('count') for rt, r in reads.items()} }; denied={denied}")


def _check_clin_009(evidence: dict[str, Any], rule: Rule) -> Finding:
    """Consult note written and read back."""
    entry, missing = _clinician_step(evidence, rule, "note_write")
    if missing is not None:
        return missing
    finding = _judge_write(rule, entry)
    rb = entry.get("read_back") or {}
    if finding.verdict == "pass" and not _ok_status(rb.get("status_code")):
        return _finding(rule, "fail", finding.evidence + f"; read-back status={rb.get('status_code')}")
    return finding


def _check_clin_010(evidence: dict[str, Any], rule: Rule) -> Finding:
    """The written note is found by DocumentReference.Search."""
    search, missing = _clinician_step(evidence, rule, "note_search")
    if missing is not None:
        return missing
    note = evidence["clinician"].get("note_write") or {}
    if not note.get("posted"):
        return _finding(rule, "needs_human", "no note was written to search for",
                        "Note write did not succeed — judged by CLIN-009.")
    ok = _ok_status(search.get("status_code")) and search.get("found_written_note")
    return _finding(rule, "pass" if ok else "fail",
                    f"status={search.get('status_code')}; results={search.get('count')}; "
                    f"note {note.get('id')!r} found={search.get('found_written_note')}; error={search.get('error', '')}")


def _check_clin_011(evidence: dict[str, Any], rule: Rule) -> Finding:
    """Mobile app registered as a public client."""
    reg = evidence.get("app_registration") or {}
    client_type = reg.get("client_type", "")
    detail = f"APP_CLIENT_TYPE={client_type!r}; tool sends client secret={reg.get('tool_sends_client_secret')}"
    if not client_type:
        return _finding(rule, "needs_human", detail, "Not attested — set APP_CLIENT_TYPE.")
    if client_type != "public":
        return _finding(rule, "fail", detail)
    if reg.get("tool_sends_client_secret"):
        return _finding(rule, "needs_human", detail,
                        "Registered public, but this tool logged in with a client secret, so its login "
                        "did not test the app's real (secretless) flow. Clear OAUTH_CLIENT_SECRET and rerun.")
    return _finding(rule, "pass", detail)


def _check_clin_012(evidence: dict[str, Any], rule: Rule) -> Finding:
    """Redirect URI is a deep link or https."""
    from urllib.parse import urlparse

    uri = (evidence.get("app_registration") or {}).get("redirect_uri", "")
    if not uri:
        return _finding(rule, "needs_human", "APP_REDIRECT_URI not set", "Not attested — set APP_REDIRECT_URI.")
    scheme = urlparse(uri).scheme.lower()
    if not scheme:
        return _finding(rule, "fail", f"redirect_uri={uri!r} has no scheme")
    ok = scheme != "http"
    kind = "https link" if scheme == "https" else ("plain http" if scheme == "http" else f"private-use scheme {scheme!r}")
    return _finding(rule, "pass" if ok else "fail", f"redirect_uri={uri!r} ({kind})")


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
    "CLIN-001": _check_clin_001,
    "CLIN-002": _check_clin_002,
    "CLIN-003": _check_clin_003,
    "CLIN-004": _check_clin_004,
    "CLIN-005": _check_clin_005,
    "CLIN-006": _check_clin_006,
    "CLIN-007": _check_clin_007,
    "CLIN-008": _check_clin_008,
    "CLIN-009": _check_clin_009,
    "CLIN-010": _check_clin_010,
    "CLIN-011": _check_clin_011,
    "CLIN-012": _check_clin_012,
    # SEAM: HL7 FHIR Validator wrapping will add rule IDs here (e.g. FHIR-V-*)
    # SEAM: ONC g10 Test Kit invocation will add rule IDs here (e.g. ONC-G10-*)
}
