# Day 15 — Wire in the HL7 FHIR validator

Plan: *"Wire in the HL7 FHIR validator (CLI/library) — don't build your own."*

## What was built

`epic_compliance/validator/` — a backend-agnostic integration layer:

| File | Role |
|---|---|
| `models.py` | `ValidationMessage`, `ValidationResult`. `is_valid()` is **tri-state**: True / False / None-when-the-validator-never-ran |
| `outcome.py` | OperationOutcome → messages; sorts by severity, promotes unknown severities to `error`, drops terminology-server noise |
| `backends.py` | `HL7ValidatorService` (HTTP), `JavaCliValidator` (subprocess), `NullValidator` |
| `profiles.py` | US Core **3.1.1** canonicals; prefers the profile the server declares in `meta.profile` |
| `__init__.py` | `get_validator(config)` — `auto` → service, then jar, then Null |

Config: `VALIDATOR_MODE` (auto/service/java/off), `VALIDATOR_SERVICE_URL`,
`VALIDATOR_JAR_PATH`, `VALIDATOR_TIMEOUT_S`.
CLI: `python -m epic_compliance validator` reports the backend and smoke-tests it.
Compose: `docker/fhir-validator.compose.yml` runs the validator standalone.

## Two API details, both learned from real 500s

The service is `infernocommunity/inferno-resource-validator:1.0.78` — the same
image the g10 test kit runs. My first guess at its API was wrong twice:

1. **Bare resource → 500.** `NullPointerException: ValidationRequest.getValidationContext() is null`.
   It wants a `ValidationRequest`: `validationContext` + `filesToValidate`, where
   `fileContent` is the resource as an **escaped JSON string**, not a nested object.
2. **Profile without its IG → 500.** `Error: Unable to resolve profile <url>`, with
   `IGs: []` in the log. `validationContext.igs` must list `hl7.fhir.us.core#3.1.1`.

Both are now encoded in `build_validation_request()` and pinned by tests.

## Why HTTP service, not the jar (ADR-005)

No JRE on this machine, and the official `validator_cli.jar` needs one. The
container is the same validator the certifier's test kit uses, so results match
the gold reference. The jar backend stays as an escape hatch.

## Deployment gotcha — the podman VM was too small

The validator JVM asks for ~1.6 GB heap. The default machine had 2048 MB and the
container was **OOM-killed (exit 137)** mid-package-load. Fixed with:

```bash
podman machine stop && podman machine set --memory 6144 && podman machine start
podman compose -f docker/fhir-validator.compose.yml up -d
```

Also note the first `/validate` call is slow (~10 s here, minutes on a cold
package cache) — it downloads and loads FHIR core + terminology + US Core.

## Status — honest

- `GET /validator/version` → **200**, wrapper 1.0.78 / validator 6.9.7. Verified live.
- `POST /validate` → request envelope corrected from two live 500s, but **a
  successful end-to-end validation was not confirmed** before I stopped. The
  `igs` fix addresses the last observed error; it is not yet proven green.
  **Day 16 must start by running `python -m epic_compliance validator` and
  confirming a real OperationOutcome comes back.**
- 33 unit tests (109 total, was 74). All backends, parsing, degradation, and
  request shape are covered without needing the container.

## Deliberately NOT done (later days)

- No `Finding` mapping yet — that is Day 18.
- Nothing calls the validator from `pipeline.py`; the hand-written `FHIR-001..005`
  checks still produce all FHIR verdicts. Day 16/17 swap them over.
- ONC test kit API invocation is still untouched (Day 19).
