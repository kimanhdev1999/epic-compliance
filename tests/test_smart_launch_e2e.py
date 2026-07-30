"""Day 13 — end-to-end SMART App Launch against the Epic sandbox.

Three tiers, by what each needs to run:

  (a) offline      — PKCE maths + authorization-URL construction. Always run.
  (b) -m live      — real HTTPS to Epic's public discovery doc. Needs network,
                     no credentials. Run with: pytest -m live
  (c) -m launch    — the full authorization-code + PKCE flow. Needs a client_id
                     registered at fhir.epic.com AND a human at a browser, so it
                     is skipped unless EPIC_SANDBOX_CLIENT_ID is set and the
                     operator opts in with: pytest -m launch --launch-interactive

The `xfail(strict=True)` markers below pin known Day-13 gaps. When the gap is
closed the test XPASSes, strict turns that into a failure, and you are forced to
delete the marker — so the suite can never quietly forget a fixed bug.
"""
from __future__ import annotations

import base64
import hashlib
import os
import urllib.parse

import httpx
import pytest

from epic_compliance.smart.discovery import (
    REQUIRED_CAPABILITIES,
    fetch_smart_configuration,
)
from epic_compliance.smart.pkce import build_authorization_url, generate_pkce

EPIC_FHIR_BASE = "https://fhir.epic.com/interconnect-fhir-oauth/api/FHIR/R4"

# Scopes a patient-facing app asks Epic for. Note these are *requested*; Epic
# grants a subset and does not enumerate resource scopes in its discovery doc.
REQUESTED_SCOPES = [
    "openid",
    "fhirUser",
    "launch/patient",
    "patient/Patient.read",
    "patient/Observation.read",
    "offline_access",
]

CLIENT_ID = os.environ.get("EPIC_SANDBOX_CLIENT_ID", "")
REDIRECT_URI = os.environ.get("EPIC_SANDBOX_REDIRECT_URI", "http://localhost:8000/callback")

needs_client = pytest.mark.skipif(
    not CLIENT_ID,
    reason="set EPIC_SANDBOX_CLIENT_ID (register a non-prod app at fhir.epic.com)",
)


# ─── (a) offline: PKCE correctness ───────────────────────────────────────────


def test_pkce_challenge_matches_rfc7636_s256():
    """code_challenge must be BASE64URL(SHA256(verifier)), unpadded."""
    pkce = generate_pkce()
    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(pkce.code_verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    assert pkce.code_challenge == expected
    assert pkce.code_challenge_method == "S256"
    assert "=" not in pkce.code_challenge, "padding must be stripped"
    # RFC 7636 §4.1 length bounds
    assert 43 <= len(pkce.code_verifier) <= 128


def test_pkce_verifier_is_unique_per_launch():
    """A reused verifier would let a stolen code be replayed."""
    assert len({generate_pkce().code_verifier for _ in range(50)}) == 50


# ─── (a) offline: authorization URL shape ────────────────────────────────────


def _authz_params(**kw) -> dict[str, str]:
    url = build_authorization_url(
        authorization_endpoint="https://example.org/authorize",
        client_id="test-client",
        redirect_uri=REDIRECT_URI,
        scopes=REQUESTED_SCOPES,
        pkce=generate_pkce(),
        state="xyz123",
        **kw,
    )
    return dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))


def test_authorization_url_has_required_oauth_params():
    p = _authz_params()
    assert p["response_type"] == "code"
    assert p["client_id"] == "test-client"
    assert p["redirect_uri"] == REDIRECT_URI
    assert p["code_challenge_method"] == "S256"
    assert p["code_challenge"]
    assert p["state"] == "xyz123", "missing state = CSRF exposure"
    assert set(p["scope"].split()) == set(REQUESTED_SCOPES)


def test_authorization_url_never_leaks_the_verifier():
    """Only the challenge goes on the wire; the verifier stays client-side."""
    assert "code_verifier" not in _authz_params()


@pytest.mark.xfail(
    strict=True,
    reason="Day 13: build_authorization_url() omits `aud`. Epic rejects the "
    "launch without it. Fix in epic_compliance/smart/pkce.py, then drop this marker.",
)
def test_authorization_url_includes_aud():
    """SMART App Launch requires aud={fhir_base_url}; Epic enforces it."""
    p = _authz_params()
    assert p["aud"].rstrip("/") == EPIC_FHIR_BASE.rstrip("/")


# ─── (b) live: Epic's real discovery document ────────────────────────────────


@pytest.fixture(scope="module")
def live_smart_config():
    try:
        return fetch_smart_configuration(EPIC_FHIR_BASE, mock=False)
    except (httpx.HTTPError, httpx.TimeoutException) as exc:
        pytest.skip(f"Epic sandbox unreachable: {exc}")


@pytest.mark.live
def test_live_discovery_returns_endpoints(live_smart_config):
    cfg = live_smart_config
    assert cfg.authorization_endpoint.startswith("https://"), "TLS required"
    assert cfg.token_endpoint.startswith("https://")
    assert cfg.issuer


@pytest.mark.live
def test_live_discovery_advertises_pkce_s256(live_smart_config):
    """AUTH-002. Without S256 a public mobile client cannot launch safely."""
    assert "S256" in live_smart_config.code_challenge_methods_supported


@pytest.mark.live
def test_live_discovery_advertises_required_capabilities(live_smart_config):
    """AUTH-001 / AUTH-004."""
    missing = set(REQUIRED_CAPABILITIES) - set(live_smart_config.capabilities)
    assert not missing, f"Epic is missing capabilities: {sorted(missing)}"


@pytest.mark.live
def test_live_discovery_exposes_jwks_for_id_token_verification(live_smart_config):
    """Without jwks_uri you cannot verify the id_token signature, so `fhirUser`
    and `sub` are unauthenticated claims and must not be trusted."""
    assert live_smart_config.raw.get("jwks_uri", "").startswith("https://")


@pytest.mark.live
def test_live_scopes_supported_does_not_enumerate_resource_scopes(live_smart_config):
    """Documents an Epic quirk that will otherwise cause false-positive findings.

    Epic advertises only coarse scopes (openid, fhirUser, launch, profile). It
    does NOT list `launch/patient`, `patient/*.read`, or `offline_access` even
    though it honours them. So a rule asserting
    `"launch/patient" in scopes_supported` would fail against a compliant Epic.
    Resource-scope conformance must be judged from the *token response*, not
    from discovery.
    """
    advertised = set(live_smart_config.scopes_supported)
    assert "launch/patient" not in advertised
    assert not any(s.startswith("patient/") for s in advertised)


@pytest.mark.live
def test_mock_fixture_has_not_drifted_from_live_epic(live_smart_config):
    """MOCK_SMART_CONFIG is what every mock-mode finding is graded against. If it
    drifts from reality, mock runs pass while live runs fail."""
    from epic_compliance.smart.discovery import MOCK_SMART_CONFIG

    assert MOCK_SMART_CONFIG.authorization_endpoint == live_smart_config.authorization_endpoint
    assert MOCK_SMART_CONFIG.token_endpoint == live_smart_config.token_endpoint
    assert MOCK_SMART_CONFIG.issuer == live_smart_config.issuer, (
        "mock issuer is stale — update MOCK_SMART_CONFIG in smart/discovery.py"
    )


# ─── (c) launch: the full interactive flow ───────────────────────────────────


@pytest.mark.launch
@needs_client
def test_full_standalone_launch(launch_driver, live_smart_config):
    """The Day 13 deliverable: real login, real token, real patient read.

    `launch_driver` (see conftest.py) opens the browser, serves the redirect_uri,
    and returns the captured authorization code.
    """
    pkce = generate_pkce()
    state = base64.urlsafe_b64encode(os.urandom(16)).rstrip(b"=").decode()

    code = launch_driver.authorize(
        authorization_endpoint=live_smart_config.authorization_endpoint,
        client_id=CLIENT_ID,
        redirect_uri=REDIRECT_URI,
        scopes=REQUESTED_SCOPES,
        pkce=pkce,
        state=state,
        aud=EPIC_FHIR_BASE,
    )
    assert launch_driver.returned_state == state, "state mismatch — possible CSRF"

    tok = launch_driver.exchange(code=code, pkce=pkce)

    # Token shape
    assert tok.access_token
    assert tok.token_type.lower() == "bearer"
    assert tok.expires_in > 0

    # Granted scopes must be a SUBSET of requested — never a superset.
    granted = set(tok.scope.split())
    assert granted <= set(REQUESTED_SCOPES), (
        f"server granted un-requested scopes: {granted - set(REQUESTED_SCOPES)}"
    )

    # Launch context: the app must learn the patient from the token, not choose it.
    assert tok.patient, "no patient context despite launch/patient scope"
    assert tok.id_token, "no id_token despite openid scope"

    # The actual point of all this: read that patient.
    r = httpx.get(
        f"{EPIC_FHIR_BASE}/Patient/{tok.patient}",
        headers={
            "Authorization": f"Bearer {tok.access_token}",
            "Accept": "application/fhir+json",
        },
        timeout=30,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["resourceType"] == "Patient"
    assert body["id"] == tok.patient


@pytest.mark.launch
@needs_client
def test_pkce_is_enforced_wrong_verifier_rejected(launch_driver, live_smart_config):
    """NEGATIVE CASE — the one that actually proves security.

    A server that accepts any verifier has PKCE in name only: an attacker who
    intercepts the redirect can redeem the code themselves.
    """
    pkce = generate_pkce()
    code = launch_driver.authorize(
        authorization_endpoint=live_smart_config.authorization_endpoint,
        client_id=CLIENT_ID,
        redirect_uri=REDIRECT_URI,
        scopes=REQUESTED_SCOPES,
        pkce=pkce,
        state="s",
        aud=EPIC_FHIR_BASE,
    )
    with pytest.raises(httpx.HTTPStatusError) as exc:
        launch_driver.exchange(code=code, pkce=generate_pkce())  # deliberately wrong
    assert exc.value.response.status_code == 400


@pytest.mark.launch
@needs_client
def test_authorization_code_is_single_use(launch_driver, live_smart_config):
    """A replayable code means a leaked redirect URL grants access forever."""
    pkce = generate_pkce()
    code = launch_driver.authorize(
        authorization_endpoint=live_smart_config.authorization_endpoint,
        client_id=CLIENT_ID,
        redirect_uri=REDIRECT_URI,
        scopes=REQUESTED_SCOPES,
        pkce=pkce,
        state="s",
        aud=EPIC_FHIR_BASE,
    )
    assert launch_driver.exchange(code=code, pkce=pkce).access_token
    with pytest.raises(httpx.HTTPStatusError):
        launch_driver.exchange(code=code, pkce=pkce)  # replay


@pytest.mark.launch
@needs_client
def test_out_of_scope_resource_is_refused(launch_driver, live_smart_config):
    """Scope must actually restrict. Condition.read was never requested."""
    pkce = generate_pkce()
    code = launch_driver.authorize(
        authorization_endpoint=live_smart_config.authorization_endpoint,
        client_id=CLIENT_ID,
        redirect_uri=REDIRECT_URI,
        scopes=["openid", "fhirUser", "launch/patient", "patient/Patient.read"],
        pkce=pkce,
        state="s",
        aud=EPIC_FHIR_BASE,
    )
    tok = launch_driver.exchange(code=code, pkce=pkce)
    r = httpx.get(
        f"{EPIC_FHIR_BASE}/Condition",
        params={"patient": tok.patient},
        headers={"Authorization": f"Bearer {tok.access_token}"},
        timeout=30,
    )
    assert r.status_code in (401, 403), f"expected refusal, got {r.status_code}"


@pytest.mark.launch
@needs_client
def test_cross_patient_access_is_refused(launch_driver, live_smart_config):
    """The core HIPAA boundary: a patient-scoped token reaches ONE patient."""
    pkce = generate_pkce()
    code = launch_driver.authorize(
        authorization_endpoint=live_smart_config.authorization_endpoint,
        client_id=CLIENT_ID,
        redirect_uri=REDIRECT_URI,
        scopes=REQUESTED_SCOPES,
        pkce=pkce,
        state="s",
        aud=EPIC_FHIR_BASE,
    )
    tok = launch_driver.exchange(code=code, pkce=pkce)
    other = os.environ.get("EPIC_SANDBOX_OTHER_PATIENT_ID", "")
    if not other or other == tok.patient:
        pytest.skip("set EPIC_SANDBOX_OTHER_PATIENT_ID to a different sandbox patient")
    r = httpx.get(
        f"{EPIC_FHIR_BASE}/Patient/{other}",
        headers={"Authorization": f"Bearer {tok.access_token}"},
        timeout=30,
    )
    assert r.status_code in (401, 403, 404), f"LEAK: read another patient ({r.status_code})"


@pytest.mark.launch
@needs_client
def test_expired_or_garbage_token_is_refused():
    """Sanity check that the endpoint is not simply open."""
    r = httpx.get(
        f"{EPIC_FHIR_BASE}/Patient/anything",
        headers={"Authorization": "Bearer not-a-real-token"},
        timeout=30,
    )
    assert r.status_code in (401, 403)
