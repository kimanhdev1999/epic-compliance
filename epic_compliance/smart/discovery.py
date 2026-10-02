"""SMART App Launch — .well-known/smart-configuration discovery."""
from __future__ import annotations

import httpx
from pydantic import BaseModel


REQUIRED_CAPABILITIES = [
    "launch-ehr",
    "launch-standalone",
    "client-public",
    "client-confidential-symmetric",
    "sso-openid-connect",
    "context-banner",
    "context-style",
    "context-ehr-patient",
    "permission-offline",
    "permission-patient",
    "permission-user",
]


class SmartConfiguration(BaseModel):
    authorization_endpoint: str = ""
    token_endpoint: str = ""
    jwks_uri: str = ""
    capabilities: list[str] = []
    code_challenge_methods_supported: list[str] = []
    scopes_supported: list[str] = []
    issuer: str = ""
    raw: dict = {}


# Mock fixture used in RUN_MODE=mock
MOCK_SMART_CONFIG = SmartConfiguration(
    authorization_endpoint="https://fhir.epic.com/interconnect-fhir-oauth/oauth2/authorize",
    token_endpoint="https://fhir.epic.com/interconnect-fhir-oauth/oauth2/token",
    jwks_uri="https://fhir.epic.com/interconnect-fhir-oauth/oauth2/jwks",
    capabilities=[
        "launch-ehr",
        "launch-standalone",
        "client-public",
        "client-confidential-symmetric",
        "sso-openid-connect",
        "context-banner",
        "context-style",
        "context-ehr-patient",
        "permission-offline",
        "permission-patient",
        "permission-user",
        "authorize-post",
    ],
    code_challenge_methods_supported=["S256"],
    scopes_supported=[
        "openid",
        "fhirUser",
        "offline_access",
        "launch",
        "launch/patient",
        "patient/*.read",
        "user/*.read",
    ],
    issuer="https://fhir.epic.com/interconnect-fhir-oauth/oauth2",
    raw={"mock": True, "jwks_uri": "https://fhir.epic.com/interconnect-fhir-oauth/oauth2/jwks"},
)


def fetch_smart_configuration(fhir_base_url: str, mock: bool = False) -> SmartConfiguration:
    """Fetch and parse .well-known/smart-configuration from the FHIR base URL."""
    if mock:
        return MOCK_SMART_CONFIG

    url = fhir_base_url.rstrip("/") + "/.well-known/smart-configuration"
    resp = httpx.get(url, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    return SmartConfiguration(
        authorization_endpoint=data.get("authorization_endpoint", ""),
        token_endpoint=data.get("token_endpoint", ""),
        jwks_uri=data.get("jwks_uri", ""),
        capabilities=data.get("capabilities", []),
        code_challenge_methods_supported=data.get("code_challenge_methods_supported", []),
        scopes_supported=data.get("scopes_supported", []),
        issuer=data.get("issuer", ""),
        raw=data,
    )
