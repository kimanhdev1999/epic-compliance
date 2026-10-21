"""Config ingest: typed app configuration from env / .env file."""
from __future__ import annotations

from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    fhir_base_url: str = Field(
        default="https://fhir.epic.com/interconnect-fhir-oauth/api/FHIR/R4",
        alias="FHIR_BASE_URL",
    )
    smart_launch_url: str = Field(
        default="https://fhir.epic.com/interconnect-fhir-oauth/oauth2/authorize",
        alias="SMART_LAUNCH_URL",
    )
    smart_token_url: str = Field(
        default="https://fhir.epic.com/interconnect-fhir-oauth/oauth2/token",
        alias="SMART_TOKEN_URL",
    )
    oauth_client_id: str = Field(default="mock-client-id", alias="OAUTH_CLIENT_ID")
    oauth_client_secret: str = Field(default="", alias="OAUTH_CLIENT_SECRET")
    oauth_redirect_uri: str = Field(
        default="http://localhost:8000/callback", alias="OAUTH_REDIRECT_URI"
    )
    anthropic_api_key: str = Field(default="", alias="ANTHROPIC_API_KEY")
    run_mode: Literal["mock", "live"] = Field(default="mock", alias="RUN_MODE")
    onc_test_kit_url: str = Field(
        default="http://localhost:8080", alias="ONC_TEST_KIT_URL"
    )

    # --- HL7 FHIR validator (Day 15) ---------------------------------------
    # auto: HTTP service, then validator_cli.jar, then none (needs_human).
    validator_mode: Literal["auto", "service", "java", "off"] = Field(
        default="auto", alias="VALIDATOR_MODE"
    )
    validator_service_url: str = Field(
        default="http://localhost:3500", alias="VALIDATOR_SERVICE_URL"
    )
    validator_jar_path: str = Field(default="", alias="VALIDATOR_JAR_PATH")
    validator_timeout_s: float = Field(default=60.0, alias="VALIDATOR_TIMEOUT_S")
    # Start the validator container (podman, else docker) with the app when
    # VALIDATOR_SERVICE_URL is local and nothing answers there.
    validator_autostart: bool = Field(default=True, alias="VALIDATOR_AUTOSTART")

    # --- Launch profile (ADR-010) ------------------------------------------
    # patient_launch: patient-context token (patient/* scopes, `patient` in
    #   the token response) and the AI write-back of ADR-007.
    # clinician_standalone: a clinician signs in through Epic with no patient
    #   selected (user/* scopes), then searches for and confirms the patient.
    #   Runs rules/clinician_workflow.json instead of the patient-only rules.
    launch_profile: Literal["patient_launch", "clinician_standalone"] = Field(
        default="patient_launch", alias="LAUNCH_PROFILE"
    )
    # Search used to find the sandbox test patient, as a FHIR query string,
    # e.g. "family=Smith&given=Jane&birthdate=1980-01-31", or an identifier
    # search in the form Epic's Patient.Search docs give for MRNs. Take the
    # test patient from Epic's sandbox test-data page. Blank -> needs_human.
    test_patient_query: str = Field(default="", alias="TEST_PATIENT_QUERY")
    # Resource the organization's Epic build files clinical photos as.
    image_write_resource: Literal["Media", "DocumentReference"] = Field(
        default="Media", alias="IMAGE_WRITE_RESOURCE"
    )
    # True when the organization requires photos and notes to be filed
    # against an Encounter; a patient with no encounter then fails CLIN-004.
    encounter_required: bool = Field(default=False, alias="ENCOUNTER_REQUIRED")
    # How the app under test is registered with Epic, and where Epic sends
    # the clinician back after login. Blank = not attested -> needs_human.
    app_client_type: Literal["", "public", "confidential"] = Field(
        default="", alias="APP_CLIENT_TYPE"
    )
    app_redirect_uri: str = Field(default="", alias="APP_REDIRECT_URI")

    # --- App-under-test config (SEC-00x LLM rules need these, not Epic's) --
    # These describe the app being compliance-tested, not this tool itself.
    # Unset (default "") means "not attested" -> the LLM evaluator correctly
    # sees no evidence and returns needs_human rather than guessing a pass.
    transport_tls_version: str = Field(default="", alias="TRANSPORT_TLS_VERSION")
    audit_logging_enabled: str = Field(default="", alias="AUDIT_LOGGING_ENABLED")
    audit_log_retention_days: str = Field(default="", alias="AUDIT_LOG_RETENTION_DAYS")
    baa_in_place: str = Field(default="", alias="BAA_IN_PLACE")
    covered_entity_relationship: str = Field(default="", alias="COVERED_ENTITY_RELATIONSHIP")


_config: AppConfig | None = None


def get_config() -> AppConfig:
    global _config
    if _config is None:
        _config = AppConfig()
    return _config


# Fields the web UI is allowed to override per-run. Excludes secrets that should
# only ever come from the environment is intentional EXCEPT anthropic_api_key,
# which the user may want to supply from the browser for a one-off live run.
OVERRIDABLE_FIELDS = {
    "fhir_base_url",
    "smart_launch_url",
    "smart_token_url",
    "oauth_client_id",
    "oauth_redirect_uri",
    "run_mode",
    "onc_test_kit_url",
    "anthropic_api_key",
    "transport_tls_version",
    "audit_logging_enabled",
    "audit_log_retention_days",
    "baa_in_place",
    "covered_entity_relationship",
    "launch_profile",
    "test_patient_query",
    "image_write_resource",
    "encounter_required",
    "app_client_type",
    "app_redirect_uri",
}


def config_with_overrides(overrides: dict[str, str]) -> AppConfig:
    """Return a config derived from the base config with safe per-run overrides.

    Empty / missing values are ignored so a blank form field falls back to the
    environment-configured default rather than wiping it.
    """
    base = get_config()
    clean = {
        k: v
        for k, v in overrides.items()
        if k in OVERRIDABLE_FIELDS and v not in (None, "")
    }
    # model_copy skips validation, so a form's "false" would stay a truthy str.
    if "encounter_required" in clean:
        clean["encounter_required"] = str(clean["encounter_required"]).lower() in ("1", "true", "yes", "on")
    return base.model_copy(update=clean)
