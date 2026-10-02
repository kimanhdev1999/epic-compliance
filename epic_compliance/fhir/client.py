"""FHIR client — fetches US Core resources using a bearer token.

In mock mode returns offline fixture data so the pipeline runs end-to-end
without a live FHIR endpoint.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

US_CORE_RESOURCES = [
    "Patient",
    "Observation",
    "Condition",
    "MedicationRequest",
    "AllergyIntolerance",
    "Immunization",
    "DiagnosticReport",
    "DocumentReference",
    "Encounter",
    "Procedure",
]

# Minimal mock FHIR resources — enough to test rule assertions
MOCK_FHIR_RESOURCES: dict[str, Any] = {
    "Patient": {
        "resourceType": "Patient",
        "id": "mock-patient-id",
        "meta": {"profile": ["http://hl7.org/fhir/us/core/StructureDefinition/us-core-patient"]},
        "identifier": [{"system": "urn:oid:1.2.840.114350.1.13.0.1.7.5.737384.0", "value": "12345"}],
        "name": [{"use": "official", "family": "Smith", "given": ["John"]}],
        "gender": "male",
        "birthDate": "1970-01-01",
        "address": [{"line": ["123 Main St"], "city": "Springfield", "state": "IL", "postalCode": "62701"}],
    },
    "Observation": {
        "resourceType": "Observation",
        "id": "mock-obs-1",
        "meta": {"profile": ["http://hl7.org/fhir/us/core/StructureDefinition/us-core-observation-lab"]},
        "status": "final",
        "category": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/observation-category", "code": "laboratory"}]}],
        "code": {"coding": [{"system": "http://loinc.org", "code": "2339-0", "display": "Glucose [Mass/volume] in Blood"}]},
        "subject": {"reference": "Patient/mock-patient-id"},
        "effectiveDateTime": "2024-01-15T10:30:00Z",
        "valueQuantity": {"value": 95.0, "unit": "mg/dL", "system": "http://unitsofmeasure.org", "code": "mg/dL"},
    },
    "Condition": {
        "resourceType": "Condition",
        "id": "mock-condition-1",
        "meta": {"profile": ["http://hl7.org/fhir/us/core/StructureDefinition/us-core-condition"]},
        "clinicalStatus": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/condition-clinical", "code": "active"}]},
        "verificationStatus": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/condition-ver-status", "code": "confirmed"}]},
        "category": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/condition-category", "code": "problem-list-item"}]}],
        "code": {"coding": [{"system": "http://snomed.info/sct", "code": "73211009", "display": "Diabetes mellitus"}]},
        "subject": {"reference": "Patient/mock-patient-id"},
    },
    "MedicationRequest": {
        "resourceType": "MedicationRequest",
        "id": "mock-medrx-1",
        "meta": {"profile": ["http://hl7.org/fhir/us/core/StructureDefinition/us-core-medicationrequest"]},
        "status": "active",
        "intent": "order",
        "medicationCodeableConcept": {"coding": [{"system": "http://www.nlm.nih.gov/research/umls/rxnorm", "code": "314076", "display": "Metformin 500mg"}]},
        "subject": {"reference": "Patient/mock-patient-id"},
        "authoredOn": "2024-01-10",
    },
    "AllergyIntolerance": {
        "resourceType": "AllergyIntolerance",
        "id": "mock-allergy-1",
        "meta": {"profile": ["http://hl7.org/fhir/us/core/StructureDefinition/us-core-allergyintolerance"]},
        "clinicalStatus": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/allergyintolerance-clinical", "code": "active"}]},
        "verificationStatus": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/allergyintolerance-verification", "code": "confirmed"}]},
        "patient": {"reference": "Patient/mock-patient-id"},
        "code": {"coding": [{"system": "http://www.nlm.nih.gov/research/umls/rxnorm", "code": "7980", "display": "Penicillin"}]},
    },
    "Immunization": {
        "resourceType": "Immunization",
        "id": "mock-imm-1",
        "meta": {"profile": ["http://hl7.org/fhir/us/core/StructureDefinition/us-core-immunization"]},
        "status": "completed",
        "vaccineCode": {"coding": [{"system": "http://hl7.org/fhir/sid/cvx", "code": "141", "display": "Influenza, seasonal, injectable"}]},
        "patient": {"reference": "Patient/mock-patient-id"},
        "occurrenceDateTime": "2023-10-15",
    },
    "DiagnosticReport": {
        "resourceType": "DiagnosticReport",
        "id": "mock-diagreport-1",
        "meta": {"profile": ["http://hl7.org/fhir/us/core/StructureDefinition/us-core-diagnosticreport-lab"]},
        "status": "final",
        "category": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/v2-0074", "code": "LAB"}]}],
        "code": {"coding": [{"system": "http://loinc.org", "code": "24323-8", "display": "Comprehensive metabolic panel"}]},
        "subject": {"reference": "Patient/mock-patient-id"},
        "effectiveDateTime": "2024-01-15T10:30:00Z",
    },
    "DocumentReference": {
        "resourceType": "DocumentReference",
        "id": "mock-docref-1",
        "meta": {"profile": ["http://hl7.org/fhir/us/core/StructureDefinition/us-core-documentreference"]},
        "status": "current",
        "type": {"coding": [{"system": "http://loinc.org", "code": "34133-9", "display": "Summary of episode note"}]},
        "subject": {"reference": "Patient/mock-patient-id"},
        "date": "2024-01-15T10:30:00Z",
        "content": [{"attachment": {"contentType": "application/pdf", "url": "DocumentReference/mock-docref-1/binary"}}],
    },
    "Encounter": {
        "resourceType": "Encounter",
        "id": "mock-encounter-1",
        "meta": {"profile": ["http://hl7.org/fhir/us/core/StructureDefinition/us-core-encounter"]},
        "status": "finished",
        "class": {"system": "http://terminology.hl7.org/CodeSystem/v3-ActCode", "code": "AMB"},
        "subject": {"reference": "Patient/mock-patient-id"},
        "period": {"start": "2024-01-15T10:00:00Z", "end": "2024-01-15T10:45:00Z"},
    },
    "Procedure": {
        "resourceType": "Procedure",
        "id": "mock-procedure-1",
        "meta": {"profile": ["http://hl7.org/fhir/us/core/StructureDefinition/us-core-procedure"]},
        "status": "completed",
        "code": {"coding": [{"system": "http://snomed.info/sct", "code": "71388002", "display": "Procedure"}]},
        "subject": {"reference": "Patient/mock-patient-id"},
        "performedDateTime": "2024-01-15T10:30:00Z",
    },
}


class FhirClient:
    def __init__(self, base_url: str, access_token: str, mock: bool = False) -> None:
        self.base_url = base_url.rstrip("/")
        self.access_token = access_token
        self.mock = mock

    def fetch_resource(self, resource_type: str, patient_id: str = "mock-patient-id") -> dict[str, Any]:
        """Fetch a single resource (or return mock fixture)."""
        if self.mock:
            return MOCK_FHIR_RESOURCES.get(resource_type, {
                "resourceType": resource_type,
                "id": f"mock-{resource_type.lower()}-stub",
                "_mock": True,
                "_note": f"No fixture defined for {resource_type}",
            })

        url = f"{self.base_url}/{resource_type}"
        params = {"patient": patient_id, "_count": "1"}
        headers = {"Authorization": f"Bearer {self.access_token}", "Accept": "application/fhir+json"}
        resp = httpx.get(url, params=params, headers=headers, timeout=15)
        resp.raise_for_status()
        bundle = resp.json()
        entries = bundle.get("entry", [])
        if entries:
            return entries[0].get("resource", {})
        return {"resourceType": resource_type, "_empty": True}

    def fetch_all_us_core(self, patient_id: str = "mock-patient-id") -> dict[str, Any]:
        """Fetch all US Core resources and return as {resource_type: resource_dict}."""
        results: dict[str, Any] = {}
        for rt in US_CORE_RESOURCES:
            try:
                results[rt] = self.fetch_resource(rt, patient_id)
            except Exception as exc:
                results[rt] = {"resourceType": rt, "_error": str(exc)}
        return results

    def post_resource(self, resource: dict[str, Any]) -> dict[str, Any]:
        """POST one resource to the FHIR server, as this app's own client (write-back).

        This app never hosts a FHIR server for others to query (see
        fhir/writeback.py) — this is the one place it acts as a write client.

        Returns ``{"status_code": int, "resource": dict}`` on any HTTP response
        (2xx or not — a 403 is a valid, informative result, not an exception).
        Returns ``{"status_code": None, "_error": str}`` only if the request
        could not be made at all (network failure). Mock mode fabricates a 201
        Created response with a synthetic id so the pipeline runs offline.
        """
        rt = resource.get("resourceType", "Resource")
        if self.mock:
            created = dict(resource)
            created["id"] = f"mock-{rt.lower()}-created-1"
            return {"status_code": 201, "resource": created}

        url = f"{self.base_url}/{rt}"
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/fhir+json",
            "Accept": "application/fhir+json",
        }
        try:
            resp = httpx.post(url, json=resource, headers=headers, timeout=15)
        except (httpx.HTTPError, httpx.TimeoutException) as exc:
            return {"status_code": None, "_error": str(exc)}
        try:
            body = resp.json()
        except ValueError:
            body = {"_raw": resp.text[:500]}
        return {"status_code": resp.status_code, "resource": body}
