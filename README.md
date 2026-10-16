# Epic Compliance Tool

**Get your app integrated with Epic faster.** Describe your app, get the exact
set of Epic integration checks it needs, and see what is blocking go-live —
ONC (g)(10), SMART on FHIR, US Core, and the security questions a health
system's review will ask — before you sit down with their Epic team.

It connects to a FHIR server (the Epic sandbox, or offline demo data), collects
evidence (SMART configuration, OAuth token, US Core resources, the resources
your app writes), runs the checks that apply to your app — deterministic
probes plus LLM-assisted review of your security answers — and produces a
prioritized findings report you can browse, export, or consume as JSON.

Every finding is one of `pass` / `fail` / `needs_human` ("needs review"), with
the evidence behind it, a source citation (SMART IG / ONC §), and how to fix
it. `needs_human` is a first-class verdict: the tool never guesses.

## How getting into Epic actually works

You do **not** need a public marketplace listing to go live with one health
system. Epic's App Orchard is gone (renamed App Market in 2021, closed
December 2022); its successor, **Epic Showroom**, is optional and only adds
visibility. For an app used by a single health system the path is:

1. **Register the app** at fhir.epic.com (open.epic). Free; you get
   non-production and production client IDs and the public sandbox.
2. **The health system activates it.** Their Epic analysts enable your client
   ID in their Epic instance and approve the scopes it requests. This — not a
   marketplace — is what turns you on.
3. **Pass their review.** Security / vendor-risk questionnaire, a BAA, and a
   contract.
4. *(Optional, later)* a Showroom **Connection Hub** listing once you're live,
   or **Vendor Services** if you need APIs beyond standard FHIR.

This tool targets steps 1–3: prove your integration works against the Epic
sandbox, then bring the report to the health system's Epic and security teams
as evidence. Confirm the activation steps with that health system's Epic
team — each one runs it slightly differently.

## The flow

Modeled on cloud and AI security-audit tools (AWS Security Hub, Prowler, and
the like): you describe what you're running, the tool decides what to check,
and the result is a triaged list of what to fix.

1. **Describe your app** — in a chat, in your own words. The tool asks
   follow-up questions until it knows who signs in (clinicians or patients),
   what kind of app it is (mobile, browser-only, web with a backend), what it
   does with patient data (reads the chart, saves photos, writes notes/results
   back, sends PHI to a third-party AI), and your security posture (TLS, audit
   logging, BAA — "not sure" is fine). It then shows what it understood for
   you to confirm or correct. With `ANTHROPIC_API_KEY` set, Claude runs the
   conversation; without one, a keyword parser with scripted follow-ups does,
   so it works offline too.
2. **Review the test plan** — the checks your answers call for, each with
   *why* it's included, plus the checks skipped as not applicable — and pick
   where to test (demo data or the Epic sandbox). Nothing has run yet.
3. **Fix findings** — a readiness score, failures by severity, a
   **Fix these first** list with the remediation for each, a **Needs your
   review** list for anything the evidence can't decide, and every finding
   filterable by status. Export as HTML, PDF or JSON for the health system.

The chat lives in `epic_compliance/intake_chat.py`; the plan logic in
`epic_compliance/assessment.py` (`SELECTORS` maps each
rule to the answers that pull it in).

## Who this is for — worked example

Say you're building **DermScan**, a mobile app a dermatologist at
**Advocate Health** uses to photograph a lesion, file the photo in the
patient's chart, and get an AI read of it.

Tell the chat: *"A mobile app dermatologists at Advocate Health use to
photograph a lesion, save the photo to the chart and get an AI read."* It
picks up clinicians, mobile, photos and a third-party AI, asks about your
security setup, and the plan comes back with checks including:

- **Sign-in** — Epic's SMART endpoints, PKCE S256, state handling, code replay, id_token signature (`AUTH-*`). Your app must be a public client with a deep-link redirect, because a phone can't keep a secret (`CLIN-011`, `CLIN-012`).
- **Right patient, every time** — the clinician signs in with no patient, searches, and confirms one; the search must match exactly one patient and show enough to confirm them (`CLIN-001`, `CLIN-002`). Everything written must land on that patient (`CLIN-007`).
- **The photo** — it must validate, be accepted by Epic, and read back so the care team can open it (`CLIN-005`, `CLIN-006`).
- **Chart reads** — problems, meds and allergies are allowed, and US Core data is shaped the way your parser expects (`CLIN-008`, `FHIR-*`).
- **Security** — TLS 1.2+, audit logging of PHI access, and a BAA with Advocate *and* the AI vendor that sees the image (`SEC-*`). That last one is the check teams most often discover too late.

Write-back checks for notes, and the patient-app checks (`WRITE-*`,
`AUTH-009`), are skipped — they don't apply to this app — and the report says
so.

**What it does _not_ decide.** It is a conformance and evidence tool, not an
approval oracle:

- It has no opinion on your model: clinical validity, bias across skin tones, and whether you're an FDA-regulated device (SaMD) are out of scope.
- It doesn't audit your source code, mobile storage or key handling. `SEC-*` rules judge the answers you give and return `needs_human` when they're thin.
- It is a client-side check: it verifies the requests your registration can make. It does not host or validate a FHIR server of your own.
- It is not the health system's review. Their Epic, security and vendor-risk teams decide; a clean run is evidence you bring to that conversation.

Practical way to use it: run it on demo data to see your plan, run it against
the Epic sandbox with your non-production client ID before you meet the health
system, and keep running the CLI in CI so a regression in your login or scopes
fails the build. (Caveat: the in-pipeline token exchange is still stubbed — see
the status table below.)

## What it gives you

- **Web UI** — the describe → plan → fix flow above, assessment history, standalone HTML export, printable/PDF report. FastAPI + Jinja + HTMX.
- **CLI** — `python -m epic_compliance run` runs every rule for the configured launch profile, prints a colored findings table, and exits non-zero on failures (CI-friendly).
- **JSON API** — `/api/run`, `/api/runs`, `/api/runs/{id}` for integration.
- **Demo (mock) mode** — the whole pipeline runs offline against fixtures, with no credentials and no API key. This is the default.

## Quick start

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env          # optional; defaults work in mock mode

python -m epic_compliance serve   # web UI at http://localhost:8000 — start an assessment
python -m epic_compliance run     # CLI run (mock)
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
| `rules/clinician_workflow.json` | 12 | automated + attested (clinician standalone: patient search/confirm, photo and note write-back, public-client registration) |
| `rules/write_back.json` | 4 | automated (Observation/DiagnosticReport/Media POST, US Core 3.1.1 shape, write-scope enforcement) |

Each rule carries `id, category, source, severity, evidence_needed, check_type,
description, remediation_hint` (plus optional `subject` and `profiles`). To
have the web flow pick a new rule up only for the right apps, add it to
`SELECTORS` in `epic_compliance/assessment.py`; a rule with no selector is
included in every plan. An `automated` rule needs a matching function in
the `AUTOMATED_CHECKS` registry; if one is missing the engine emits
`needs_human` rather than failing the run.

## Status: real vs. stubbed

| Component | Status |
|---|---|
| SMART discovery, PKCE (S256), FHIR fetch, automated checks | Real |
| LLM evaluator (Claude) | Real; mock returns `needs_human` |
| Web UI, run history, HTML/print export, JSON API | Real |
| Write-back (`Observation`/`DiagnosticReport`/`Media` POST, `/callback` route) | Real (mock + live client POST); no live sandbox run yet — see `notes/day-writeback-results.md` |
| OAuth token exchange inside the pipeline | **Stub** — uses `MOCK_TOKEN_RESPONSE`; live flow needs a browser redirect |
| HL7 FHIR Validator, ONC g10 Test Kit | Seams only — not yet wired; WRITE-001/002/003 report `needs_human` until it's running |

## Build a standalone Mac app

For non-technical users, package the tool as a double-clickable `Epic
Compliance.app` in a `.dmg` — no Python, pip, or terminal required to run it:

```bash
scripts/build_macos_app.sh
```

This creates an isolated build venv, installs the `macapp` extra
(`pyinstaller` + `rumps`), and produces:

- `dist/Epic Compliance.app` — a menu-bar app (no dock window). Launching it
  starts the server on `localhost:8000` and opens the dashboard in the
  default browser; quit from the menu-bar icon.
- `dist/Epic Compliance-<version>.dmg` — drag-to-Applications installer.

Run-history state lives in `~/Library/Application Support/Epic Compliance/`
(not inside the read-only app bundle) so it survives app updates.

The build is unsigned/unnotarized, so first launch requires right-click →
Open (or allowing it under System Settings → Privacy & Security). It ships
with `RUN_MODE=mock` defaults built in (no `.env` bundled); live-mode
credentials still need to be set as environment variables before launching
the app, or hand-configured via the web UI's per-run overrides.

## Docs

- `docs/architecture.md` — code layout, request flow, and where to add things
- `notes/` — per-day results and ADRs (`notes/decisions.md`)
