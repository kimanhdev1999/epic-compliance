# Progress

What's done, what's open, and what's next. **Every session reads this before starting
work and updates it before opening a PR** (see `CLAUDE.md`). Keep it short: one line
per item, newest first within each section. Detail belongs in `notes/decisions.md`
(ADRs), PR descriptions and `docs/ux-redesign/requirements.md`.

**Last updated:** 2026-10-27 (run report: clicking a test opens its fix screen)

## How to keep this current
- **Starting work:** read *Open* and *Next*. If your task is listed, work from that line.
- **Finishing work:** in your PR, move items you closed to *Done* with the PR number, add
  anything new you found to *Open*, and bump *Last updated*.
- **UI harness markers:** an *Open* item that the harness tracks as a `known(...)` /
  `KNOWN` entry names that marker. When the fix lands, the marker XPASSes: remove the
  marker and move the item to *Done* in the same PR.
- **Check:** `pytest` and `pytest -m ui` before every PR. The xfail count should only go down.
- **Checked automatically:**
  - `tests/test_progress.py` (runs in CI) fails when:
    - a harness `known(...)` / `KNOWN` marker isn't listed under *Open*;
    - a marker listed under *Open* no longer exists, meaning it was fixed: move the row to *Done*;
    - a *Done* row lacks a PR number;
    - *Last updated* isn't a date.
  - The CI job `progress` fails a PR that changes `epic_compliance/`, `rules/`, `tests/` or `scripts/` without changing this file. Label the PR `no-progress-update` to skip it for trivial changes.
  - Write the PR number in *Done* once the PR is open (`#NN`), not "this PR".

## Standing rules (owner's requirements)
- The UI, export and canvas never name Claude, Anthropic or a model. Use "Answer review" and "Basis". See `docs/ux-redesign/requirements.md`.
- The UI shows only results from the user's real Epic runs, never sample, mock or demo data (ADR-020 amendment, PR #35).
- One git worktree per session. Each PR targets `main`; don't stack PRs on other branches.

## Done
| What | PR / ADR |
|---|---|
| Run report: clicking a test opens its fix screen (how to fix + evidence) instead of expanding details in place; `/tests?open=<id>` opens the drawer again (also fixes Re-check this test) | #42 |
| A re-check's run page shows just that test, before → after; History marks re-checks; a re-check no longer drops a run saved while it ran | #41 |
| docs/progress.md checked on every PR: CI job plus sync tests with the UI harness | #39 |
| No real health system named anywhere: examples, canvas boards, tests and docs use "Example Health" | #38 |
| Test detail redesigned (reason box, plain observations, Technical details); no vendor names in the UI | #36 |
| FHIR validator starts with the app; write checks (CLIN-005/006/009, WRITE-001..004) give real verdicts | #37, ADR-021 |
| UI never shows sample data; real runs no longer say `[MOCK]` or "mock/sandbox" | #35 |
| Plan page offers only the Epic run; test patient defaults to Camila Lopez (`identifier=203713`); `/launch` no longer dead-ends | #34, ADR-020 |
| 141 reviewer questions (Epic, HIPAA, HECVAT, OWASP MASVS, FHIR, FDA, ONC (b)(11)) run as tests | #34, ADR-019 step 1 |
| Tests that don't apply to the described app are hidden everywhere | #31 |
| Re-run a single test from its panel | #29 |
| "Where to fix it" on every result | #30, ADR-018 |
| One git worktree per session | #27 |
| Browser UI harness with honesty rules (`pytest -m ui`) | #26 |

## Open
Sources: the two UX reviews of 2026-10-20 (Epic-reviewer and first-time-developer personas), plus
the harness. **P0** = misleading or blocking, **P1** = confusing, **P2** = polish.

| Pri | Item | Where | Harness marker |
|---|---|---|---|
| P0 | Readiness % counts Epic-server and tool-login checks (headline says 80%; the app's own checks give ~43%) | `viewmodel.py`, `workspace._progress`; planned in ADR-017 | `test_readiness_counts_only_your_app` |
| P0 | AUTH-006 always passes on live runs (`state_validated` is hard-coded from the tool's own callback) | `api.py` callback | — |
| P0 | Green "No failures" while items still need review | `_report_body.html` | `test_no_failures_only_when_nothing_is_open` |
| P0 | Offline chat records "look up a patient" in a clinician app as patient users, which picks the wrong launch profile | `intake_chat.py` `_parse` | `test_chat_hears_clinicians` |
| P0 | Export reads as certification ("… Compliance Report") and drops the what-this-tests labels | `export.html` | `test_export_doesnt_claim_certification`, `test_export_says_what_each_check_tests` |
| P0 | No "register your app on fhir.epic.com" step (redirect URI, APIs to enable, sync delay); callback errors aren't explained | plan page, `_callback_result.html` | — |
| P0 | US Core read checks (FHIR-001..003) pass on field presence alone; recheck now that the validator runs (#37) | `automated_checks.py` | — |
| P1 | Too many reviewer questions (~130 of 141 for a mobile photo app): the chat doesn't ask about AI image analysis or storing PHI outside Epic, and questions aren't grouped | ADR-019 steps 2–3 | — |
| P1 | Reviewer questions aren't tailored by Claude yet (select, reword, add "AI-suggested") | ADR-019 step 2 | — |
| P1 | Reviewer-question sources to confirm: Joint Commission NPSG (×2), HITRUST, skin-imaging consensus, DICOM, FDA AI draft; consent and EXIF questions need a US source | `rules/reviewer/questions.json` | — |
| P1 | Library gaps: accessibility (VPAT/WCAG), 42 CFR Part 2, state privacy laws, FTC Health Breach Notification Rule, support/SLA/downtime | `rules/reviewer/questions.json` | — |
| P1 | "How to fix" shown on passed checks | `_macros.html`, `export.html` | `test_how_to_fix_only_on_open_items` |
| P1 | Export says "Needs Human" (UI says "Needs review") | `export.html` | `test_one_name_for_each_status[…print]` |
| P1 | A finding can't be traced to the raw request/response (method, URL, status, redacted body) | `models.Finding`, report | — |
| P1 | Attested and AI-judged passes count toward readiness like verified ones | scoring | — |
| P1 | Home doesn't move the user forward after the chat; there's no "ready to submit" end state | `home.html`, `workspace.py` | — |
| P1 | Sign-in step gives no sandbox credentials link | `plan.html` | — |
| P2 | Export page scrolls sideways 2px on phones | `export.html` | `KNOWN[("/runs/{run}/print", "phone")]` |
| P2 | A sample run ticks Connect / Run / Fix on Home (harness only now; sample runs are hidden in the app) | `workspace.py` checklist | `test_checklist_ignores_sample_runs` |
| P2 | ONC test kit shows "Configured" without checking it's reachable | `api._integrations` | — |
| P2 | A run is one blocking POST with only a spinner (no progress) | plan page; ADR-017 phase 4 | — |
| P2 | Evidence form saves the statement even when the file is rejected | `api.add_evidence` | — |
| P2 | `data/runs.db` is tracked in git despite `.gitignore` | repo | — |

## Next
1. Fix the chat's clinician/patient misread, and add the two missing intake questions (AI image analysis, PHI stored outside Epic). This cuts the reviewer-question count.
2. ADR-019 step 2: Claude tailors the plan, with plan evals (a mobile lesion-photo app must get camera roll, EXIF/GPS, FDA status, patient ID and body site).
3. ADR-017 phase 1: split readiness into "Your app" and "Epic environment"; gate "No failures"; export title and labels.
4. Update the design canvas with the AI test plan (row 4 boards) once the plan screens settle.
