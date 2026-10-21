"""Write-back resource builders — the "direction B" push model.

This app's architecture (decided outside this tool, documented in
notes/decisions.md ADR-006): a mobile app sends a patient photo to its own
backend, the backend runs an AI diagnosis, then POSTs the result INTO Epic as
FHIR resources using write scopes granted at SMART launch. The doctor sees it
natively in Epic's chart.

This module only builds the resource bodies. Nothing here hosts a FHIR server,
issues a CapabilityStatement, or accepts inbound requests — this app is
exclusively an outbound FHIR *client* making POST calls to Epic, the same as
any other SMART app. Do not add server-side FHIR routes here.
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone
from typing import Any

#: Scopes this write-back flow needs at SMART launch, on top of whatever read
#: scopes the app also requests (see api.py LAUNCH_SCOPES). Distinct from the
#: read scopes AUTH-003 checks — Epic grants read/write independently.
WRITE_SCOPES_NEEDED = [
    "patient/DiagnosticReport.write",
    "patient/Observation.write",
    "patient/Media.write",
]

#: US Core 3.1.1 profile canonicals for the write-back resources, where one
#: exists (empty profile = base FHIR R4 validation). US Core 3.1.1 has no Media
#: profile, and its Observation profiles (lab, vitals, smoking status) don't fit
#: an AI imaging finding. us-core-observation-clinical-result is US Core 6.x
#: only: the validator, which loads 3.1.1, can't resolve it and answers HTTP 500.
WRITE_PROFILES: dict[str, str] = {
    "Observation": "",
    "DiagnosticReport": "http://hl7.org/fhir/us/core/StructureDefinition/us-core-diagnosticreport-note",
    "Media": "",
}


def build_observation(
    patient_id: str,
    *,
    value_text: str = "AI-assisted diagnosis finding: no acute abnormality detected.",
    status: str = "final",
) -> dict[str, Any]:
    """The AI diagnosis finding, as an Observation this app writes back."""
    return {
        "resourceType": "Observation",
        "status": status,
        "category": [
            {
                "coding": [
                    {
                        "system": "http://terminology.hl7.org/CodeSystem/observation-category",
                        "code": "imaging",
                        "display": "Imaging",
                    }
                ]
            }
        ],
        "code": {
            "coding": [
                # LOINC's exact display; the validator rejects any other wording.
                {"system": "http://loinc.org", "code": "18782-3",
                 "display": "Radiology Study observation (narrative)"}
            ]
        },
        "subject": {"reference": f"Patient/{patient_id}"},
        "effectiveDateTime": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "valueString": value_text,
    }


def build_diagnostic_report(
    patient_id: str,
    observation_id: str,
    *,
    status: str = "final",
) -> dict[str, Any]:
    """The report the doctor sees in the chart, referencing the AI Observation."""
    return {
        "resourceType": "DiagnosticReport",
        "status": status,
        "category": [
            {
                "coding": [
                    {"system": "http://terminology.hl7.org/CodeSystem/v2-0074", "code": "OTH", "display": "Other"}
                ]
            }
        ],
        "code": {
            "coding": [
                {"system": "http://loinc.org", "code": "18748-4", "display": "Diagnostic imaging study"}
            ]
        },
        "subject": {"reference": f"Patient/{patient_id}"},
        # Required by us-core-diagnosticreport-note (effective[x] 1..1).
        "effectiveDateTime": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "result": [{"reference": f"Observation/{observation_id}"}],
    }


def build_media(
    patient_id: str,
    *,
    content_type: str = "image/jpeg",
    content_url: str = "",
) -> dict[str, Any]:
    """The patient photo the diagnosis was made from, as a Media resource.

    US Core does not define a Media profile, so this validates against the
    base R4 Media resource shape only (see WRITE_PROFILES).
    """
    content: dict[str, Any] = {"contentType": content_type, "title": "AI diagnosis source photo"}
    if content_url:
        content["url"] = content_url
    return {
        "resourceType": "Media",
        "status": "completed",
        "subject": {"reference": f"Patient/{patient_id}"},
        "content": content,
    }


# --------------------------------------------------------------------------- #
# clinician_standalone (ADR-010): the clinician photographs a patient they
# searched for and confirmed, then the derm files a consult note. Same rule as
# above: these are bodies this app POSTs to Epic, never served by it.
# --------------------------------------------------------------------------- #

#: Scopes a clinician standalone login requests. user/* because no patient is
#: selected at login; no launch/patient, and no offline_access because a
#: public (mobile) client is not given refresh tokens for it.
CLINICIAN_SCOPES = [
    "openid",
    "fhirUser",
    "user/Patient.read",
    "user/Practitioner.read",
    "user/Encounter.read",
    "user/Media.read",
    "user/Media.write",
    "user/Binary.read",
    "user/Condition.read",
    "user/MedicationRequest.read",
    "user/AllergyIntolerance.read",
    "user/DocumentReference.read",
    "user/DocumentReference.write",
]

US_CORE_DOCREF = "http://hl7.org/fhir/us/core/StructureDefinition/us-core-documentreference"

#: A 1x1 JPEG — a real image so Binary read-back has bytes to return.
SAMPLE_JPEG_B64 = (
    "/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAP//////////////////////////////////////////"
    "////////////////////////////////////////////wgALCAABAAEBAREA/8QAFBABAAAAAAAA"
    "AAAAAAAAAAAAAP/aAAgBAQABPxA="
)

_CLINICAL_NOTE_CATEGORY = [{
    "coding": [{
        "system": "http://hl7.org/fhir/us/core/CodeSystem/us-core-documentreference-category",
        "code": "clinical-note",
        "display": "Clinical Note",
    }]
}]


def _context(encounter_id: str) -> dict[str, Any]:
    return {"encounter": [{"reference": f"Encounter/{encounter_id}"}]} if encounter_id else {}


def build_clinical_photo(
    resource_type: str,
    patient_id: str,
    *,
    encounter_id: str = "",
    practitioner_ref: str = "",
    data_b64: str = SAMPLE_JPEG_B64,
) -> dict[str, Any]:
    """The clinician's photo, as whichever resource the organization's build
    files images as (IMAGE_WRITE_RESOURCE): Media, or DocumentReference."""
    attachment = {"contentType": "image/jpeg", "data": data_b64, "title": "Clinical photo"}
    if resource_type == "Media":
        media: dict[str, Any] = {
            "resourceType": "Media",
            "status": "completed",
            "type": {"coding": [{
                "system": "http://terminology.hl7.org/CodeSystem/media-type",
                "code": "image", "display": "Image",
            }]},
            "subject": {"reference": f"Patient/{patient_id}"},
            "content": attachment,
        }
        if encounter_id:
            media["encounter"] = {"reference": f"Encounter/{encounter_id}"}
        if practitioner_ref:
            media["operator"] = {"reference": practitioner_ref}
        return media

    doc: dict[str, Any] = {
        "resourceType": "DocumentReference",
        "status": "current",
        "type": {"coding": [{"system": "http://loinc.org", "code": "72170-4", "display": "Photographic image"}]},
        "category": _CLINICAL_NOTE_CATEGORY,
        "subject": {"reference": f"Patient/{patient_id}"},
        "content": [{"attachment": attachment}],
    }
    if practitioner_ref:
        doc["author"] = [{"reference": practitioner_ref}]
    if encounter_id:
        doc["context"] = _context(encounter_id)
    return doc


def build_consult_note(
    patient_id: str,
    *,
    encounter_id: str = "",
    practitioner_ref: str = "",
    text: str = "Dermatology e-consult: compliance-tool test note. Not clinical content.",
) -> dict[str, Any]:
    """The derm's assessment, as a Clinical Notes DocumentReference."""
    doc: dict[str, Any] = {
        "resourceType": "DocumentReference",
        "status": "current",
        "docStatus": "final",
        "type": {"coding": [{"system": "http://loinc.org", "code": "11488-4", "display": "Consult note"}]},
        "category": _CLINICAL_NOTE_CATEGORY,
        "subject": {"reference": f"Patient/{patient_id}"},
        "content": [{"attachment": {
            "contentType": "text/plain",
            "data": base64.b64encode(text.encode()).decode("ascii"),
        }}],
    }
    if practitioner_ref:
        doc["author"] = [{"reference": practitioner_ref}]
    if encounter_id:
        doc["context"] = _context(encounter_id)
    return doc
