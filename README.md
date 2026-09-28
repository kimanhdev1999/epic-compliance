# Epic Compliance Tool

Automated **ONC (g)(10) compliance verification for Epic FHIR apps** — for teams
building a third-party app (mobile or web) that needs to integrate with Epic and
survive Epic's marketplace review.

It connects to a FHIR server (Epic sandbox by default), collects evidence
(SMART configuration, OAuth token, US Core resources), runs a catalog of
compliance rules against that evidence — deterministic checks plus
LLM-assisted checks for narrative/security requirements — and produces a
structured findings report you can browse, export, or consume as JSON.

Every finding is one of `pass` / `fail` / `needs_human`, with the evidence that
produced it, a source citation (SMART IG / ONC §), and a remediation hint.
`needs_human` is a first-class verdict: the tool never guesses.

## Who this is for — worked example

Say you have a **mobile app that photographs a patient's skin and flags possible
lesions**, and you want it listed on Epic's app marketplace so hospitals can
install it. Epic will expect your app to talk to their FHIR API the standard
way: SMART on FHIR launch, OAuth2 + PKCE, US Core–shaped reads, and defensible
security practices around the PHI you touch.

This tool answers one question: **is your app's Epic integration layer actually
conformant today, and if not, exactly what is broken?** Point it at the Epic
sandbox (or a customer's FHIR base URL) with your registered client id, run it,
and you get a per-requirement verdict instead of a vague "it seems to work."

For the skin app, a run tells you concretely:

- Your app can launch standalone and in-EHR, and PKCE S256 is negotiated correctly (`AUTH-001…005`) — the launch path Epic requires of a mobile client.
- The patient context handed to your app is real and usable: you get a `patient` id, `openid`/`fhirUser` claims, and at least `patient/*.read` scope, so the photo you capture can be attached to the right person (`AUTH-003`, `AUTH-005`).
- The clinical data you read back to give the model context — demographics, `Condition`, `Observation` — conforms to US Core and declares its profiles, so your feature won't break on the next customer's Epic build (`FHIR-001…005`).
- Your PHI posture gets stated and judged, not assumed: TLS 1.2+, audit logging on every PHI access, and a BAA with Epic and every subprocessor that sees an image (`SEC-001…003`). If the image goes to a third-party inference API, that vendor needs a BAA — this is the check teams most often discover too late.

**Just as important, what it does _not_ decide.** It is a conformance and
evidence tool, not a market-approval oracle:

- It has no opinion on your model. Clinical validity, sensitivity/specificity, bias across skin tones, and whether lesion-flagging makes you an FDA-regulated device (SaMD) are all out of scope.
- It doesn't audit your app's source, mobile storage, or key handling. The `SEC-*` rules judge the evidence you give them and return `needs_human` when that evidence is thin — by design.
- It doesn't cover writing results back (e.g. `DocumentReference`/`Media` for the image, or a note into the chart). Today's rules are read-path only.
- It is not Epic's review. Epic runs its own vendor, security, and business process, and their published requirements are the authority — treat a clean run as evidence you bring to that review, not a substitute for it.

Practical way to use it: run it in mock mode to see the rule set, then run it
live against the Epic sandbox with your client id before you submit, and keep
running it in CI so a regression in your launch or scopes fails the build.
(Caveat: the live token exchange is still stubbed — see the status table below.)

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
