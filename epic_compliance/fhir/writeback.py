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
#: exists. US Core 3.1.1 has no Media profile, so Media validates against the
#: base FHIR R4 spec only (empty profile = base-spec validation).
WRITE_PROFILES: dict[str, str] = {
    "Observation": "http://hl7.org/fhir/us/core/StructureDefinition/us-core-observation-clinical-result",
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
                {"system": "http://loinc.org", "code": "18782-3", "display": "Radiology Study observation"}
            ]
        },
        "subject": {"reference": f"Patient/{patient_id}"},
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
