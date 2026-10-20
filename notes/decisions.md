# Architectural Decisions

## ADR-001: Backend — Python 3.x + FastAPI

Date: 2026-06-19

### Decision
Use Python (3.11+ target; tested on 3.14) with FastAPI as the backend framework.

### Reasoning
- FastAPI gives async HTTP, Pydantic v2 integration, auto OpenAPI docs, and a clean `/run`/`/report` API surface with almost no boilerplate.
- Pydantic v2 + pydantic-settings handles typed config ingest and env/.env loading cleanly.
- The `anthropic` Python SDK is first-class (same company), making LLM evaluator integration straightforward.
- `httpx` covers both sync and async FHIR/SMART HTTP calls with a consistent interface.
- `rich` gives a polished CLI findings table with no extra effort.
- Python's ecosystem has best coverage of FHIR tooling (fhir.resources, hl7, etc.) for future validator wrapping.

### Alternatives considered
- **TypeScript/Node + tRPC**: Strong FHIR ecosystem (fhir-kit-client) but less natural for subprocess orchestration of the ONC test kit and HL7 validator JAR.
- **Go**: Fast and easy to deploy as a binary, but Anthropic SDK and FHIR tooling are thinner.

---

## ADR-002: Rule catalog format — JSON files in rules/

Date: 2026-06-19

### Decision
One JSON file per rule category (`rules/auth.json`, `rules/fhir_resources.json`, `rules/security.json`). Schema: `{id, category, source, severity, evidence_needed, check_type, description, remediation_hint}`.

### Reasoning
- Plain JSON is editable without code, diffable in git, and trivially loaded.
- Splitting by category keeps files small and makes it easy to add Epic-specific rules in a separate file.
- The `check_type` field (`automated`|`llm`) drives engine branching without a code change.

---

## ADR-003: Mock mode for all external calls

Date: 2026-06-19

### Decision
Every external call (SMART discovery, token exchange, FHIR fetch, LLM) has a `mock=True` path with offline fixtures. The `RUN_MODE=mock` env var enables the full mock pipeline.

### Reasoning
- Lets the pipeline run end-to-end in CI and local dev without Epic credentials or an Anthropic API key.
- Makes tests deterministic and fast (no network).
- Live and mock paths share the same interfaces, so switching to live is a config change, not a code change.

---

## ADR-004: LLM evaluator invariants

Date: 2026-06-19

### Decision
- LLM verdicts must always be one of `pass | fail | needs_human` (needs_human is always available as escape hatch).
- Citation must quote actual evidence text — never empty. If the LLM omits a citation, the verdict is forced to `needs_human` and flagged.
- In mock mode (no API key), all LLM rules return `needs_human` with a mock citation explaining why.

### Reasoning
Prevents the LLM from asserting compliance with no evidence trail. Every LLM finding must be auditable back to the evidence string that was sent.

---

## ADR-005: Do not build a FHIR validator or conformance engine

Date: 2026-06-19

### Decision
Hand-written assertions in `automated_checks.py` are structural checks only (field presence, value membership). Real conformance validation will wrap:
- The HL7 FHIR Validator (Java CLI / `infernocommunity/inferno-resource-validator` container already running in the ONC test kit stack).
- The ONC (g)(10) Test Kit API (`http://localhost:8080`).

Seams are clearly marked with `# SEAM:` comments in `automated_checks.py` and `AUTOMATED_CHECKS` registry.

### Reasoning
Building a conformance engine from scratch would consume the entire budget on something that already exists and is maintained by HL7/ONC. Our value is orchestration, the Epic layer, and the LLM evaluator.

---

## ADR-006: Use the HL7 validator HTTP service, not validator_cli.jar

Date: 2026-08-05 (Day 15 — implements the seam ADR-005 reserved)

### Decision
The default validator backend is `infernocommunity/inferno-resource-validator`
over HTTP (`docker/fhir-validator.compose.yml`, port 3500). `validator_cli.jar`
via subprocess stays as a second backend, selectable with `VALIDATOR_MODE=java`.
`get_validator()` in `auto` mode tries the service, then the jar, then returns a
`NullValidator`.

### Reasoning
- No JRE on the dev machine; the jar needs Java 11+. The container needs none.
- It is the *same* validator image the ONC (g)(10) test kit runs, so our results
  match the gold reference rather than a differently-configured validator.
- Keeping the jar backend means CI (or a machine without a container runtime)
  is not blocked.

### Consequence — the rule that matters
A validator that cannot be reached returns `available=False`, and
`ValidationResult.is_valid()` returns **None**, not False and never True. Callers
must map None to `needs_human`. This is the same invariant as the Day 14 auth
evidence: absence of proof is not proof of compliance.

### Known API constraints (both found via live 500s)
- Request must be a `ValidationRequest` envelope; `fileContent` is an escaped
  JSON string.
- `validationContext.igs` must include `hl7.fhir.us.core#3.1.1` or profile
  resolution fails.
- The validator JVM needs >2 GB; the podman machine must be sized accordingly
  (`podman machine set --memory 6144`) or the container is OOM-killed.

---

## ADR-007: Write-back only ("direction B") — this app never hosts a FHIR server

Date: 2026-09-29

### Decision
The app this tool verifies sends a patient photo to its own backend, runs an
AI diagnosis, then **POSTs** the result into Epic as FHIR resources
(`Observation`, `DiagnosticReport`, `Media`) using write scopes granted at
SMART launch. The doctor sees the result natively in Epic's chart. This tool's
write-back rules (`rules/write_back.json`, `WRITE-001..004`) test exactly that:
this app acting as an outbound FHIR *client* making POST calls, the same role
every SMART app already plays for reads.

The alternative ("direction C" — the app hosts its own FHIR API/CapabilityStatement
for Epic, or anyone, to query) was explicitly rejected and nothing resembling it
was built: no `CapabilityStatement` route, no SMART Backend Services server-side
auth, no inbound FHIR endpoint anywhere in `epic_compliance/`.

### Reasoning
- **Scaling to Epic's App Market.** Direction C means standing up and
  operating a FHIR server, per customer organization, that Epic (or Epic's
  proxy layer) must be able to reach and trust. Direction B means this app is
  a garden-variety SMART client — Epic already knows how to authorize and
  receive writes from those, at any scale, with no new infrastructure per
  customer.
- **Data lives where the doctor already looks.** A push into the patient's
  actual chart is clinically useful immediately; a second FHIR server the EHR
  has to be taught to poll is not how any current Epic integration pattern
  works.
- **Smaller attack surface.** No inbound authorization server, no server-side
  scope enforcement, no possibility of a misconfigured CapabilityStatement
  leaking PHI to an unintended caller. The only new capability this app has is
  "can construct and POST a well-formed FHIR resource," which is exactly what
  WRITE-001..004 test.

### Consequences
- `epic_compliance/fhir/writeback.py` only builds resource *bodies*; it has no
  route decorators, no server logic.
- `FhirClient.post_resource()` (epic_compliance/fhir/client.py) is the only
  new capability added to the FHIR client — an outbound POST, not a listener.
- WRITE-004 ("write scopes actually granted") is proven the same way the
  AUTH-006..010 negative-path probes are proven: by the actual attempt
  succeeding or being refused (401/403), never by reading the requested-scope
  string back to itself.
- The validator (`epic_compliance/validator/`) is reused unchanged for
  outgoing payloads — `get_validator(config).validate(resource, profile)` is
  called on the constructed Observation/DiagnosticReport/Media before POSTing,
  same tri-state `is_valid()` semantics as the read path.

---

## ADR-008: Wire SEC-00x LLM rules to real app_config evidence, add an eval harness

Date: 2026-10-02

### Decision
Found that `SEC-001/002/003` (`rules/security.json`) were structurally dead:
their `evidence_needed` referenced `app_config` fields (`transport_tls_version`,
`audit_logging_enabled`, `audit_log_retention_days`, `baa_in_place`,
`covered_entity_relationship`) that were never collected anywhere —
`collect_evidence()` had no `app_config` key, and `_build_evidence_for_llm()`
only pulled `smart_configuration`/`token_response`. Every live call to the LLM
evaluator for these three rules was therefore sending no real evidence, making
`needs_human` the only honest outcome regardless of the actual app's posture.

Also found `epic_compliance/rules_engine/llm_evaluator.py` had zero test
coverage and no way to know whether its verdicts agree with a human — the
mock/citation/parsing invariants in its own docstring were unverified.

Fixed both:
- Added the five `app_config` fields to `AppConfig` (env-settable, default `""`
  = not attested) and to `OVERRIDABLE_FIELDS`; `collect_evidence()` now emits
  an `app_config` evidence block (`_unavailable` if nothing was attested,
  never a silent empty dict that could read as "nothing wrong").
  `_build_evidence_for_llm()` now includes it.
- `tests/test_llm_evaluator.py` — mock mode, response parsing (direct JSON,
  JSON embedded in prose, malformed, invalid verdict), and the live path
  against a stubbed `anthropic` client (never touches the network): citation
  invariant, missing-citation-forces-needs_human, requirement+evidence are
  actually sent.
- `scripts/eval_llm_rules.py` + `tests/fixtures/llm_eval_cases.json` (10 hand-labeled
  cases across all three SEC rules) — a runnable harness that scores the real
  LLM's verdicts against expected ones (`needs_human` always counts as a safe
  non-miss; a confident wrong verdict is the only true miss). `--mock` runs the
  harness without an API key as a sanity check of the harness itself, not an
  eval. `tests/test_llm_eval_harness.py` covers the scoring logic with a
  stubbed `evaluate_with_llm`, so CI never needs `ANTHROPIC_API_KEY`.

### Reasoning
A compliance tool whose LLM-assisted checks silently never had real input is
worse than one that's honest about not checking at all — `needs_human` was
masking a wiring gap, not reflecting a genuine "needs a human" case. Fixing
the evidence path is what makes `needs_human` → `pass`/`fail` on these rules
mean something. The eval harness exists because "the LLM evaluator runs
without crashing" (what the old test suite covered — nothing) is not the same
claim as "the LLM evaluator agrees with a human reviewer," and only the latter
is worth anything for a compliance tool.

### Consequences
- Running with `RUN_MODE=live` and a real `ANTHROPIC_API_KEY` but no
  `TRANSPORT_TLS_VERSION`/etc. set still correctly yields `needs_human` for
  SEC-001/002/003 — attestation is opt-in via `.env`, not auto-discovered (no
  code exists anywhere to introspect a third-party app's actual TLS config or
  BAA status; that's inherently a human-attested fact).
- `scripts/eval_llm_rules.py` should be re-run (with a real API key) any time
  `SEC-00x` wording or the LLM system prompt changes — it's the only thing
  that would catch a prompt change silently flipping verdicts on known cases.
- Dashboard form (`epic_compliance/templates/dashboard.html`) was **not**
  updated to expose the five new fields — they're `.env`-only for now. Adding
  UI controls for them is a separate, smaller follow-up if wanted.

---

## ADR-009: Widen scope from "Epic integration works" to "the app is ready to submit"

Date: 2026-10-07

### Decision
Until now every rule was fed by this tool's own calls to Epic: its own SMART
login, its own sample write-back resources, and the app team's typed security
answers. A clean run proved Epic behaves and the app's Epic registration grants
what it needs. It said nothing about the app's backend. The tool grows four
capabilities, in this order:

1. **Honest verdicts on what exists today (done in this change).** Every rule
   now carries a `subject` (`epic_platform`, `app_registration`, `tool_sample`,
   `tool_launch`, `app_attested`), shown in the report as "What this tests".
   Evidence the tool never collected no longer produces a verdict: a failed
   discovery is one AUTH-001 `fail` (AUTH-002/004/010 → `needs_human`) instead
   of a crashed run, and with no token FHIR reads are skipped and FHIR-001..005
   say `needs_human` instead of failing on 401s (FHIR-004 previously *passed*
   with nothing inspected). AUTH-001 now checks the required capability list
   (adds `context-standalone-patient`, which the ONC g10 kit requires for
   standalone launch) instead of "list is non-empty".
2. **Verify mode — read back what the app wrote.** After a real app visit in
   the Epic sandbox, search Epic by patient + Encounter/time window and judge
   the app's actual `Encounter`, `DocumentReference` (SOAP note),
   `DiagnosticReport`/`Observation` (AI results) and `Media` (photos and the
   visit recording): US Core validity, right patient, all linked to one
   Encounter. New rules in `rules/app_writes.json`.
3. **App probes — test the app's own endpoints black-box.** Against the app's
   **staging** backend only: inspect the authorize redirect it builds (S256,
   `aud`, random `state`, minimal scopes); send its real callback a forged
   `state`, missing `code` and replayed code and expect 4xx; read its real TLS
   version/HSTS, replacing the typed SEC-001 answer. New config
   `APP_BASE_URL`, `APP_LAUNCH_PATH`, `APP_CALLBACK_PATH`; rules in
   `rules/app_behavior.json`.
4. **Submission evidence pack + readiness verdict.** Checklist of what Epic's
   review asks for (threat model, data-flow diagram, subprocessor inventory,
   encryption/key management, recent pen test, SOC 2 or equivalent, scope
   justification); uploaded documents judged for completeness by the LLM
   evaluator; scope justification and test evidence generated from runs;
   exported as one bundle. Top-level verdict: blockers / missing evidence /
   needs human / ready to submit. Rules in `rules/submission.json`. Confirm
   the checklist against Epic's own vendor documentation before building —
   the current list comes from third-party guides.

### Decisions taken with the app team (2026-10-07)
- Probing the app's **staging** backend (forged callbacks, TLS checks) is
  approved. Never production.
- The app backend **will** export its Epic request log (JSONL), but not yet.
  Verify mode ships read-back-only first, which sees only writes Epic
  accepted; log ingest is added later to catch rejected writes.
- The visit **recording goes to Epic**, so it gets a write rule (as `Media`,
  or `DocumentReference` + `Binary` — decide when building step 2).

### Reasoning
- The question the app team actually has is "will Epic accept us", and the
  only evidence that answers it is the app's own behaviour and output. Rules
  that only exercise Epic stay, but labelled so a green "Tests Epic" row is
  never mistaken for evidence about the app.
- The tool outputs "ready to submit", never "will be accepted". Epic runs its
  own security, vendor and business review; this tool assembles evidence for it.
- Read-back first because it needs no access to the app backend and directly
  answers the Patients-mode question ("is the whole visit in the chart").

### Consequences
- `Rule.subject` is required in spirit: `tests/test_no_false_verdicts.py`
  fails if any rule's subject is not in `viewmodel.SUBJECTS`.
- `collect_evidence()` no longer raises on discovery errors; callers that
  relied on the exception (none today) would need to read
  `smart_configuration["_error"]`.
- `api.py`'s `/launch` still calls discovery directly. It returns a 502 when
  the authorization endpoint is empty, but a discovery *exception* (wrong URL,
  404) still surfaces as an unhandled 500 — not fixed in this change.

## ADR-010: Launch profiles — add clinician standalone alongside patient launch

Date: 2026-10-14

### Decision
The app's design changed: a standalone mobile app where a clinician signs in
through Epic, then searches for and confirms the patient, photographs the
lesion (image write over FHIR, which the organization's build supports), and a
derm files a consult note. `LAUNCH_PROFILE` selects which flow is tested:

- `patient_launch` (default, unchanged): patient chosen at login, `patient/*`
  scopes, AI write-back of ADR-007 (WRITE-001..004, AUTH-009).
- `clinician_standalone`: `user/*` scopes, no `patient` in the token, a
  Practitioner `fhirUser`. Runs `rules/clinician_workflow.json` (CLIN-001..012)
  and skips AUTH-009 and WRITE-001..004.

Rules carry an optional `profiles` list; a rule outside the active profile is
not run (not reported needs_human). AUTH-003 and AUTH-005 read
`evidence["launch_profile"]` and judge `user/*` scopes and a Practitioner
fhirUser under the clinician profile.

New config: `LAUNCH_PROFILE`, `TEST_PATIENT_QUERY`, `IMAGE_WRITE_RESOURCE`
(Media | DocumentReference), `ENCOUNTER_REQUIRED`, `APP_CLIENT_TYPE`,
`APP_REDIRECT_URI`. `/launch` requests `CLINICIAN_SCOPES` under the clinician
profile (no `launch/patient`; no `offline_access` for a public client).

### Reasoning
- Under the old rules a correct clinician app would *fail* AUTH-005 (no
  patient in the token) and AUTH-003 (no `patient/*` scopes), and WRITE-001/002
  tested resources this design does not write.
- Wrong-patient photos are the main risk the design names. CLIN-007 judges
  what Epic *stored* (read back by id), not what was sent: every write must
  reference the confirmed patient and the selected Encounter.
- CLIN-001 treats a search matching several patients as needs_human, not a
  pass: only an unambiguous (identifier/MRN) lookup proves the search step.
- Write ids are taken from the response body or, failing that, the
  `Location` header — Epic often answers a create with an empty body.

### Consequences / limits
- CLIN-005..010 are `tool_sample`: this tool performs the flow through the
  app's registration. They prove Epic accepts it, not that the app's own code
  confirms the right patient. Reading back the app's real writes is still
  ADR-009 step 2.
- The Epic sandbox may not support image writes the way the organization's
  build does; CLIN-005 in the sandbox is not proof for their environment.
  Repeat the run against their non-production environment before go-live.
- CLIN-011/012 are attested (from config), not probed. Probing the app's real
  redirect handling is ADR-009 step 3.

## ADR-011: Assessment flow — describe the app, generate the plan, then run

### Decision
The web UI is now a three-step assessment, modeled on cloud/AI security-audit
tools: **describe your app** (intake questionnaire) → **review test plan**
(the checks the answers call for, each with a reason, plus the skipped ones)
→ **fix findings** (readiness score, failures by severity, "Fix these first",
"Needs your review", filterable findings). The raw run form is gone from the
UI; `POST /runs`, the CLI and the JSON API are unchanged.

`epic_compliance/assessment.py` maps answers to rules (`SELECTORS`: rule id →
included + reason) and to config overrides (launch profile, client type,
attested security answers). The engine gained an optional `rule_ids` filter;
the intake and plan are stored with the run (`config.assessment`) so a report
shows its own scope.

### Reasoning
- The user's goal is getting one app live at one health system (Advocate),
  not a marketplace listing. Asking about the app first means the report only
  contains checks that matter to it, and says why each is there.
- Showing the plan before running keeps the tool honest: what will be judged,
  and how (automated probe vs. AI review of stated answers), is visible up front.
- A rule with no selector is included by default, so adding a rule file never
  silently drops a check; a test keeps `SELECTORS` in step with the catalog.

### Consequences / limits
- The intake chooses the launch profile; live runs still take the client ID
  for `/launch` from `.env`, not the form.
- "I'm not sure" answers are left blank on purpose, so SEC-* stay needs_human.
- The CLI still runs every rule for the configured profile (no intake).

### Amendment (2026-10-16): the intake is a chat, not a form
The user asked for step 1 to be a single chat box: describe the app in words,
and the tool asks follow-ups for anything unclear. `intake_chat.py` covers four
topics (who signs in, app type, patient-data use, security posture) and offers
the plan only when all are settled, after showing "Here's what I understood".

- With `ANTHROPIC_API_KEY`, Claude (`LLM_MODEL`, ADR-004) returns structured
  updates via `output_config.format` (JSON schema); the server, not the model,
  decides when the intake is complete. Any API failure falls back to the
  offline keyword parser for that turn.
- Without a key, the keyword parser + scripted questions keep mock mode fully
  offline. It is deliberately simple; the confirmation card is the guard
  against a misread.
- Chat state round-trips through the page as JSON (single-tenant, no session
  store). "Where to test" moved to the plan page, since it doesn't change
  which checks apply.

## ADR-012: Compliance workspace UI

### Decision
The web UI becomes a workspace for one app, following the common layout of
compliance-automation tools:

- A left sidebar: Home, Frameworks, Tests, Integrations, Reports, and at the
  bottom Describe your app (the ADR-011 chat) and Settings.
- **Home**: a Get-started checklist, overall readiness, progress per framework,
  Needs attention (failing, then needs-review tests), readiness over time,
  integration status.
- **Frameworks**: ONC (g)(10), SMART App Launch, US Core / FHIR R4, Epic
  requirements, HIPAA Security Rule, Patient safety. A rule maps to every
  framework its `source` cites (`workspace.FRAMEWORKS`), so one test can count
  toward several.
- **Tests**: every catalog rule with a status — Failing / Needs review /
  Passing / Not applicable (left out of the plan, with the plan's reason) /
  Not run. Filters and search run client-side. A row opens a detail drawer
  (HTMX partial) with the result, how to fix it, why it's in scope, and its
  history; without HTMX the same URL is a full page.
- **Integrations**: Epic sandbox, Claude, HL7 FHIR validator, ONC test kit,
  each with a status and the tests it unlocks.
  Claude (`ANTHROPIC_API_KEY`) is expected to be set, so the Get-started
  checklist has no "Connect Claude" step; the Integrations card still shows
  its status, and without a key the chat and SEC-00x checks fall back as before.
- **Re-run tests** (`POST /assess/rerun`) re-runs the saved plan, standing in
  for continuous monitoring.

### Reasoning
The user asked for the most common UI pattern for compliance tools. The
leading compliance-automation products have converged on the same
structure, so following it makes the tool familiar to anyone who has gone
through SOC 2 or a HIPAA review. It also fits the job better than a one-off
report: getting into a health system takes weeks of fixing and re-testing, and
the workspace shows progress across those runs.

### Consequences / limits
- The workspace is the latest run plus the last intake. It's single-tenant
  with one app at a time, like the rest of the tool (`config.py`).
- Framework mapping is by citation text (`_cites`).
  `tests/test_workspace.py` fails if a new rule maps to no framework.
- Readiness counts only passing tests in scope, so it reads lower
  than the report's "passed of decided" score, which leaves needs-review out.
- Not built yet: test owners and due dates, evidence uploads for
  needs-review tests, policies, and a Trust Center page. These need storage
  beyond run history.
- `/` is now Home; the chat moved to `/assess`.
- The light theme (dark-mode aware) replaces the dark GitHub-style theme.


## ADR-013: Save the describe-your-app conversation and allow editing answers
**Date:** 2026-10-17  **Status:** Accepted

### Context
The chat state lived only in the browser page (a hidden form field). The
answers reached the database only when a run started (`last_intake`). Closing
the tab before running lost the whole conversation. Apps also change over the
weeks of a health-system review, so an answer given earlier ("read-only")
needs to be corrected later.

### Decision
- Every chat turn appends a snapshot of the full `ChatState` to a new
  `chat_turns` table in the local SQLite DB (`store.save_chat_state`). The
  newest row is the current conversation, and `/assess` opens it.
- **Edit an answer:** each of the user's messages has an Edit button
  (`POST /assess/edit`). It rewinds to the snapshot taken just before that
  message (`intake_chat.rewind`), so the intake reflects only the earlier
  answers. The edited message is then answered as a new turn, and later
  messages are replaced. If no exact snapshot exists (older turns were
  trimmed by `MAX_TURNS`), it keeps the current intake and drops the later
  messages.
- **New conversation** (`POST /assess/new`) saves a fresh start as the newest
  snapshot. Earlier conversations stay in the table.
- Home and the sidebar use the saved conversation's answers until the first
  run saves `last_intake`.

### Reasoning
Append-only snapshots are the simplest way to support both "resume later" and
"edit an earlier answer" correctly. Replaying the conversation to rebuild the
intake would mean new Claude calls and could give different answers. The rows
are small, and the DB is local and single-tenant like the rest of the tool.

### Consequences / limits
- The conversation can contain PHI-adjacent product details but not patient
  data; it is stored only in the local DB, like run history.
- No UI yet to browse or restore older conversations; they are kept in
  `chat_turns` for that later.
- Editing an answer doesn't change existing reports. Run the plan again to
  apply it.

## ADR-014: Sample-data ("demo") mode becomes a smoke test, not a UI choice
**Date:** 2026-10-17  **Status:** Accepted

### Context
The plan page offered "Demo data" next to "Epic sandbox (live)". Demo runs
use built-in sample Epic responses that are written to be correct, so most
tests pass and the report says nothing about the user's integration. Users of
an Epic integration tool found the choice confusing and the results
misleading. In demo mode the SEC-* rules also skipped Claude even with a key.

### Decision
- The web UI only runs against the Epic sandbox. The plan page no longer asks
  where to test. Run is disabled until a SMART sign-in has completed, and the
  page polls `GET /launch/status` to unlock it. Without `OAUTH_CLIENT_ID` it
  says to add one. `Intake.run_mode` defaults to `live`.
- The Home checklist connects the Epic sandbox before the first run. Settings
  no longer has a run-mode field.
- Sample-data mode (`RUN_MODE=mock`, `run_mode=mock` on the routes) stays for
  development and as the **smoke test**: `tests/test_smoke.py` (`pytest -m
  smoke`) runs chat → plan → run → report → every workspace page → rerun for a
  clinician and a patient app, plus the CLI, with no network, credentials or
  Claude key.
- `.github/workflows/ci.yml` runs the smoke test, then the unit tests, on
  every pull request and push to main. `live` and `launch` tests don't run in
  CI.

### Consequences
- Nothing can be run from the UI without a registered non-production Epic
  app. That's the point: only sandbox results mean anything to a health system.
- Every UI run is live, so SEC-001..003 are always judged by Claude when a key
  is set.
- Old demo runs in the history still show as "Demo data".

## ADR-015: Evidence on tests, and missing answers are never a fail
**Date:** 2026-10-19  **Status:** Accepted

### Context
The AI-reviewed tests (SEC-001..003) judge what the developer states about
TLS, audit logging and BAAs. The only way to state anything was the chat, and
the only output was the chat answer, which is an attestation without proof.
Developers and backend engineers use this tool to hand evidence to a health
system's security review, so they need to attach proof.

A run also showed a bug: with "Not sure" answers there was nothing to judge,
but Claude returned **fail** for SEC-002 even though its instructions said to
use needs_human when evidence is insufficient.

### Decision
- **Missing answers are never a fail.** `engine._nothing_to_judge` returns
  needs_human without calling Claude when an attested rule has none of its
  `app_config` answers (read from `evidence_needed`) and no attached evidence.
  Claude's system prompt also says missing or "not sure" information is never
  a fail; fail only when evidence positively shows non-compliance. SEC-001's
  `evidence_needed` now names the real field (`transport_tls_version`).
- **Evidence on every test.** The test side panel has an Evidence section:
  a statement, files (text/log/JSON/YAML/CSV/config, PNG/JPEG/GIF/WebP, PDF;
  up to 10 MB each) and links. Rows live in an `evidence` table and files in
  `data/evidence/` next to the DB (git-ignored), named by a random id; the
  original name is sanitised and the SHA-256 is recorded. Downloads are always
  served as attachments.
- **Claude reviews it.** For AI-reviewed tests, statements, links and text
  files go in the request as numbered `developer_evidence`; images and PDFs
  are sent as image / document blocks. Claude cites evidence by number, treats
  a statement as an attestation and a bare link as no proof.
- **The report lists it.** Each run stores a snapshot of the evidence it used
  (metadata and hash, not file contents) in its config; the report and the
  export show "Evidence provided" under each finding.
- The upload form warns: no real patient data; sandbox or de-identified
  samples only.

### Consequences / limits
- Evidence is per test, not per run: it applies to every later run until
  removed. A report keeps the snapshot even if a file is later deleted, but the
  download link then returns 404.
- Automated tests show attached evidence in the report but their verdict still
  comes from the automated check only.
- Links are not fetched. Files are sent to the Anthropic API for AI-reviewed
  tests; nothing is uploaded anywhere else.

## ADR-016: Honest, current results and a guided run setup (UX review)
**Date:** 2026-10-19  **Status:** Accepted

### Context
A UX review (the `ux-flow-reviewer` agent, `.claude/agents/`) walked every
flow. Its top findings matched what confused the user:
- Results never said they were out of date. After a rule fix, or after adding
  evidence or changing an answer, Tests still showed the old "Failing".
- The plan page put the sign-in step above the client ID field it needs, and
  both sat below 23 checks. The test patient field was optional, unexplained,
  and leaving it empty silently skipped 9 clinician checks.
- Re-run started a live run with no Epic sign-in (e.g. after a restart) and
  overwrote real results with "Needs review".
- The report's 100% (passed of decided) contradicted Home's 74% (passed of all).
- The Evidence form looked unfinished.

### Decision
- **Freshness.** Each run stores a `rules_fingerprint` (hash of `rules/*.json`)
  next to its evidence snapshot and intake. `workspace.stale_reasons` compares
  them with now; Home, Tests and Frameworks show "These results are out of
  date. Since the last run, …" with Re-run. Tests whose evidence changed get a
  "Changed" chip and a note in the side panel.
- **Plan page: "Before you run" first.** Numbered steps (client ID, test patient
  for clinician apps, sign in) with check marks, above the checks list. Run
  unlocks only when all are done, and says what's missing. There is now a
  `GET /assess/plan` built from the saved answers, so the page can be reloaded.
- **Guards.** `/assess/run` and `/assess/rerun` won't start a live run without
  a sign-in, or a clinician run without a test patient. They redirect to the
  plan page with a notice instead. The sign-in also expires with Epic's token.
- **Plain messages.** "Not signed in to the Epic sandbox…", "Skipped: no test
  patient was given…", and answer names instead of config field names. The side
  panel's button goes to the test plan when that's where the fix is.
- **One readiness number.** The report uses Home's formula (passing / all
  checks run) and says how many await review.
- **Evidence UI redesigned.** A list of items and a tabbed form (Statement / File / Link)
  with a drop zone. The form folds away once evidence exists, and the copy
  depends on whether Claude is connected.

## ADR-017: UX redesign: one profile, one connection, one run, one report
**Date:** 2026-10-20  **Status:** Proposed

### Context
A second end-to-end UX walkthrough (`ux-flow-reviewer`) found that the verdicts are
honest but the experience is still confusing:
- The user's answers live in three places (chat state, `last_intake`, plan page).
  Re-run uses `last_intake`, so chat corrections are silently ignored.
- Epic can be connected from three screens that behave differently.
  Integrations signs in without a launch profile, and the checklist says
  "connected" when only a client ID is saved.
- Needs-review results show raw evidence (dict dumps, `[MOCK]`, errno), and the
  export carries that text to reviewers.
- Readiness counts `epic_platform` tests, which pass for any app pointed at Epic.
- One idea has several names: run, assessment, report, plan, check, rule, finding.
- There are dead ends: the Backend starter loops, the out-of-date banner sticks
  because proof on out-of-plan tests is counted, and missing pages return raw JSON 404s.

### Decision
Adopt the redesign in `docs/ux-redesign/` (canvas source and README). Its key rules:
- **App profile** is the single, versioned source of truth. The chat is one way to
  fill it in. Each run stores `profile_version`, and Re-run uses the current
  profile with a plan diff.
- **One Connect to Epic flow** used everywhere. "Connected" requires all of:
  - a successful sign-in;
  - the matching launch profile;
  - at least 5 minutes before the sign-in expires.

  Connection metadata (not the token) is persisted, so a restart shows "Expired".
- **Results** gain `summary` (one plain sentence) and `needs_reason`
  (answer | proof | connect | patient | external). Raw evidence moves to "Technical details".
- **Scoring:** "Your app" readiness covers the subjects `app_registration`,
  `tool_sample` and `app_attested`, and is the headline. "Epic environment"
  (`epic_platform`, `tool_launch`) is reported separately.
  The verdict is one of Blocked, Fixes needed, Missing proof or Ready to submit.
- **Glossary:** Test, Plan, Run, Result, Proof, Report, Connection.
- **Navigation:** Overview, App profile, Tests, Run, Runs, Report, Connections.
  Frameworks become a Tests filter; Integrations and Settings merge into Connections.
- Delivered in four phases (wording first, then profile, connection, run and report);
  see `docs/ux-redesign/README.md`.

### Consequences
- New `profiles` table and `runs.profile_version`. `settings.last_intake` is retired.
- Old routes (`/assess*`, `/frameworks*`, `/integrations`, `/config`) redirect to the new ones.
- Staleness compares in-plan tests only, and proof is not accepted on out-of-plan tests.
- The CLI and `/api/run` still ignore the plan. A `--profile` flag is planned so they can use it.

## ADR-018: "Where to fix it" on every result; sample runs never replace real ones
**Date:** 2026-10-20  **Status:** Accepted

### Context
The user opened Tests, saw two failures (AUTH-010, FHIR-004) and couldn't tell
whether the mistake was theirs, their Epic registration's or Epic's. In fact:
- Both tests check Epic's own behaviour.
- Both came from a sample-data (mock) run made by older code.

The workspace always showed the newest run, sample or real. The "Tests Epic"
badge said what a test exercises, not who has to act.

### Decision
- **Where to fix it.** Every Failed or Needs review result carries one of six
  labels: Your app · Your Epic app registration · Your answers or proof ·
  This tool's setup · Epic's sandbox · Sample data.
  - A check that knows the cause sets `Finding.fix_where`. For example, CLIN-001
    with 0 matches means the test patient is wrong, so it sets `tool_setup`.
  - Otherwise `fix_where.where_to_fix()` derives the label from the rule's
    `subject` and the verdict. This also covers runs saved before the field existed.
  - A failed `tool_sample` write counts as the registration when Epic refused it
    (401/403), and as Epic's sandbox otherwise. The tool's own sample is valid
    by construction.
- **Blocking** means Failed, critical or high, and not Epic's sandbox or Sample
  data. The user can't fix those two, so they never block.
- **Sample runs never replace a real run.** `store.latest_run()` prefers the
  newest real run. With no real run yet, it shows the newest sample run under a
  "Sample data — not a real result" banner, and every failure in it is labelled
  Sample data.
- **Where it shows:**
  - Tests: a column plus a filter (`?where=`).
  - Test panel: a box above "How to fix". "How to fix" is hidden when the user
    has nothing to fix.
  - Home rows, the report and the export.

### Consequences
- UI tests that post `run_mode=mock` still see their run, now with the sample banner.
- `tests/ui/test_honesty.py::test_sample_data_is_unmissable` now passes. Its
  known-issue marker is removed.
- The design canvas (`docs/ux-redesign`, Spec 3 § 03b) describes these rules.

## ADR-019: AI-generated test plan, grounded in a reviewer-question library
**Date:** 2026-10-20  **Status:** Accepted (design); built in four PRs

### Context
The plan only toggled fixed checks with `SELECTORS` (`assessment.py`). The only
questions about the vendor's own practices were SEC-001..003 (TLS, audit logging,
BAA). Real reviews ask much more: a health system's security questionnaire (HECVAT
or its own), Epic's app-review topics, and for some apps FDA status. For example,
a mobile lesion-photo app also gets asked about photos in the camera roll, location
data in images, patient identity before capture, body site and laterality, consent,
and whether image analysis makes it a medical device. The user's goal for the tool:
describe the app, then get a plan covering *enough* of what a reviewer will ask,
alongside the FHIR/SMART/ONC tests. Producing that plan is the LLM's main job.

### Decision
The plan has two layers:
1. **Automated tests** (`rules/*.json`, FHIR/SMART/ONC/write-back). They are still
   chosen deterministically. The LLM may explain them but can never drop one.
2. **Reviewer questions**, generated by Claude from the App profile (ADR-017):
   - Grounded in `rules/reviewer_questions.json`, a curated library. Every entry has
     a cited source (HIPAA 45 CFR section, ONC 170.315, Epic vendor docs, HECVAT
     section, FDA guidance, OWASP MASVS, RFC 8252) and applicability tags
     (`mobile`, `images`, `writes_back`, `ai_analysis`, `ai_vendor`, …).
   - Claude **selects** every library question that applies (with a reason specific
     to this app), **tailors** the wording, and may **add** questions the library
     lacks. Added ones are shown as "AI-suggested, no source" until the user keeps
     or removes them.
   - The server validates the output (structured JSON): library ids must exist,
     questions tagged `all` can't be dropped, and every item needs a reason.
   - **Without an API key**, questions are selected by tags. The plan is less
     tailored but complete for the library, and stays offline.
   - Answering works like SEC-00x: an answer in chat or attached proof, judged under
     ADR-004 (verdict must cite evidence). An unanswered question is Needs review
     (`needs_reason: answer|proof`), never a pass or fail (ADR-015).
   - Subject `app_attested`, so the questions count toward "Your app" readiness. The
     report shows coverage ("Reviewer questions: 14 of 38 answered").

### Guards
- LLM evals (`tests/fixtures/plan_eval_cases.json`): app descriptions, each with the
  library ids the plan **must** include. Example: a mobile lesion-photo app must get
  camera-roll storage, EXIF/GPS stripping, FDA device status, patient ID before
  capture, and body site. A prompt or model change that drops one fails the eval.
- Library entries with an unverified source are marked and listed in the plan as
  "source to confirm". Sources are never invented.
- The tool still says "ready to submit", never "will be accepted" (ADR-009).

### Delivery
1. Library and loader, plus tag-based selection (offline).
2. LLM plan generator, validation, plan evals.
3. Plan, test and report UI: question sections, sources, AI-suggested badges,
   coverage, UI-harness honesty rules.
4. Answering and judging each question in chat and through proof.
