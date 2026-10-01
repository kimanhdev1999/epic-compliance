# Scenario: HL7 v2 ORU^R01 inbound results interface — scope check

## The flow being evaluated

1. Doctor places an order in Epic ("AI skin image analysis").
2. Epic sends the order out via **HL7 ORM/OMG** (v2) or **FHIR `ServiceRequest`**.
3. The app captures the photo, runs AI, produces a result.
4. The app sends **HL7 v2 ORU^R01** back to Epic Bridges, carrying the
   placer order number so Epic attaches the result to the right order/patient.
5. The result shows up in Results / Chart Review, routes to In Basket for the
   doctor to review/sign. From there, *other* apps can read it as FHIR
   `DiagnosticReport`.

## Verdict: this compliance tool does NOT verify this flow

Nothing in `rules/` (`auth.json`, `fhir_resources.json`, `security.json`,
`write_back.json`) or in `epic_compliance/validator/` touches HL7 v2 or
Epic Bridges. The tool's two covered directions are both FHIR-only:

- **Direction A (read):** app reads FHIR resources from Epic via SMART —
  `FHIR-00x` rules, `fhir_resources.json`.
- **Direction B (write-back):** app POSTs `DiagnosticReport`/`Observation`/
  `Media` as FHIR R4 directly into Epic via SMART write scopes —
  `WRITE-00x` rules, `rules/write_back.json`, see
  `notes/day-writeback-results.md`.

The ORU^R01 flow above is a **third, distinct integration path** (an HL7 v2
MLLP interface through Epic Bridges) that this tool has no rules, no
validator, and no test-kit coverage for. The ONC (g)(10) test kit at
`localhost:8080` is also irrelevant here — it certifies SMART on
FHIR/US Core export, not inbound v2 interfaces.

## Scope matrix

| Step in the flow | Standard | Covered by this tool? | Why / why not |
|---|---|---|---|
| 1. Order placed in Epic | internal Epic workflow | N/A | Nothing to verify from outside |
| 2. Order sent out (ORM/OMG v2 **or** FHIR `ServiceRequest`) | HL7 v2 or FHIR R4 | **No** (v2 case) / **Partially possible** (FHIR case, but not built) | No `ServiceRequest` rules exist in `fhir_resources.json` today; would need a new rule category if the app also *reads* orders |
| 3–4. App generates result, sends ORU^R01 to Bridges | HL7 v2.x MLLP | **No** | Out of scope by design — this tool only speaks FHIR/SMART, never HL7 v2/MLLP. Would need a wholly separate HL7 v2 test harness (different protocol, different cert path) |
| 5. Result lands in Chart Review / In Basket | internal Epic | N/A | Can't observe from outside Epic |
| 6. Other apps read result via FHIR `DiagnosticReport` | FHIR R4 | **Yes** — this is Direction A | Same `FHIR-00x` checks that already cover reading any US Core resource apply here unchanged |

## Test case: TC-SCOPE-001 — tool correctly reports "not applicable" for v2 ORU

**Given** a user runs this compliance tool against an integration that uses
HL7 v2 ORU^R01 over Bridges for result delivery (no FHIR write-back for
results),
**when** they look for which rule proves the result was correctly delivered
and linked to the order,
**then** they should find **no rule claims this** — `WRITE-00x` only proves
FHIR POSTs succeeded, which this flow never performs. The absence of a
WRITE-00x finding must not be misread as "compliant"; it means "not
evaluated, different protocol."

**Pass condition for this test case:** a human reviewing the tool's output
report does NOT see any ORU-related claim marked `pass`, and this doc (or
an equivalent note in `notes/decisions.md`) exists so nobody assumes
coverage that isn't there.

**Fail condition:** if a future rule is accidentally written that claims to
validate "result delivery" generically and would silently `pass` for an
ORU-based integration without ever inspecting a v2 message — that's a false
positive and must be fixed/scoped out immediately.

## Test case: TC-SCOPE-002 — hybrid flow, FHIR half is still checkable

**Given** steps 1–5 use HL7 v2 end-to-end, but step 6 (another app reading
the signed result) uses FHIR `DiagnosticReport`,
**when** that *reading* app is the one being compliance-tested (not the
app that generated the result),
**then** the existing Direction A `FHIR-00x` checks against
`DiagnosticReport` **do apply and are sufficient** — the tool has no need to
know or care that the underlying result originated from an HL7 v2 interface
upstream.

## If HL7 v2 coverage is ever wanted

This would require, at minimum:
- An MLLP listener/sender test harness (TCP, not HTTP) — completely
  different stack from `httpx`/FastAPI used today.
- A v2.5.1 message parser/validator (e.g. HAPI v2, or Epic's own Bridges
  validation specs) — different from the FHIR validator already wired up
  per `notes/day15-results.md`.
- A new rule category (e.g. `rules/hl7v2_oru.json`) with its own
  `check_type`s, separate from `FHIR-00x`/`WRITE-00x`.
- Access to an actual Bridges interface engine or Epic's v2 conformance
  test environment — the ONC (g)(10) kit does not provide this.

This is a **new build track**, not an extension of existing rules. Not
started; no day in `plan/epic-compliance-build-plan.md` currently covers it.
