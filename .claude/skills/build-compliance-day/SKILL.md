---
name: build-compliance-day
description: Execute one day of the Epic Compliance Tool build plan end-to-end — find the next unchecked day, read prior notes, implement it, verify, then record results and tick the plan. Use when the user says "do day N", "next day", "continue the build plan", "what's next on the compliance tool", or names a plan day by number. For freeform building that ignores the day pacing, use the epic-compliance-builder agent instead; for smoke-testing only, use verify-epic-compliance.
---

# Build one day of the Epic Compliance Tool

The plan at `plan/epic-compliance-build-plan.md` is the source of truth for scope
and ordering. One day ≈ 1–2 hours of work. Do **one** day per invocation unless
the user explicitly asks for more — the pacing is deliberate, and a day that
quietly absorbs the next three days' work leaves the plan lying about progress.

## The loop

### 1. Orient (always, before writing code)
- Read `plan/epic-compliance-build-plan.md`; find the first `[ ]` day, or the day
  the user named.
- Read `notes/decisions.md` for the ADRs — stack choices are already made, don't
  re-litigate them.
- Read `notes/day{N-1}-results.md` and any topic notes touching the same area.
- **Read the code that already exists for this area.** The plan is aspirational;
  the repo is real. Days get marked `[x]` with stubs left behind (see
  "Known stubs" below). Verify the prior day's work is actually live before
  building on top of it.

### 2. Build
Implement that day's bullet, and only that bullet. Where the day's work touches
an existing stub, replacing the stub *is* the work — do not layer new code on top
of a mock that still short-circuits the real path.

### 3. Verify
Run the harness — do not hand-roll ad-hoc checks:
```bash
./scripts/verify_tool.sh          # after any code change
./scripts/verify_tool.sh --quick  # trivial changes only
```
`fail=N / needs_human=N` in the pipeline output is normal tool *output* (findings
the tool is designed to surface), not a harness failure. Exit 1 from the CLI run
is expected when there are `fail` findings.

If the day added a new surface (live OAuth, HL7 validator, HTML export), add a
check for it to `scripts/verify_tool.sh` so `verify-epic-compliance` keeps
covering the whole tool.

### 4. Record
- Write `notes/day{N}-results.md`: what was built, raw command output, what broke
  and why, what's still stubbed.
- Any architectural decision → append an ADR to `notes/decisions.md` with the
  reasoning, not just the choice.
- Tick the day `[x]` in the plan — **only if it genuinely works.** A day left at
  `[ ]` with a note explaining the blocker is more useful than a false `[x]`.
- Buffer days (7, 14, 21…) are for catch-up. If earlier days slipped, spend the
  buffer rather than compressing new work.

## Invariants (these outlive any single day)

- **Never build a FHIR validator or conformance engine.** Wrap the HL7 validator
  and the ONC (g)(10) Test Kit. This repo is orchestration + the Epic layer + the
  LLM evaluator + a thin UI. Nothing else.
- **Every finding is traceable**: `{rule_id, verdict, evidence}` minimum. No bare
  booleans, no verdict without the evidence it was derived from.
- **LLM verdicts must cite evidence** and always keep the `needs_human` escape
  hatch. An LLM asserting compliance with no citation is a bug.
- **A mock must never produce a `pass` in live mode.** If `run_mode == "live"` and
  a stage fell back to a mock fixture, the finding is `needs_human`. Silent fake
  passes are the single most dangerous failure mode in a compliance tool — it will
  tell you you're certified when nothing was checked.
- **Keep the UI ugly.** Functional-and-ugly ships. Do not gold-plate templates.
- Never commit credentials or `.env`. `.env.example` documents keys, never values.

## Known stubs (as of Day 12 — update as they're closed)

Grep for `STUB` before trusting any pipeline stage. Current ones:
- `pipeline.py` — `token = MOCK_TOKEN_RESPONSE`, hardcoded in **both** mock and
  live mode. Means `AUTH-003` / `AUTH-005` in `rules/auth.json` cannot currently
  fail. Closing this is Day 13.
- `smart/pkce.py` — `exchange_code_for_token()` is written but never called by
  the pipeline; `build_authorization_url()` omits the `aud` parameter, which Epic
  requires (launches are rejected without it).
- `api.py` — no `/callback` route, despite `config.py` declaring
  `oauth_redirect_uri = http://localhost:8000/callback`.

## Environment

```bash
source .venv/bin/activate          # first time: python3 -m venv .venv && pip install -e '.[dev]'
python -m epic_compliance run      # CLI pipeline
uvicorn epic_compliance.api:app --reload   # dashboard
pytest tests/
```

ONC (g)(10) Test Kit is a **separate repo** at
`/Users/belleasia/BelleGitRepos/HealthcareApp/onc-certification-g10-test-kit`,
served at `http://localhost:8080`. It uses **Podman**, not Docker —
`podman compose up -d` / `podman compose down`. Read `notes/docker-setup.md`
before any container command. Do not edit that repo unless told to.

## Epic sandbox reference

```
FHIR base:  https://fhir.epic.com/interconnect-fhir-oauth/api/FHIR/R4/
Discovery:  {base}/.well-known/smart-configuration
```
Sandbox test credentials and client registration live on fhir.epic.com and rotate
— confirm there rather than trusting a value hardcoded in this repo.

## Report back

What was built, how to run it, what the verify harness said, and what is still
stubbed. Keep it short. If the day is blocked, say what's blocking it and what
you did instead.
