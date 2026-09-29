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
