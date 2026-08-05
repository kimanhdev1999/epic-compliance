"""US Core profile canonicals for the resources the tool fetches.

US Core 3.1.1 is the version ONC (g)(10) certification requires for USCDI v1,
which is what this tool targets. Pinning the version matters: validating against
a newer US Core would report failures a certifier would not.
"""
from __future__ import annotations

US_CORE_VERSION = "3.1.1"
_BASE = "http://hl7.org/fhir/us/core/StructureDefinition/"

US_CORE_PROFILES: dict[str, str] = {
    "Patient": f"{_BASE}us-core-patient",
    "Observation": f"{_BASE}us-core-observation-lab",
    "Condition": f"{_BASE}us-core-condition",
    "MedicationRequest": f"{_BASE}us-core-medicationrequest",
    "AllergyIntolerance": f"{_BASE}us-core-allergyintolerance",
    "Immunization": f"{_BASE}us-core-immunization",
    "DiagnosticReport": f"{_BASE}us-core-diagnosticreport-lab",
    "DocumentReference": f"{_BASE}us-core-documentreference",
    "Encounter": f"{_BASE}us-core-encounter",
    "Procedure": f"{_BASE}us-core-procedure",
}


def profile_for(resource: dict) -> str:
    """Best profile canonical for a resource.

    Prefers a US Core profile the resource declares in meta.profile — a server
    may claim a more specific profile than our default (e.g. us-core-vital-signs
    rather than us-core-observation-lab), and validating against what it claims
    is the fairer test. Falls back to our per-type default.
    """
    declared = (resource.get("meta") or {}).get("profile") or []
    for candidate in declared:
        if isinstance(candidate, str) and candidate.startswith(_BASE):
            return candidate
    return US_CORE_PROFILES.get(resource.get("resourceType", ""), "")
