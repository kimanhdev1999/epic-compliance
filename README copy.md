# Epic Compliance Tool

Automated ONC (g)(10) compliance verification for Epic FHIR apps. Checks auth (SMART/PKCE), FHIR resource conformance (US Core), and security/narrative requirements (LLM-assisted), then produces a structured findings report.

## Requirements

- Python 3.11+ (tested on 3.14)
- (Optional) Anthropic API key for live LLM evaluation of security rules

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env
# Edit .env with your Epic sandbox creds and Anthropic API key
```

## Run (mock mode — no credentials needed)

```bash
python -m epic_compliance run
```

Runs the full pipeline against offline fixtures and prints a findings table grouped by category.

## Run (live mode)

Set `RUN_MODE=live` in `.env` along with your `FHIR_BASE_URL`, `OAUTH_CLIENT_ID`, etc. Note: the OAuth authorization-code flow requires a browser redirect; the `/callback` FastAPI endpoint handles the token exchange in live mode.

## FastAPI server

```bash
python -m epic_compliance serve
# or
uvicorn epic_compliance.api:app --reload
```

Endpoints:
- `GET /healthz` — health check
- `POST /run` — execute the compliance pipeline
- `GET /report` — return the last report as JSON

## Tests

```bash
pytest tests/ -v
```

## Project structure

```
epic_compliance/
  config.py          — typed config from env/.env (pydantic-settings)
  models.py          — Finding, Report domain models
  pipeline.py        — top-level orchestrator
  api.py             — FastAPI app
  __main__.py        — CLI entrypoint
  smart/
    discovery.py     — .well-known/smart-configuration fetch + parse
    pkce.py          — PKCE generation, authorization URL builder, token exchange stub
  fhir/
    client.py        — FHIR resource fetcher (US Core), mock mode
  rules_engine/
    catalog.py       — loads rules/*.json
    automated_checks.py — hand-written rule assertions (seams for HL7 validator + ONC kit)
    llm_evaluator.py — Claude API evaluator for security/narrative rules
    engine.py        — runs all rules, aggregates findings

rules/
  auth.json          — SMART/OAuth rules (5)
  fhir_resources.json — US Core conformance rules (5)
  security.json      — Security/privacy rules, LLM-evaluated (3)
```

## What's real vs stubbed

| Component | Status |
|---|---|
| SMART discovery parse | Real |
| PKCE generation (S256) | Real |
| OAuth token exchange | Stub — needs browser redirect for live auth code |
| FHIR resource fetch | Real (mock fixtures in mock mode) |
| Automated rule checks | Real hand-written assertions |
| HL7 FHIR Validator wrapping | SEAM only — not yet wired |
| ONC g10 Test Kit invocation | SEAM only — not yet wired |
| LLM evaluator (Claude) | Real (mock returns needs_human without API key) |
| FastAPI /run /report | Real |
| HTML/PDF export | Not yet (planned Week 8) |
