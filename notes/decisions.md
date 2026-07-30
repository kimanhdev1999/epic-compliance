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
