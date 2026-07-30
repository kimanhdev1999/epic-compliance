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
    return base.model_copy(update=clean)
