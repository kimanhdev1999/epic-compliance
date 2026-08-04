# Epic Compliance Tool

Automated **ONC (g)(10) compliance verification for Epic FHIR apps**.

It connects to a FHIR server (Epic sandbox by default), collects evidence
(SMART configuration, OAuth token, US Core resources), runs a catalog of
compliance rules against that evidence — deterministic checks plus
LLM-assisted checks for narrative/security requirements — and produces a
structured findings report you can browse, export, or consume as JSON.

Every finding is one of `pass` / `fail` / `needs_human`, with the evidence that
produced it, a source citation (SMART IG / ONC §), and a remediation hint.
`needs_human` is a first-class verdict: the tool never guesses.

## What it gives you

- **CLI** — `python -m epic_compliance run`, prints a colored findings table, exits non-zero on failures (CI-friendly).
- **Web UI** — FastAPI + Jinja + HTMX: run form, run history, report detail, standalone HTML export, printable/PDF report.
- **JSON API** — `/api/run`, `/api/runs`, `/api/runs/{id}` for integration.
- **Mock mode** — the whole pipeline runs offline against fixtures, with no credentials and no API key. This is the default.

## Quick start

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env          # optional; defaults work in mock mode

python -m epic_compliance run     # CLI run (mock)
python -m epic_compliance serve   # web UI at http://localhost:8000
pytest -v                         # tests
```

Requires Python 3.11+. An `ANTHROPIC_API_KEY` is optional — without it, LLM
rules return `needs_human` instead of being evaluated.

## Configuration

All config is env / `.env` driven (`epic_compliance/config.py`):

| Var | Default | Meaning |
|---|---|---|
| `RUN_MODE` | `mock` | `mock` = offline fixtures, `live` = real HTTP calls |
| `FHIR_BASE_URL` | Epic sandbox R4 | FHIR base used for discovery + resource fetch |
| `SMART_LAUNCH_URL` / `SMART_TOKEN_URL` | Epic sandbox | OAuth endpoints |
| `OAUTH_CLIENT_ID` / `OAUTH_CLIENT_SECRET` / `OAUTH_REDIRECT_URI` | mock / empty / localhost | SMART app registration |
| `ANTHROPIC_API_KEY` | empty | enables live LLM evaluation |
| `ONC_TEST_KIT_URL` | `http://localhost:8080` | ONC (g)(10) test kit (seam, not yet wired) |
| `EPIC_COMPLIANCE_DB` | `data/runs.db` | SQLite run-history location |

The web UI can override a safe subset per run (`OVERRIDABLE_FIELDS`); secrets
are redacted before anything is persisted or rendered.

## Rule catalog

Rules are plain JSON in `rules/` — add a file or an entry, no catalog code
change needed:

| File | Rules | Check type |
|---|---|---|
| `rules/auth.json` | 5 | automated (SMART discovery, PKCE S256, scopes, token shape) |
| `rules/fhir_resources.json` | 5 | automated (US Core resource / profile conformance) |
| `rules/security.json` | 3 | llm (narrative security & privacy requirements) |

Each rule carries `id, category, source, severity, evidence_needed, check_type,
description, remediation_hint`. An `automated` rule needs a matching function in
the `AUTOMATED_CHECKS` registry; if one is missing the engine emits
`needs_human` rather than failing the run.

## Status: real vs. stubbed

| Component | Status |
|---|---|
| SMART discovery, PKCE (S256), FHIR fetch, automated checks | Real |
| LLM evaluator (Claude) | Real; mock returns `needs_human` |
| Web UI, run history, HTML/print export, JSON API | Real |
| OAuth token exchange inside the pipeline | **Stub** — uses `MOCK_TOKEN_RESPONSE`; live flow needs a browser redirect |
| HL7 FHIR Validator, ONC g10 Test Kit | Seams only — not yet wired |

## Docs

- `docs/architecture.md` — code layout, request flow, and where to add things
- `plan/epic-compliance-build-plan.md` — 8-week build plan (source of truth for next steps)
- `notes/` — per-day results and ADRs (`notes/decisions.md`)
