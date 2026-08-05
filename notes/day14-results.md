# Day 14 — auth module: close the Day 13 gaps, add negative-path rules

Plan item: *"Write tests for the auth module. Buffer / catch-up."* Day 13 had already
built the e2e suite, so the real work was the code those tests were pointing at.

## Fixed

1. **Live mode no longer fakes a token.** `pipeline.collect_evidence()` hardcoded
   `token = MOCK_TOKEN_RESPONSE` in *both* modes, so AUTH-003 and AUTH-005 were
   structurally incapable of failing — every report claimed a pass on scopes and
   patient context without ever seeing a real token. Live mode now emits
   `{"_unavailable": TOKEN_UNAVAILABLE_REASON}` and the dependent rules return
   `needs_human`. A compliance tool that cannot fail is worse than no tool.
2. **`aud` is now emitted and mandatory.** `build_authorization_url()` omitted it;
   Epic rejects the launch without it. It is keyword-only and required, so a caller
   cannot silently forget it. The `xfail(strict=True)` marker and the `LaunchDriver`
   stopgap in `conftest.py` are both deleted.
3. **AUTH-003 no longer false-fails a conformant Epic.** It required the literal
   `patient/*.read`; Epic grants per-resource scopes (`patient/Patient.read`).
   Now accepts wildcard or per-resource read scopes. This is the token-side
   counterpart to the Day 13 discovery-side finding.

## Added — AUTH-006..010 (negative paths)

Day 13 proved these against live Epic in tests but none of it reached a report:

| Rule | Asserts | Source |
|---|---|---|
| AUTH-006 | state round-tripped and validated (CSRF) | SMART v2 §4.1.1, OAuth BCP §4.7 |
| AUTH-007 | wrong PKCE verifier is rejected | RFC 7636 §4.6 |
| AUTH-008 | authorization code is single-use | RFC 6749 §4.1.2 |
| AUTH-009 | cross-patient read is refused | (g)(10) §170.315(g)(10)(v)(A) |
| AUTH-010 | id_token verifiable against `jwks_uri` | OIDC Core §3.1.3.7 |

These read a new `auth_probe` evidence key. Each field is **tri-state**:
`True` = control proved enforced, `False` = proved broken, `None`/absent = not
tested → `needs_human`. "Not tested" must never collapse into "fine"; that
distinction is the whole point of the probe.

`validate_state()` added to `pkce.py` (constant-time compare via `hmac`).

## Tests

New `tests/test_auth_module.py` (44 tests): state validation table, the live-mode
no-fake-token regression, needs_human degradation on absent evidence, the AUTH-003
scope-shape table including Epic's real shape, tri-state probe behaviour per rule,
and token-exchange parsing (`expires_in` as a string, confidential vs public
client, HTTP error, missing fields). Plus a catalog-integrity test that fails if an
automated rule has no registered check — previously such a rule silently became
`needs_human`.

`pytest -m "not launch"` → **74 passed** (was 28).
`./scripts/verify_tool.sh` → ALL CHECKS PASSED (22 checks).
Mock CLI run → pass=13 fail=2 needs_human=3.

## Still open

- No `/callback` route in `api.py`, so the pipeline still cannot complete a live
  launch on its own — `auth_probe` is only populated by `pytest -m launch` today.
  Wiring the callback is what makes AUTH-006..010 evaluable in a normal run.
- `id_token` signature is not actually verified in code (AUTH-010 trusts the probe
  field); needs a real JWKS fetch + verify.
- Rules are still read-path only — no write-back (`DocumentReference`/`Media`).
