"""Assessment intake — turn "describe your app" answers into a test plan.

The web flow is intake -> plan -> run -> report. This module owns the first
two steps: an ``Intake`` holds what the user told us about their app, and
``build_plan`` decides which catalog rules apply to that app (each with the
reason it was included or left out) plus the config overrides the run needs.

Selection is by rule id. A catalog rule this module has no entry for is
included as a general requirement, so adding a rule file never silently drops
a check from every assessment.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Literal

from pydantic import BaseModel

from .rules_engine.catalog import Rule, load_rules


class Intake(BaseModel):
    """What the user told us about the app under assessment."""

    app_name: str = ""
    health_system: str = ""
    # Who signs in: clinicians pick the patient after login, patients are the patient.
    users: Literal["clinicians", "patients"] = "clinicians"
    platform: Literal["mobile", "web_browser", "web_server"] = "mobile"
    reads_chart: bool = True
    saves_images: bool = False
    writes_back: bool = False
    third_party_ai: bool = False

    # Security posture. "" = don't know -> the rule stays needs_human.
    tls_version: str = ""
    audit_logging: str = ""
    audit_retention_days: str = ""
    baa_status: str = ""

    # Where to run.
    run_mode: Literal["mock", "live"] = "mock"
    fhir_base_url: str = ""
    oauth_client_id: str = ""
    app_redirect_uri: str = ""
    test_patient_query: str = ""
    encounter_required: bool = False

    @classmethod
    def from_form(cls, form: dict[str, Any]) -> "Intake":
        """Build from an HTML form, where an unchecked checkbox is simply absent."""
        data = {k: v for k, v in form.items() if k in cls.model_fields}
        for name, f in cls.model_fields.items():
            if f.annotation is bool:
                data[name] = str(form.get(name, "")).lower() in ("1", "true", "on", "yes")
        return cls.model_validate(data)


USERS_LABELS = {
    "clinicians": "Clinicians at the health system",
    "patients": "Patients",
}
PLATFORM_LABELS = {
    "mobile": "Native mobile app (iOS / Android)",
    "web_browser": "Browser-only web app (no backend holds secrets)",
    "web_server": "Web app with its own backend server",
}
BAA_LABELS = {
    "all": "Signed with the health system and every vendor that touches PHI",
    "health_system_only": "Signed with the health system, not yet with every vendor",
    "none": "Not signed yet",
}

PLAN_SECTIONS = {
    "auth": "Sign-in & authorization",
    "clinician_workflow": "Clinician workflow",
    "fhir_conformance": "Reading chart data",
    "write_back": "Writing to the chart",
    "security": "Security & privacy",
}


@dataclass
class PlannedCheck:
    rule: Rule
    included: bool
    reason: str


@dataclass
class Plan:
    intake: Intake
    launch_profile: str
    checks: list[PlannedCheck] = field(default_factory=list)

    @property
    def included(self) -> list[PlannedCheck]:
        return [c for c in self.checks if c.included]

    @property
    def excluded(self) -> list[PlannedCheck]:
        return [c for c in self.checks if not c.included]

    @property
    def rule_ids(self) -> set[str]:
        return {c.rule.id for c in self.included}

    def sections(self) -> list[dict[str, Any]]:
        """Included checks grouped by catalog category, in PLAN_SECTIONS order."""
        groups: dict[str, list[PlannedCheck]] = {}
        for c in self.included:
            groups.setdefault(c.rule.category, []).append(c)
        order = list(PLAN_SECTIONS)
        return [
            {"key": k, "label": PLAN_SECTIONS.get(k, k.replace("_", " ").title()), "checks": v}
            for k, v in sorted(groups.items(), key=lambda kv: order.index(kv[0]) if kv[0] in order else 99)
        ]

    def config_overrides(self) -> dict[str, str]:
        """Per-run overrides for config_with_overrides()."""
        i = self.intake
        relationship = ""
        if i.health_system:
            relationship = f"Business associate of {i.health_system}"
        if i.third_party_ai:
            relationship = (relationship or "Business associate of the health system") + (
                "; PHI (including any captured images) is sent to a third-party AI / cloud service"
            )
        baa = {
            "all": "yes — with the health system and every PHI subprocessor",
            "health_system_only": "with the health system only; not yet with every PHI subprocessor",
            "none": "no",
        }.get(i.baa_status, "")
        return {
            "run_mode": i.run_mode,
            "launch_profile": self.launch_profile,
            "fhir_base_url": i.fhir_base_url,
            "oauth_client_id": i.oauth_client_id,
            "app_redirect_uri": i.app_redirect_uri,
            "test_patient_query": i.test_patient_query,
            "encounter_required": "true" if i.encounter_required else "false",
            "app_client_type": "confidential" if i.platform == "web_server" else "public",
            "transport_tls_version": i.tls_version,
            "audit_logging_enabled": i.audit_logging,
            "audit_log_retention_days": i.audit_retention_days,
            "baa_in_place": baa,
            "covered_entity_relationship": relationship,
        }

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe summary stored with the run, so the report can show its scope."""
        return {
            "intake": self.intake.model_dump(),
            "launch_profile": self.launch_profile,
            "included": [{"rule_id": c.rule.id, "reason": c.reason} for c in self.included],
            "excluded": [
                {"rule_id": c.rule.id, "reason": c.reason, "description": c.rule.description}
                for c in self.excluded
            ],
        }


# --------------------------------------------------------------------------- #
# Selection: rule_id -> fn(intake) -> (included, reason)
# --------------------------------------------------------------------------- #
Decision = tuple[bool, str]


def _always(reason: str) -> Callable[[Intake], Decision]:
    return lambda i: (True, reason)


def _when(pred: Callable[[Intake], bool], yes: str, no: str) -> Callable[[Intake], Decision]:
    return lambda i: (True, yes) if pred(i) else (False, no)


def _patients(i: Intake) -> bool:
    return i.users == "patients"


def _clinicians(i: Intake) -> bool:
    return i.users == "clinicians"


_EPIC_LOGIN = "Every Epic app signs in through SMART on FHIR, so Epic's login has to work for yours."
_NOT_READING = "You said the app doesn't read chart data."

SELECTORS: dict[str, Callable[[Intake], Decision]] = {
    "AUTH-001": _always(_EPIC_LOGIN),
    "AUTH-002": _always("Epic requires PKCE for public clients, and recommends it for every app."),
    "AUTH-003": _always("The app only works if Epic actually grants the scopes it asks for."),
    "AUTH-004": _always(_EPIC_LOGIN),
    "AUTH-005": _always("The app needs to know who signed in (and which patient, for a patient app)."),
    "AUTH-006": _always("A missing or unchecked state value lets an attacker hijack the login."),
    "AUTH-007": _always("Proves a stolen authorization code is useless without the PKCE verifier."),
    "AUTH-008": _always("Proves an authorization code can't be replayed."),
    "AUTH-009": _when(_patients, "A patient app must never see another patient's record.",
                      "Clinician apps see many patients by design; covered by the patient-selection checks instead."),
    "AUTH-010": _always("The app has to verify the signed-in user's identity token."),

    "FHIR-001": _when(lambda i: i.reads_chart, "You read patient demographics.", _NOT_READING),
    "FHIR-002": _when(lambda i: i.reads_chart, "You read clinical observations.", _NOT_READING),
    "FHIR-003": _when(lambda i: i.reads_chart, "You read the problem list (Conditions).", _NOT_READING),
    "FHIR-004": _when(lambda i: i.reads_chart, "Profile tags tell your parser what shape to expect.", _NOT_READING),
    "FHIR-005": _when(lambda i: i.reads_chart, "Confirms the data classes your app may rely on are reachable.", _NOT_READING),

    "WRITE-001": _when(lambda i: _patients(i) and i.writes_back,
                       "You write results back into the chart.", "The app doesn't write results back as a patient app."),
    "WRITE-002": _when(lambda i: _patients(i) and i.writes_back,
                       "You write results back into the chart.", "The app doesn't write results back as a patient app."),
    "WRITE-003": _when(lambda i: _patients(i) and i.saves_images,
                       "You save images into the chart.", "The app doesn't save images as a patient app."),
    "WRITE-004": _when(lambda i: _patients(i) and (i.writes_back or i.saves_images),
                       "Write scopes must be granted, not just requested.", "The app doesn't write to the chart as a patient app."),

    "CLIN-001": _when(_clinicians, "Clinicians search for the patient after signing in — the top wrong-patient risk.",
                      "Patient apps get the patient from the login."),
    "CLIN-002": _when(_clinicians, "The clinician must see enough to confirm it's the right patient.",
                      "Patient apps get the patient from the login."),
    "CLIN-003": _when(_clinicians, "The app needs to know which clinician is signed in.", "Only applies to clinician apps."),
    "CLIN-004": _when(lambda i: _clinicians(i) and (i.saves_images or i.writes_back),
                      "Photos and notes are usually filed against a visit.", "The app doesn't write to the chart."),
    "CLIN-005": _when(lambda i: _clinicians(i) and i.saves_images,
                      "You save photos into the chart.", "The app doesn't save images."),
    "CLIN-006": _when(lambda i: _clinicians(i) and i.saves_images,
                      "A saved photo must be readable back by the care team.", "The app doesn't save images."),
    "CLIN-007": _when(lambda i: _clinicians(i) and (i.saves_images or i.writes_back),
                      "Everything you write must land on the patient the clinician confirmed.", "The app doesn't write to the chart."),
    "CLIN-008": _when(lambda i: _clinicians(i) and i.reads_chart,
                      "You read the problem list, medications and allergies.", _NOT_READING),
    "CLIN-009": _when(lambda i: _clinicians(i) and i.writes_back,
                      "You write notes or results back into the chart.", "The app doesn't write notes back."),
    "CLIN-010": _when(lambda i: _clinicians(i) and i.writes_back,
                      "Clinicians need to find prior notes the app wrote.", "The app doesn't write notes back."),
    "CLIN-011": _when(lambda i: _clinicians(i) and i.platform == "mobile",
                      "A mobile app can't keep a secret, so it must be a public client.", "Only applies to mobile clinician apps."),
    "CLIN-012": _when(lambda i: _clinicians(i) and i.platform == "mobile",
                      "A mobile app's redirect must be a deep link it owns, never plain http.", "Only applies to mobile clinician apps."),

    "SEC-001": _always("Health systems require TLS 1.2+ for anything carrying PHI."),
    "SEC-002": _always("HIPAA requires an audit trail of PHI access; every security review asks."),
    "SEC-003": lambda i: (True, "You send PHI to a third-party AI / cloud service — each one needs a BAA."
                          if i.third_party_ai else "Required before the health system will share PHI with you."),
}


def build_plan(intake: Intake) -> Plan:
    launch_profile = "clinician_standalone" if intake.users == "clinicians" else "patient_launch"
    plan = Plan(intake=intake, launch_profile=launch_profile)
    for rule in load_rules():
        selector = SELECTORS.get(rule.id)
        if selector is None:
            included, reason = True, "General requirement for every Epic app."
        else:
            included, reason = selector(intake)
        # The engine only runs rules for the launch profile; keep the plan honest.
        if included and not rule.applies_to(launch_profile):
            included, reason = False, "Doesn't apply to this kind of app."
        plan.checks.append(PlannedCheck(rule=rule, included=included, reason=reason))
    return plan
