"""PKCE (Proof Key for Code Exchange) implementation for OAuth2 authorization-code flow."""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import urllib.parse
from dataclasses import dataclass


@dataclass
class PKCEChallenge:
    code_verifier: str
    code_challenge: str
    code_challenge_method: str = "S256"


def generate_pkce() -> PKCEChallenge:
    """Generate a PKCE code_verifier and code_challenge (S256 method)."""
    code_verifier = base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode("ascii")
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return PKCEChallenge(code_verifier=code_verifier, code_challenge=code_challenge)


def build_authorization_url(
    authorization_endpoint: str,
    client_id: str,
    redirect_uri: str,
    scopes: list[str],
    pkce: PKCEChallenge,
    *,
    aud: str,
    state: str | None = None,
    launch: str | None = None,
) -> str:
    """Construct the OAuth2 authorization URL with PKCE parameters.

    ``aud`` is keyword-only and required: SMART App Launch mandates
    ``aud={fhir_base_url}`` and Epic rejects the launch without it. Making it
    required means a caller cannot silently omit it (the Day 13 bug).
    """
    if not aud:
        raise ValueError("aud is required — SMART App Launch mandates aud={fhir_base_url}")

    params: dict[str, str] = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": " ".join(scopes),
        "aud": aud,
        "code_challenge": pkce.code_challenge,
        "code_challenge_method": pkce.code_challenge_method,
    }
    if state:
        params["state"] = state
    if launch:
        params["launch"] = launch
    return authorization_endpoint + "?" + urllib.parse.urlencode(params)


@dataclass
class TokenResponse:
    access_token: str
    token_type: str
    expires_in: int
    scope: str
    patient: str = ""
    id_token: str = ""
    refresh_token: str = ""
    raw: dict = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.raw is None:
            self.raw = {}


# STUB: token exchange requires a browser redirect to get the authorization code.
# In live mode this would be driven by the FastAPI /callback endpoint.
# The function below is the interface — call it with a real code after redirect.
def exchange_code_for_token(
    token_endpoint: str,
    code: str,
    client_id: str,
    redirect_uri: str,
    code_verifier: str,
    client_secret: str = "",
) -> TokenResponse:
    """Exchange authorization code for access token (LIVE mode only — needs real code)."""
    import httpx

    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "code_verifier": code_verifier,
    }
    if client_secret:
        data["client_secret"] = client_secret

    resp = httpx.post(token_endpoint, data=data, timeout=15)
    resp.raise_for_status()
    raw = resp.json()
    return TokenResponse(
        access_token=raw.get("access_token", ""),
        token_type=raw.get("token_type", "Bearer"),
        expires_in=int(raw.get("expires_in", 3600)),
        scope=raw.get("scope", ""),
        patient=raw.get("patient", ""),
        id_token=raw.get("id_token", ""),
        refresh_token=raw.get("refresh_token", ""),
        raw=raw,
    )


def validate_state(sent_state: str, returned_state: str) -> bool:
    """Constant-time comparison of the OAuth ``state`` round-trip (CSRF control).

    An app that ignores the returned state accepts an authorization code minted
    for a different session — this is the check AUTH-006 asserts was performed.
    """
    if not sent_state or not returned_state:
        return False
    return hmac.compare_digest(sent_state, returned_state)


# Result of the interactive negative-path probes. Populated by a real launch;
# every field is tri-state: True (proved), False (proved broken), None (not run).
AUTH_PROBE_FIELDS = (
    "state_validated",
    "wrong_verifier_rejected",
    "code_single_use",
    "cross_patient_refused",
    "id_token_verifiable",
)

# Mock fixture: a fully conformant server, so mock runs exercise the pass path.
MOCK_AUTH_PROBE: dict[str, bool] = {f: True for f in AUTH_PROBE_FIELDS}


MOCK_TOKEN_RESPONSE = TokenResponse(
    access_token="mock-access-token-abc123",
    token_type="Bearer",
    expires_in=3600,
    scope="launch/patient patient/*.read openid fhirUser offline_access",
    patient="mock-patient-id",
    id_token="mock-id-token",
    refresh_token="mock-refresh-token",
    raw={"mock": True},
)
