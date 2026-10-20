# UX redesign: one profile, one connection, one run, one report

**Live canvas:** https://claude.ai/artifact/AzaXJ7sNMaQ4p2iuvFcu6R (private; share it from the page's Share menu)
**Decision:** ADR-017 in `notes/decisions.md`
**Status:** Proposed. No code changes yet; the release plan is on board "Spec 4".

The files in `canvas/` are the canvas's source: one `.dc.html` per artboard plus
`canvas.json` (layout). They are kept here so the design is versioned next to the
code it describes. Open the live canvas to view them.

## Why

A walkthrough of the current app (empty database → DermScan run → evidence → re-run)
found that the engine is honest but the experience isn't clear:

- Chat corrections are ignored by Re-run, Home and the plan page (Re-run uses `last_intake`).
- Epic can be connected from three places, and they behave differently. The checklist
  says "connected" with only a client ID saved.
- Results show raw dicts, `[MOCK]` and `[Errno 61]`, and the export carries them to reviewers.
- Readiness is lifted by tests that only test Epic.
- The same idea goes by run, assessment, report, plan, check, rule and finding.
- There are dead ends: the "Backend service" starter loops forever, the out-of-date
  banner stays after a re-run, missing pages return raw JSON 404s, and proof can half-save.

## What changes

1. **App profile is the source of truth.** It is versioned and editable; the assistant
   chat fills it in. Re-run always uses the current profile and shows the plan diff first.
2. **One "Connect to Epic" flow, reused everywhere.** It covers:
   - the sign-in address to register on Epic, with a copy button;
   - the APIs the plan needs;
   - the scopes Epic granted;
   - when the sign-in expires.
3. **Every "Needs input" result names its reason and gives one action:** Answer a question,
   Add proof, Connect a service, Refine test patient, or Verify outside the app.
   Raw text moves behind "Technical details".
4. **Two numbers and a verdict.** "Your app" readiness is the headline. "Epic environment"
   is shown separately. A one-word verdict sits on top: Blocked, Fixes needed,
   Missing proof or Ready to submit.
5. **One glossary:** App profile, Plan, Test, Run, Result, Proof, Report, Connection.
6. **A report a reviewer can read cold.** It has a cover, scope, method and limits,
   and a proof index with SHA-256 hashes. Sample-data runs are watermarked.

## Boards

| Board | File |
|---|---|
| Spec 1 · Principles, problems → fixes, IA, glossary | `Main.dc.html` |
| Spec 2 · Journey and Epic connection states | `Spec-Journey.dc.html` |
| Spec 3 · Business logic (entities, plan selection, results, scoring, staleness, runs, proof, report, a11y) | `Spec-Rules.dc.html` |
| Spec 4 · Release plan, old → new routes, acceptance checks | `Spec-Roadmap.dc.html` |
| 1.1 Overview, first visit | `Welcome.dc.html` |
| 1.2 Describe your app (assistant) | `Assistant.dc.html` |
| 1.3 App profile | `Profile.dc.html` |
| 1.4 Connect to Epic | `Connect.dc.html` |
| 1.5 Run setup (pre-flight + plan) | `RunSetup.dc.html` |
| 2.1 Run in progress | `RunProgress.dc.html` |
| 2.2 Overview, with results | `Overview.dc.html` |
| 2.3 Tests | `Tests.dc.html` |
| 2.4 Test detail | `TestDetail.dc.html` |
| 2.5 Overview, phone | `OverviewPhone.dc.html` |
| 3.1 Runs (history and compare) | `Runs.dc.html` |
| 3.2 Readiness report | `Report.dc.html` |
| 3.3 Connections and settings | `Connections.dc.html` |
| 3.4 Errors, empty and edge states | `States.dc.html` |

## Release plan (summary)

| Phase | Size | Theme | Scope |
|---|---|---|---|
| 1 | ~1 week | Say it clearly | Glossary rename; `summary` and `needs_reason` on results; split readiness; remove the Backend starter; HTML 404s; atomic proof save; staleness counts in-plan tests only. |
| 2 | 1–2 weeks | One source of truth | Versioned `profiles` table; App profile page; the assistant writes to it; Re-run uses the current profile and shows the plan diff. |
| 3 | 1 week | One connection | Connect to Epic flow; persisted connection metadata and expiry; test patient "Check"; Connections replaces Integrations and Settings. |
| 4 | 1–2 weeks | Run & share | Run setup page; background run with progress; new Overview; report redesign and PDF; compare runs. |
| Later | — | ADR-009 steps 2–4 | Verify mode; app probes; submission evidence pack. |
