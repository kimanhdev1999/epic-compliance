# Day 13 — Full SMART launch end-to-end against sandbox

**Status: PARTIAL — plan left at `[ ]`.** Test harness + live discovery verified.
The interactive launch itself is blocked on Epic client registration.

## Blocker

No `.env` and no registered sandbox app, so `OAUTH_CLIENT_ID` is unset. The
authorization-code flow cannot run without a `client_id` from fhir.epic.com
(non-prod app registration). Everything not requiring credentials was completed
and verified live.

## What was built

- `tests/test_smart_launch_e2e.py` — 17 tests in three tiers: offline PKCE/URL,
  `-m live` (real Epic discovery, no creds), `-m launch` (interactive, needs creds).
- `tests/conftest.py` — `LaunchDriver` fixture: builds the authorize URL, opens a
  browser, serves `redirect_uri` on a one-shot HTTP server, captures `code`/`state`,
  redeems the token. Gated behind `--launch-interactive`.
- `pyproject.toml` — registered `live` and `launch` markers.

## Verified live against Epic (2026-07-30)

`GET {base}/.well-known/smart-configuration` → **HTTP 200**.

- `code_challenge_methods_supported: ["S256"]` → AUTH-002 passes live.
- All 11 `REQUIRED_CAPABILITIES` present → AUTH-001 / AUTH-004 pass live.
- `jwks_uri` present → id_token signature is verifiable.

## Bug found and fixed

`MOCK_SMART_CONFIG.issuer` was `.../interconnect-fhir-oauth`; Epic actually returns
`.../interconnect-fhir-oauth/oauth2`. Fixed in `smart/discovery.py`. Caught by
`test_mock_fixture_has_not_drifted_from_live_epic` — keep that test, it is the only
thing preventing mock-vs-live divergence.

## Epic quirk — guard against a false positive

Epic's `scopes_supported` advertises only `openid`, `fhirUser`, `launch`, `profile`,
`epic.scanning.dmsusername`. It does **not** list `launch/patient`, `patient/*.read`,
or `offline_access` even though it honours them. Any rule asserting
`"launch/patient" in scopes_supported` will report a false failure against a fully
compliant Epic. **Resource-scope conformance must be judged from the token response,
not from discovery.** Pinned by `test_live_scopes_supported_does_not_enumerate_resource_scopes`.

## Still open (the actual Day 13 work)

1. `pipeline.py:20` — `token = MOCK_TOKEN_RESPONSE` is hardcoded in **both** mock and
   live mode. AUTH-003 and AUTH-005 therefore cannot currently fail.
2. `smart/pkce.py` — `build_authorization_url()` omits `aud`; Epic rejects the launch
   without it. Pinned by `test_authorization_url_includes_aud` (`xfail(strict=True)` —
   delete the marker when fixed). `LaunchDriver` appends `aud` itself as a stopgap.
3. `api.py` — no `/callback` route, despite `config.py:31` declaring one.
4. No live-mode guard: a live run that silently falls back to a mock reports `pass`.
   Should be `needs_human`.
5. Negative-case rules (wrong verifier, replayed code, cross-patient) exist as tests
   but not yet as catalog rules in `rules/auth.json`.

## Test results

`pytest -m "not launch"` → **28 passed, 1 xfailed** (was 18 before).
`./scripts/verify_tool.sh` → ALL CHECKS PASSED (8 checks).
