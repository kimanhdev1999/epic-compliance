# UX requirements (from the product owner)

These are standing requirements for every screen of the app, the exported report, and the
design canvas in `canvas/`. They override older wording in the canvas boards. Anyone changing
the UI (people, the `ux-flow-reviewer` agent, or any other agent) must follow them, and
`tests/ui/test_honesty.py` checks the ones a machine can.

## R1. Never mention Claude or the AI vendor

The UI never names the model or company behind the answer review.

- Don't show: "Claude", "Anthropic", model ids (`claude-sonnet-…`), "LLM", "AI citation",
  "Sent to Claude", or env var names that contain the vendor (`ANTHROPIC_API_KEY`).
- Name the capability instead:
  - **Answer review**: the check type. It replaces "AI review".
  - The review: "your answers and proof are checked against Epic's review criteria".
  - The setting: "Answer review" on Connections and Integrations. Put setup instructions in
    `README.md` / `.env.example`, not in the UI.
  - **Basis**: the short reason the review gives. It replaces "AI citation" and "LLM citation".
- Stay honest without naming the vendor. Never suggest a person reviewed something a machine
  judged: write "checked automatically", not "reviewed by an expert".
- This applies to page text, tooltips, placeholders, flash messages, exported reports,
  `<title>`s, and the canvas boards.

## R2. No sample data in the UI

Show only results from the user's real Epic runs. Never show sample or mock runs,
"Sample data" boxes or banners, `[MOCK]` text, or "demo" wording. Sample data exists only for
the test suite and the UI harness (ADR-014).

## R3. Test detail screens (the drawer and `/tests/{id}`)

Follow canvas board 2.4 `TestDetail.dc.html`, top to bottom:

1. **Header**: test id · category, the title, then pills for status, who must act, severity
   and check type.
2. **Reason box** (only when the test isn't passing). It says where to fix it, gives the
   result in one or two plain sentences, and offers **one** primary action plus at most one
   secondary action, for example "Add proof" and "Edit my answer".
3. **What we observed**: plain prose. Raw checker output (status codes, ids, validator text)
   goes in a collapsed **Technical details** block, never as the main text.
4. **Proof**: what's attached, then the add-proof form. Short, friendly copy.
5. **How to pass**, **Why it's in your plan**, **The requirement** (with source and
   frameworks), and **History**.
6. **Footer**: Re-check this test, and Full re-run.

Never render prose in a monospace `<pre>`. Never show internals (Python dicts, `[Errno …]`,
`UPPER_SNAKE` placeholders) outside Technical details.

## R4. General

- Every result that needs input gives its reason and one clear action.
- Use plain words a developer new to Epic understands. Explain Epic terms (SMART, FHIR, scopes)
  where they first appear.
- Layouts work at phone width (390 px).
