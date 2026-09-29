# Write-back ("direction B") — /callback route, WRITE-00x rules, outbound validation

Task: wire the SMART `/callback` route end-to-end, add write-back rules for
the push-model architecture (photo → AI diagnosis → POST into Epic as
DiagnosticReport/Observation/Media), extend the HL7 validator to check
outgoing payloads, and keep mock mode green. See notes/decisions.md ADR-007
for why write-back-only (no server-hosted FHIR API) was chosen.

## What was built

**1. `/callback` route (`epic_compliance/api.py`)**
- `GET /launch` — discovers Epic's SMART endpoints live, generates a PKCE
  challenge + random `state`, stashes both in an in-process `_pending_launches`
  dict keyed by `state`, redirects the browser to Epic's `authorization_endpoint`.
- `GET /callback` — pops the pending launch by `state` (a miss = rejected,
  proving single-use since it's `.pop()`), exchanges the code for a token via
  the existing `exchange_code_for_token()`, then calls the new
  `pipeline.set_live_token()` so the next live pipeline run uses a real token.
  Errors (Epic `error=`, no `code`, unknown `state`, exchange failure) all
  render `_callback_result.html` with 200, never a 500.
- New template `epic_compliance/templates/_callback_result.html`. Dashboard
  links to `/launch` with a note that live mode needs a completed launch first.

**2. Live-token holder (`epic_compliance/pipeline.py`)**
- Module-level `_live_token_holder` + `set_live_token()` / `get_live_token()` /
  `get_live_auth_probe()` / `clear_live_token()`. Single-tenant, in-process —
  intentionally not a DB table or session store (matches config.py's
  single-tenant scope; multi-org state was explicitly out of scope for this task).
- `collect_evidence()` rewritten: in live mode it now checks this holder
  instead of unconditionally marking `token_response`/`auth_probe` unavailable.
  If `/callback` has populated it, live evidence collection proceeds for real
  (real token, real patient id, real write-back attempts); if not, the
  Day-14 invariant is preserved — everything token-dependent stays
  `{"_unavailable": ...}` → `needs_human`, never a fabricated pass.
  **Only `state_validated` is set from a real `/callback` launch** —
  `wrong_verifier_rejected` / `code_single_use` / `cross_patient_refused` /
  `id_token_verifiable` stay unset (`needs_human`) because a normal launch
  never deliberately misbehaves; those five stay proven only by
  `pytest -m launch` in `tests/test_smart_launch_e2e.py`, same as Day 14 left them.

**3. Write-back resource builders (`epic_compliance/fhir/writeback.py`)**
`build_observation()`, `build_diagnostic_report()`, `build_media()` — construct
the three FHIR resources this app pushes into Epic after an AI diagnosis.
`WRITE_PROFILES` maps each to a US Core 3.1.1 profile canonical where one
exists; **Media has none in US Core 3.1.1**, so it validates against base R4
only (documented in the dict, not silently dropped).

**4. Outbound write client (`epic_compliance/fhir/client.py`)**
`FhirClient.post_resource()` — the only new capability added to the FHIR
client: an outbound POST (this app as a write *client*), not a listener. Mock
mode fabricates a 201 with a synthetic id so the pipeline runs offline; live
mode POSTs for real and returns whatever status Epic gives back (a 403 is a
valid result, not an exception — only a network failure is).

**5. Write-back evidence collection (`pipeline.collect_writeback_evidence()`)**
Builds Observation → validates it (`get_validator(config).validate(...)`,
same backend/tri-state as the read path in `epic_compliance/validator/`) →
POSTs it → builds DiagnosticReport referencing the Observation's *returned* id
→ validates → POSTs → builds Media → validates → POSTs. Returns one entry per
resource type: `{validation_valid: True|False|None, validation_summary,
status_code, posted: True|False|None, error, resource_returned}`.
Runs in `collect_evidence()` only when a token exists (mock, or live +
completed `/callback`); otherwise `write_back` is `{"_unavailable": ...}`.

**6. Rules (`rules/write_back.json`) + checks (`automated_checks.py`)**
- `WRITE-001/002/003` (DiagnosticReport/Observation/Media): pass only if
  `validation_valid is True` **and** `posted is True`; `needs_human` if either
  is `None` (validator or POST never completed); `fail` otherwise.
- `WRITE-004` (write scopes actually honored): pass only if all three POSTs
  succeeded; `fail` if any was refused with 401/403 (names which resource was
  denied); `needs_human` if any POST never completed. Deliberately **does not**
  read the granted-scope string — the task called this out explicitly, and
  AUTH-003 already covers "was the scope string granted." This proves the
  scope was *honored*, not just advertised.
- Registered in `AUTOMATED_CHECKS`; `rules/write_back.json` follows the same
  schema (`id, category, source, severity, evidence_needed, check_type,
  description, remediation_hint`) as `auth.json`/`fhir_resources.json`.

## Tests

New `tests/test_writeback.py` (42 tests): resource builders,
`collect_writeback_evidence()` against a stub always-valid / always-invalid /
unavailable validator and a mock `FhirClient` (including a 403-denying stub
client), all four WRITE-00x checks parametrized across pass/fail/needs_human/
missing-evidence, pipeline wiring in mock and live mode (including the
live-mode-without-a-launch-stays-unavailable regression and the
set_live_token()-unlocks-write-back-evidence path), and `/launch`+`/callback`
via `fastapi.testclient.TestClient` (Epic error, unknown state, missing code,
successful exchange populates the holder + single-use `state`, token-exchange
exception doesn't 500).

Also fixed one drift: `tests/test_auth_module.py`'s `_StubClient.fetch_all_us_core`
had no `patient_id` parameter; `collect_evidence()` now passes `patient_id`
positionally (needed so write-back POSTs reference the right patient). Updated
that test's stub and extended its assertions to also cover `write_back`
degrading to `needs_human` in the no-token live case.

**Before this task:** `pytest -m "not launch"` → 109 passed (Day 15 said 109,
confirmed unchanged going in).
**After:** `pytest -m "not launch"` → **151 passed**, 6 deselected (`-m launch`).
`./scripts/verify_tool.sh` → **ALL CHECKS PASSED (22 checks)**.

## Honest status — what's proven vs not

**Proven (by tests, offline):**
- `/callback` completes a token exchange and unlocks the live-mode evidence
  path — verified via `TestClient` with `exchange_code_for_token` mocked out;
  never touches the network in tests.
- `collect_evidence()` in live mode is unchanged in behavior when no launch
  has happened (still `needs_human` everywhere token-dependent) — this was a
  regression risk given the rewrite, and it's covered.
- WRITE-001..004 correctly degrade to `needs_human` when validation is
  unavailable and correctly `fail` when validation reports errors or a POST is
  refused with 401/403 — covered with stub validators/clients, not just happy path.
- `python -m epic_compliance` mock CLI run: `pass=14 fail=2 needs_human=6`
  (22 findings, up from 18 pre-task). The 3 new `needs_human` are WRITE-001/002/003
  — this machine has no HL7 validator container running right now
  (`VALIDATOR_MODE=auto` tried the HTTP service at :3500 and the jar, found
  neither), so `is_valid()` correctly comes back `None`, not a fabricated pass.
  This was verified by hand (`run_full_pipeline`, printed each WRITE finding)
  — it is the tri-state invariant working, not a bug.

**NOT proven — genuinely open:**
- **No live launch against the real Epic sandbox was run in this task.**
  `/launch` → Epic login → `/callback` → a real write-back POST to Epic was
  never exercised end-to-end with actual credentials. Day 13/14 proved the
  read-side interactive flow works against Epic; the callback route reuses the
  same `exchange_code_for_token()` that flow already validated, and the write
  path reuses `httpx.post()` the same way `fetch_resource()` already does —
  but the write-specific concerns (does Epic's sandbox actually grant
  `patient/DiagnosticReport.write` etc. to this app registration? does Epic
  accept these exact resource shapes?) are unverified against a live server.
  **This is the single most important thing to check next**, and it needs a
  human at a browser plus an Epic app registration with the write scopes
  enabled (Epic scopes are enabled per-app at fhir.epic.com, same as the read
  scopes were in Day 13).
- **WRITE-001/002/003 have never gone green against the real HL7 validator.**
  The stack that would prove this (`podman compose -f
  docker/fhir-validator.compose.yml up -d`, per notes/day15-results.md) was
  not started for this task — Day 15 already left "a successful end-to-end
  validation was not confirmed" as open, and that is still true, now also for
  the write-back payloads specifically. The `US_CORE_PROFILES`-equivalent
  `WRITE_PROFILES` canonicals for Observation/DiagnosticReport are my best
  read of which US Core 3.1.1 profile fits an AI-generated imaging finding —
  they have not been checked against the validator, so it's possible the
  validator rejects them for a reason unrelated to this app's actual
  conformance (wrong profile picked, not wrong resource).
- **Only `state_validated` is provable from a normal `/callback` launch.**
  AUTH-007..010-style negative-path proof for the write flow (e.g. "does Epic
  actually enforce `patient/Observation.write` separately from
  `patient/Observation.read`, or does it silently allow writes with a
  read-only grant?") is not attempted anywhere — WRITE-004 infers this from
  whatever Epic's real response codes are, but nobody has run it live yet to
  see what those codes actually are.
- **DocumentReference is unaffected** — the read-path FHIR-00x rules and
  `US_CORE_RESOURCES` list are untouched; this task was additive only.

## Still open for a future pass

- Run the container-based HL7 validator locally and confirm `WRITE-001..003`
  actually validate the constructed resources cleanly (or find out they don't,
  and fix the resource shapes / profile choices — this is very plausible for a
  first-pass Media/imaging Observation body).
- Run one real interactive launch with `patient/DiagnosticReport.write` /
  `patient/Observation.write` / `patient/Media.write` enabled on the sandbox
  app registration and confirm Epic actually accepts the writes (or learn what
  it rejects and why — 422s on first contact with a new resource type against
  a real server are the norm, not the exception, going by the Day 15 validator
  experience).
- `id_token` signature verification (AUTH-010) is still not implemented in
  code — unchanged from Day 14's "still open."
- Multi-organization / multi-tenant config was explicitly out of scope here
  and remains untouched (`config.py` is still single-tenant, as instructed).
