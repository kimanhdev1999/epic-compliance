"""Top-level pipeline orchestrator — wires all stages together."""
from __future__ import annotations

from typing import Any

from .config import AppConfig
from .smart.discovery import fetch_smart_configuration
from urllib.parse import parse_qsl

from .smart.pkce import MOCK_AUTH_PROBE, MOCK_CLINICIAN_TOKEN_RESPONSE, MOCK_TOKEN_RESPONSE, TokenResponse
from .fhir.client import FhirClient
from .fhir.writeback import (
    US_CORE_DOCREF,
    build_clinical_photo,
    build_consult_note,
    build_diagnostic_report,
    build_media,
    build_observation,
)
from .validator import get_validator
from .rules_engine.engine import run_pipeline
from .models import Report


#: Why a live run has no token. Any evidence dict carrying this key means the
#: token-dependent rules MUST NOT return pass/fail — there is nothing to judge.
TOKEN_UNAVAILABLE_REASON = (
    "Live run: no access token was obtained. Start a launch at GET /launch and "
    "complete it at GET /callback (a browser + Epic login is required) before "
    "running the pipeline. Token-dependent rules cannot be evaluated until then."
)


# --------------------------------------------------------------------------- #
# Live-token holder
#
# api.py's /callback route populates this after a real SMART launch completes.
# Single-tenant, in-process, module-level — deliberately not a database table:
# this holds exactly one "current" live token for the one app this tool is
# configured against, the same single-tenant scope as config.py. It is NOT
# multi-organization state.
# --------------------------------------------------------------------------- #
_live_token_holder: dict[str, Any] = {"token": None, "auth_probe": None}


def set_live_token(token: TokenResponse, auth_probe: dict[str, Any] | None = None) -> None:
    """Called by api.py's /callback after a real authorization-code exchange."""
    _live_token_holder["token"] = token
    _live_token_holder["auth_probe"] = auth_probe or {}


def get_live_token() -> TokenResponse | None:
    return _live_token_holder["token"]


def get_live_auth_probe() -> dict[str, Any]:
    return dict(_live_token_holder["auth_probe"] or {})


def clear_live_token() -> None:
    _live_token_holder["token"] = None
    _live_token_holder["auth_probe"] = None


def collect_writeback_evidence(
    fhir_client: FhirClient,
    validator: Any,
    patient_id: str,
) -> dict[str, Any]:
    """Stage: construct, validate, and POST the write-back resources.

    Mirrors the "direction B" flow this app implements: after an AI diagnosis,
    it POSTs an Observation (the finding), a DiagnosticReport (referencing that
    Observation), and a Media resource (the source photo) into Epic. This
    function performs exactly that — as an outbound client — against whatever
    FhirClient it is given (mock or live), and validates each payload against
    US Core with the existing HL7 validator before/after the POST.

    Returns one entry per resource type:
        {
          "validation_valid": True | False | None,   # tri-state, see ValidationResult.is_valid()
          "validation_summary": str,
          "status_code": int | None,
          "posted": True | False | None,              # None = request itself never completed
          "error": str,
        }
    """
    from .fhir.writeback import WRITE_PROFILES

    def _entry(resource: dict[str, Any]) -> dict[str, Any]:
        rt = resource["resourceType"]
        validation = validator.validate(resource, WRITE_PROFILES.get(rt, ""))
        post = fhir_client.post_resource(resource)
        status_code = post.get("status_code")
        posted = (200 <= status_code < 300) if isinstance(status_code, int) else None
        return {
            "validation_valid": validation.is_valid(),
            "validation_summary": validation.summary(),
            "status_code": status_code,
            "posted": posted,
            "error": post.get("_error", ""),
            "resource_returned": post.get("resource", {}),
        }, post

    observation = build_observation(patient_id)
    obs_entry, obs_post = _entry(observation)

    obs_id = (obs_post.get("resource") or {}).get("id", "") or "pending-observation"
    diagnostic_report = build_diagnostic_report(patient_id, obs_id)
    dr_entry, _ = _entry(diagnostic_report)

    media = build_media(patient_id)
    media_entry, _ = _entry(media)

    return {
        "Observation": obs_entry,
        "DiagnosticReport": dr_entry,
        "Media": media_entry,
    }


#: Mock runs search for the fixture patient's MRN when no query is set.
MOCK_PATIENT_QUERY = "identifier=12345"


def _ref_id(reference: str, resource_type: str) -> str:
    """'Practitioner/abc', a full URL ending in it, or a Location header with
    /_history/N -> 'abc'. Empty if ``resource_type`` is not in it."""
    parts = reference.split("/_history/")[0].rstrip("/").split("/")
    if len(parts) >= 2 and parts[-2] == resource_type:
        return parts[-1]
    return ""


def _points_at(resource: dict[str, Any], field: str, resource_type: str) -> str:
    """Id that ``resource[field]`` references (handles Reference or [Reference])."""
    value = resource.get(field) or {}
    if isinstance(value, list):
        value = value[0] if value else {}
    return _ref_id(value.get("reference", ""), resource_type)


def _encounter_of(resource: dict[str, Any]) -> str:
    if resource.get("resourceType") == "Media":
        return _points_at(resource, "encounter", "Encounter")
    return _points_at(resource.get("context") or {}, "encounter", "Encounter")


def _write_and_read_back(
    fhir_client: FhirClient, validator: Any, resource: dict[str, Any], profile: str
) -> dict[str, Any]:
    """POST one resource, then read it back by id from Epic.

    Judging what Epic *stored* (not what was sent) is the point: a resource
    filed against the wrong patient or no encounter only shows up on read.
    """
    rt = resource["resourceType"]
    validation = validator.validate(resource, profile)
    post = fhir_client.post_resource(resource)
    status_code = post.get("status_code")
    created_id = (post.get("resource") or {}).get("id", "") or _ref_id(post.get("location", ""), rt)
    entry: dict[str, Any] = {
        "resource_type": rt,
        "validation_valid": validation.is_valid(),
        "validation_summary": validation.summary(),
        "status_code": status_code,
        "posted": (200 <= status_code < 300) if isinstance(status_code, int) else None,
        "id": created_id,
        "error": post.get("_error", ""),
    }
    if not entry["posted"] or not created_id:
        entry["read_back"] = {"_unavailable": "write did not succeed or returned no id"}
        return entry

    read = fhir_client.read(rt, created_id)
    stored = read.get("resource") or {}
    entry["read_back"] = {
        "status_code": read.get("status_code"),
        "patient": _points_at(stored, "subject", "Patient"),
        "encounter": _encounter_of(stored),
        "error": read.get("_error", ""),
        "_stored": stored,
    }
    return entry


def _attachment_retrievable(fhir_client: FhirClient, stored: dict[str, Any]) -> dict[str, Any]:
    """Whether the image bytes come back: inline data, or Binary.Read of the url."""
    if stored.get("resourceType") == "Media":
        attachment = stored.get("content") or {}
    else:
        attachment = ((stored.get("content") or [{}])[0]).get("attachment") or {}
    if attachment.get("data"):
        return {"how": "inline data", "ok": True}
    binary_id = _ref_id(attachment.get("url", ""), "Binary")
    if not binary_id:
        return {"how": f"no data and no Binary url (url={attachment.get('url', '')!r})", "ok": False}
    read = fhir_client.read("Binary", binary_id)
    status = read.get("status_code")
    return {"how": f"Binary/{binary_id} status={status}", "ok": isinstance(status, int) and status < 300}


def collect_clinician_evidence(
    fhir_client: FhirClient,
    validator: Any,
    config: AppConfig,
    fhir_user: str,
) -> dict[str, Any]:
    """Stage: the clinician_standalone flow, as the app would perform it.

    Find and confirm the patient, identify the clinician, find the visit,
    file a photo, read it back, read what the derm needs, file a note, and
    find it again. Each step that depends on an earlier one is marked
    unavailable when that step failed, so its rule says needs_human instead
    of failing for a reason it does not test.
    """
    ev: dict[str, Any] = {}

    # Who signed in (fhirUser) -> Practitioner.Read
    practitioner_id = _ref_id(fhir_user, "Practitioner")
    if not practitioner_id:
        ev["practitioner"] = {"fhir_user": fhir_user, "_unavailable":
                              "token carried no Practitioner fhirUser — request openid fhirUser scopes"}
    else:
        read = fhir_client.read("Practitioner", practitioner_id)
        ev["practitioner"] = {
            "fhir_user": fhir_user,
            "status_code": read.get("status_code"),
            "has_name": bool((read.get("resource") or {}).get("name")),
            "error": read.get("_error", ""),
        }
    practitioner_ref = f"Practitioner/{practitioner_id}" if practitioner_id else ""

    # Patient.Search -> Patient.Read (the confirmation screen)
    query = config.test_patient_query or (MOCK_PATIENT_QUERY if fhir_client.mock else "")
    if not query:
        ev["patient_search"] = {"_unavailable": "TEST_PATIENT_QUERY not set — nothing to search for"}
        return ev
    search = fhir_client.search("Patient", dict(parse_qsl(query)))
    matches = search.get("entries", [])
    ev["patient_search"] = {
        "query": query,
        "status_code": search.get("status_code"),
        "match_count": len(matches),
        "error": search.get("_error", ""),
    }
    if not matches:
        return ev
    patient_id = matches[0].get("id", "")
    ev["selected_patient_id"] = patient_id

    read = fhir_client.read("Patient", patient_id)
    pt = read.get("resource") or {}
    ev["patient"] = {
        "id": patient_id,
        "status_code": read.get("status_code"),
        "has_name": bool(pt.get("name")),
        "has_birthDate": bool(pt.get("birthDate")),
        "has_identifier": bool(pt.get("identifier")),
        "error": read.get("_error", ""),
    }

    # Encounter.Search — no launch context, so the visit must be searched for
    enc = fhir_client.search("Encounter", {"patient": patient_id})
    encounters = enc.get("entries", [])
    encounter_id = encounters[0].get("id", "") if encounters else ""
    ev["encounter"] = {
        "status_code": enc.get("status_code"),
        "count": len(encounters),
        "id": encounter_id,
        "required": config.encounter_required,
        "error": enc.get("_error", ""),
    }

    # Photo write + read-back + Binary
    photo = build_clinical_photo(
        config.image_write_resource, patient_id,
        encounter_id=encounter_id, practitioner_ref=practitioner_ref,
    )
    profile = US_CORE_DOCREF if config.image_write_resource == "DocumentReference" else ""
    ev["image_write"] = _write_and_read_back(fhir_client, validator, photo, profile)
    stored = ev["image_write"]["read_back"].get("_stored")
    if stored:
        ev["image_write"]["attachment"] = _attachment_retrievable(fhir_client, stored)

    # Derm review reads (empty results are fine; access is what is tested)
    ev["review_reads"] = {}
    for rt in ("Condition", "MedicationRequest", "AllergyIntolerance"):
        r = fhir_client.search(rt, {"patient": patient_id})
        ev["review_reads"][rt] = {"status_code": r.get("status_code"), "count": len(r.get("entries", [])),
                                  "error": r.get("_error", "")}

    # Consult note write + read-back, then find it by search
    note = build_consult_note(patient_id, encounter_id=encounter_id, practitioner_ref=practitioner_ref)
    ev["note_write"] = _write_and_read_back(fhir_client, validator, note, US_CORE_DOCREF)
    docs = fhir_client.search("DocumentReference", {"patient": patient_id})
    ev["note_search"] = {
        "status_code": docs.get("status_code"),
        "count": len(docs.get("entries", [])),
        "found_written_note": bool(ev["note_write"].get("id")) and any(
            d.get("id") == ev["note_write"]["id"] for d in docs.get("entries", [])
        ),
        "error": docs.get("_error", ""),
    }

    # _stored was only needed for the attachment check; keep the report small.
    for key in ("image_write", "note_write"):
        ev[key]["read_back"].pop("_stored", None)
    return ev


def collect_evidence(config: AppConfig) -> dict[str, Any]:
    """Stage 1-3: discovery, auth probe, FHIR fetch, write-back. Returns evidence dict.

    In live mode there is no token until a real launch has completed through
    api.py's /callback route (see set_live_token). Until then, token- and
    write-back-dependent evidence is marked unavailable rather than silently
    substituted with the mock fixture. Faking it would make AUTH-003/005 and
    the WRITE-* rules structurally incapable of failing — a compliance tool
    that cannot fail is worse than no tool at all.
    """
    mock = config.run_mode == "mock"
    clinician = config.launch_profile == "clinician_standalone"

    # A failed discovery is itself the AUTH-001 finding, not a crashed run —
    # a wrong FHIR_BASE_URL must surface as a fail with a fix hint.
    try:
        smart_evidence: dict[str, Any] = fetch_smart_configuration(
            config.fhir_base_url, mock=mock
        ).model_dump()
    except Exception as exc:
        smart_evidence = {"_error": f"{type(exc).__name__}: {exc}"}

    if mock:
        token: TokenResponse | None = MOCK_CLINICIAN_TOKEN_RESPONSE if clinician else MOCK_TOKEN_RESPONSE
        auth_probe: dict[str, Any] | None = dict(MOCK_AUTH_PROBE)
    else:
        token = get_live_token()
        # Only state_validated is actually proven by a normal /callback launch —
        # the other AUTH-007..010 probes are deliberate negative paths (wrong
        # verifier, replayed code, cross-patient read) that a real launch never
        # exercises on purpose. Those stay needs_human until pytest -m launch
        # (or a future interactive "run negative probes" action) supplies them.
        auth_probe = dict(get_live_auth_probe()) if token is not None else None

    if token is not None:
        token_evidence: dict[str, Any] = {
            "access_token": "[REDACTED]",
            "token_type": token.token_type,
            "expires_in": token.expires_in,
            "scope": token.scope,
            "patient": token.patient,
            "fhir_user": token.fhir_user,
            "id_token": "[REDACTED]" if token.id_token else "",
            "refresh_token": "[REDACTED]" if token.refresh_token else "",
        }
        access_token = token.access_token
        patient_id = token.patient or "mock-patient-id"
    else:
        token_evidence = {"_unavailable": TOKEN_UNAVAILABLE_REASON}
        auth_probe = {"_unavailable": TOKEN_UNAVAILABLE_REASON}
        access_token = ""
        patient_id = "mock-patient-id"

    fhir_client = FhirClient(
        base_url=config.fhir_base_url,
        access_token=access_token,
        mock=mock,
    )
    clinician_evidence: dict[str, Any] = {"_unavailable": "LAUNCH_PROFILE is not clinician_standalone"}
    if token is not None and clinician:
        # No patient in the token: the patient is whoever the search confirms.
        clinician_evidence = collect_clinician_evidence(
            fhir_client, get_validator(config), config, token.fhir_user
        )
        patient_id = clinician_evidence.get("selected_patient_id", "")
    elif token is None:
        clinician_evidence = {"_unavailable": TOKEN_UNAVAILABLE_REASON}

    if token is not None and patient_id:
        fhir_resources = fhir_client.fetch_all_us_core(patient_id)
    elif token is not None:
        fhir_resources = {"_unavailable": "No patient selected — set TEST_PATIENT_QUERY to one sandbox test patient."}
    else:
        # Reading with an empty bearer token only yields 401s, which the FHIR
        # rules would misread as non-conformant data.
        fhir_resources = {"_unavailable": TOKEN_UNAVAILABLE_REASON}

    if token is not None and not clinician:
        validator = get_validator(config)
        write_back = collect_writeback_evidence(fhir_client, validator, patient_id)
    elif token is not None:
        # The patient_launch write-back rules do not run under this profile.
        write_back = {"_unavailable": "patient_launch write-back not run under clinician_standalone"}
    else:
        write_back = {"_unavailable": TOKEN_UNAVAILABLE_REASON}

    app_config_fields = {
        "transport_tls_version": config.transport_tls_version,
        "audit_logging_enabled": config.audit_logging_enabled,
        "audit_log_retention_days": config.audit_log_retention_days,
        "baa_in_place": config.baa_in_place,
        "covered_entity_relationship": config.covered_entity_relationship,
    }
    attested = {k: v for k, v in app_config_fields.items() if v}
    app_config = attested if attested else {
        "_unavailable": "No app_config fields attested — set TRANSPORT_TLS_VERSION, "
        "AUDIT_LOGGING_ENABLED, AUDIT_LOG_RETENTION_DAYS, BAA_IN_PLACE, "
        "COVERED_ENTITY_RELATIONSHIP in .env or the dashboard form."
    }

    return {
        "smart_configuration": smart_evidence,
        "token_response": token_evidence,
        "auth_probe": auth_probe,
        "fhir_resources": fhir_resources,
        "write_back": write_back,
        "clinician": clinician_evidence,
        "app_registration": {
            "launch_profile": config.launch_profile,
            "client_type": config.app_client_type,
            "redirect_uri": config.app_redirect_uri,
            # The tool logs in with the app's client id; a secret sent for a
            # public client means the tool is not testing the app's real login.
            "tool_sends_client_secret": bool(config.oauth_client_secret),
        },
        "app_config": app_config,
        "run_mode": config.run_mode,
        "launch_profile": config.launch_profile,
    }


def run_full_pipeline(config: AppConfig, rule_ids: set[str] | None = None) -> Report:
    """Run the complete pipeline: evidence collection + validation.

    ``rule_ids`` limits validation to an assessment plan's checks.
    """
    evidence = collect_evidence(config)
    mock_llm = not bool(config.anthropic_api_key) or config.run_mode == "mock"
    return run_pipeline(
        evidence=evidence,
        launch_profile=config.launch_profile,
        rule_ids=rule_ids,
        api_key=config.anthropic_api_key,
        mock_llm=mock_llm,
    )
