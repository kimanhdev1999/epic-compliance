# Epic Compliance Verification Tool — Solo Build Plan

> [!info] The premise
> Solo, **1–2 hrs/day, 7 days/week** ≈ **~105 hours** over 8 weeks. Scope is cut to fit: automated conformance + LLM-assisted manual checks, **Epic only**. The multi-system "any healthcare system" goal is architecture-only for now.

> [!tip] Survival rules
> - The weekly **buffer day** (Day 7, 14, 21…) is not optional — it absorbs whatever slipped.
> - **Biggest risk is Week 1**: Epic sandbox credentials may involve a wait on Epic's side. Start registration Day 1, do the reading days while you wait.
> - **Don't build a FHIR validator or conformance engine** — wrap the ONC (g)(10) Test Kit + the HL7 FHIR validator. Your hours go into orchestration, the Epic layer, the LLM evaluator, and a thin UI.
> - Keep the dashboard deliberately ugly. Functional-and-ugly ships; pretty-and-late doesn't.

> [!warning] What ~105 solo hours actually delivers
> The automated engine + LLM evaluator + a working report against your own app. The dashboard may stay rough; the multi-system abstraction will be architecture-only. That's a genuinely useful MVP: it tells you what's wrong with your app, fast. It does **not** guarantee Epic approval — Epic's review includes human judgment and items not in any public doc.

---

## 🗂️ Key Resources

- **[fhir.epic.com](https://fhir.epic.com)** — Epic FHIR docs, supported resources, sandbox
- **[Epic Showroom / vendorservices](https://vendorservices.epic.com)** — listing + partnership requirements (account needed)
- **[ONC (g)(10) Test Kit](https://github.com/onc-healthit/onc-certification-g10-test-kit)** — `onc-healthit/onc-certification-g10-test-kit` — the current (g)(10) test suite (v8.0.2 as of May 2026), built on the Inferno Framework. Replaces the legacy `inferno-program` repo. Public hosted instance at [inferno.healthit.gov/suites/g10_certification](https://inferno.healthit.gov/suites/g10_certification)
- **[Inferno Framework](https://inferno-framework.github.io/)** — `inferno-framework/inferno-core` — the reusable engine the (g)(10) Test Kit runs on top of
- **[US Core IG](https://hl7.org/fhir/us/core)** — must-support elements per resource (v3.1.1, v4.0.0, v6.1.0, v7.0.0)
- **[SMART App Launch IG](https://hl7.org/fhir/smart-app-launch)** — v1.0.0, v2.0.0, v2.2.0

> [!note] Inferno vs ONC (g)(10) Test Kit — clarification
> These are **not** competing tools. ONC split the original monolithic `inferno-program` into two things: the **Inferno Framework** (reusable engine) and the **(g)(10) Test Kit** (the actual certification tests, now a "test kit" that runs on top of the framework). The (g)(10) Test Kit README explicitly states it is built using the Inferno Framework. Always reference `onc-healthit/onc-certification-g10-test-kit` — it is actively maintained (v8.0.2, May 2026) and supports US Core v6.1.0/v7.0.0 and SMART App Launch v2.2.0.

---

## Week 1 — Acquire requirements + stand up the tools

- [x] **Day 1.** Register on fhir.epic.com, request sandbox credentials, read the "Epic on FHIR" getting-started + supported FHIR resources list. Bookmark everything.
- [x] **Day 2.** Read the SMART App Launch IG (launch sequence, scopes) and skim US Core IG structure. Take notes, don't memorize.
- [x] **Day 3.** Install the ONC (g)(10) Test Kit locally via Docker (`./setup.sh && ./run.sh`). Navigate to `http://localhost`. Optionally smoke-test against the public instance at inferno.healthit.gov first.
- [ ] **Day 4.** Run the (g)(10) Test Kit suite against Epic's public sandbox. Watch what passes/fails — this is your gold reference.
- [ ] **Day 5.** Read 3–4 test definitions in the (g)(10) Test Kit source. Understand how a check is structured (tests are Ruby-based test groups in the Inferno Framework).
- [ ] **Day 6.** Draft the rule-catalog schema (`id`, `category`, `source`, `severity`, `evidence_needed`, `check_type`). One JSON file.
- [ ] **Day 7.** Seed 15–20 rules into the catalog from this week's notes (auth, top US Core resources). *Buffer / catch-up.*

## Week 2 — Project scaffold + SMART/OAuth validator

- [ ] **Day 8.** Scaffold repo: backend service (your strength), config, `.env` for sandbox creds. Pick a stack you know.
- [ ] **Day 9.** Implement `.well-known/smart-configuration` discovery fetch + parse.
- [ ] **Day 10.** Implement the OAuth 2.0 authorization-code + PKCE flow against the sandbox.
- [ ] **Day 11.** Validate token response: scopes granted vs requested, `id_token`, expiry.
- [ ] **Day 12.** Turn each auth check into a structured finding `{rule_id, verdict, evidence}`.
- [x] **Day 13.** Test the full SMART launch end-to-end against sandbox. Fix breaks.
- [x] **Day 14.** Write tests for the auth module. *Buffer / catch-up.* — also closed the Day 13 gaps (live-mode false pass, `aud`) and added AUTH-006..010. See `notes/day14-results.md`.

## Week 3 — FHIR conformance via existing validators

- [ ] **Day 15.** Wire in the HL7 FHIR validator (CLI/library) — don't build your own.
- [ ] **Day 16.** Fetch a Patient resource from sandbox, validate against US Core profile.
- [ ] **Day 17.** Extend to 4–5 core resources (Observation, Condition, etc.).
- [ ] **Day 18.** Map validator output to your structured findings format.
- [ ] **Day 19.** Decide (g)(10) Test Kit integration: invoke its API programmatically vs port key checks. Implement the simpler one.
- [ ] **Day 20.** Run combined auth + FHIR checks in one pass. Consolidate findings.
- [ ] **Day 21.** Test against sandbox, fix gaps. *Buffer / catch-up.*

## Week 4 — Epic-specific layer + ingest YOUR app

- [ ] **Day 22.** Add Epic-specific checks the (g)(10) Test Kit misses: Epic's supported-resource list, required scopes for Showroom.
- [ ] **Day 23.** Build a config ingester: point the tool at your real app's FHIR endpoint + launch URLs.
- [ ] **Day 24.** Run the full engine against YOUR app for the first time. Capture raw results.
- [ ] **Day 25.** Triage real failures — separate true bugs from tool gaps.
- [ ] **Day 26.** Fix the highest-severity findings in your app OR refine rules causing false positives.
- [ ] **Day 27.** Add mobile-specific checks (redirect URI, PKCE handling for the mobile client).
- [ ] **Day 28.** Re-run, confirm improvement. *Buffer / catch-up.*

## Week 5 — LLM evaluator for manual checks (steps 1 & 4)

- [ ] **Day 29.** Draft security/listing rules (HIPAA in-transit/at-rest, audit logging, BAA ref, privacy-policy disclosures). LLM-assisted, you freeze them.
- [ ] **Day 30.** Build the evidence collector: map your AWS/app configs to each rule's `evidence_needed`.
- [ ] **Day 31.** Build the LLM evaluator: requirement + evidence in, strict JSON verdict out (`pass`/`fail`/`needs_human`).
- [ ] **Day 32.** Add the `needs_human` escape hatch + citation-to-evidence requirement. Test on 3 rules.
- [ ] **Day 33.** Run evaluator across all security rules using your real configs.
- [ ] **Day 34.** Review verdicts manually — calibrate prompts where the LLM over/under-flags.
- [ ] **Day 35.** Tag findings: `automated` / `llm-assessed` / `needs-human`. *Buffer / catch-up.*

## Week 6 — The "what's wrong, fast" dashboard

- [ ] **Day 36.** Minimal UI scaffold (keep it thin — not your strength, don't gold-plate).
- [ ] **Day 37.** Findings list grouped by category with pass/fail/needs-human status.
- [ ] **Day 38.** Each failure: requirement text + source citation + plain-language reason.
- [ ] **Day 39.** Add remediation hint per failure + severity sort.
- [ ] **Day 40.** Wire dashboard to live engine output (not mock data).
- [ ] **Day 41.** Run full pipeline → dashboard against your app, watch it work.
- [ ] **Day 42.** Polish only what blocks understanding. *Buffer / catch-up.*

## Week 7 — Harden, architect for "go global later"

- [ ] **Day 43.** Refactor rule catalog into: generic engine + Epic adapter (so other systems plug in later).
- [ ] **Day 44.** Document the adapter interface — even a stub for one other system proves the design.
- [ ] **Day 45.** Error handling: bad creds, network, malformed FHIR, LLM timeouts.
- [ ] **Day 46.** Secrets handling — no creds in code, use env/secrets manager.
- [ ] **Day 47.** Full regression run against sandbox + your app.
- [ ] **Day 48.** Fix the bug backlog from the week.
- [ ] **Day 49.** *Buffer / catch-up — you will need this by now.*

## Week 8 — Report export, docs, final validation

- [ ] **Day 50.** Exportable report (HTML/PDF) of all findings, suitable to hand your team or Epic.
- [ ] **Day 51.** README + how-to-run docs.
- [ ] **Day 52.** Run against your app, generate the real report. This is the deliverable.
- [ ] **Day 53.** Address remaining true failures in your app from the report.
- [ ] **Day 54.** Final end-to-end test, clean up.
- [ ] **Day 55.** Write the "deferred / v2" list (multi-system, deeper Epic review).
- [ ] **Day 56.** Slack day. Ship. Or recover the inevitable slipped days.
