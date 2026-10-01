# Concepts: HL7 v2 ORU/ORM/OMG vs FHIR ServiceRequest

Reference doc for the terms used in the ORU inbound-results scenario
(`notes/scenario-hl7-oru-inbound-results.md`). None of this is implemented
by this tool today — it's background for understanding why.

## HL7 v2 — the older messaging standard

HL7 v2.x (most commonly v2.3–v2.5.1) is a pipe-and-hat-delimited text
message format from the 1990s, still the dominant protocol for lab/imaging/
pharmacy interfaces inside hospitals. It runs over raw TCP using **MLLP**
(Minimal Lower Layer Protocol) — not HTTP, not REST, no OAuth. This is a
fundamentally different transport from the SMART-on-FHIR/HTTPS stack this
tool is built around.

A v2 message is a trigger-event type (e.g. `ORU^R01`) followed by segments
(`MSH`, `PID`, `ORC`, `OBR`, `OBX`...), each a pipe-delimited line.

### ORM / OMG — the order message

- **ORM^O01** — General Order message (older trigger event; still common).
- **OMG^O19** — General Clinical Order message (newer HL7 v2.5.1+
  replacement for ORM in many implementations, Epic included).

Either way, this is Epic telling an external system "an order was placed."
Key fields:
- `ORC` (Common Order) segment — order control code, **placer order
  number** (`ORC-2`), **filler order number** (`ORC-3`).
- `OBR` (Observation Request) segment — what's being ordered (e.g. "AI
  skin image analysis"), ordering provider, specimen/collection info.

The **placer order number** is the critical identifier: Epic (the
"placer") generates it when the order is placed, and whatever system
performs the order (the "filler" — your app) must echo that same number
back in its result message so Epic can match the result to the original
order and patient. This is the v2 equivalent of a foreign key.

### ORU^R01 — the result message

**O**bservational **R**esult, **U**nsolicited — the message type used to
send results *back* into Epic. Segments:
- `MSH` — message header (sending/receiving application, timestamp).
- `PID` — patient identifiers (must match Epic's patient so the result
  files under the right chart).
- `ORC` + `OBR` — echoes the original order, **including the placer order
  number from the ORM/OMG**, so Epic can link the result to that order.
- `OBX` — one or more Observation/Result segments: the actual value(s)
  (e.g. AI model output, a note, an embedded image reference), value type,
  units, abnormal flags, result status (`F` = final, `P` = preliminary).

This is sent over an **MLLP TCP socket** to Epic Bridges, which is Epic's
interface engine (think: a message router/broker in front of Epic's
database) — not a REST API endpoint, so none of this tool's `httpx`-based
FHIR client code applies to it.

Why hospitals still use this for lab/imaging results instead of FHIR: it's
the long-established, near-universal interface type lab instruments, PACS,
and middleware already speak; FHIR write-back for clinical results is
newer and far less commonly supported by instrument/LIS vendors as of 2026.

## FHIR ServiceRequest — the modern equivalent of the order message

`ServiceRequest` is the FHIR R4 resource that models "a request for a
procedure or diagnostic test to be performed" — the FHIR-native alternative
to an HL7 v2 ORM/OMG order message. If Epic's order-sending side is
*FHIR-based* instead of v2 (less common for Epic Bridges-style interfaces,
more common for app-to-app FHIR integrations), the app would instead:

- Receive or poll for a `ServiceRequest` resource (status `active`,
  `intent: order`, `code` identifying "AI skin image analysis", `subject`
  = the patient, `requester` = the ordering provider).
- To report completion, it would normally **not** reply with another
  `ServiceRequest` — it posts a `DiagnosticReport` (and/or `Observation`)
  with `basedOn` pointing back at the `ServiceRequest`'s id, which is the
  FHIR equivalent of "echo the placer order number."

This is the one piece of the scenario that *could* plausibly fall inside
this tool's existing FHIR scope (Direction A/B both already touch
`DiagnosticReport`/`Observation`) — but reading/matching `ServiceRequest`
specifically is not implemented; see the scope matrix in
`notes/scenario-hl7-oru-inbound-results.md`.

## Quick comparison table

| | HL7 v2 ORM/OMG → ORU^R01 | FHIR ServiceRequest → DiagnosticReport |
|---|---|---|
| Transport | TCP/MLLP (Epic Bridges) | HTTPS/REST (SMART on FHIR) |
| Auth | None at message level (network/VPN trust) | OAuth2 (SMART scopes) |
| Order identifier | Placer order number (`ORC-2`) | Resource `id`, referenced via `basedOn` |
| Message format | Pipe-delimited segments | JSON/XML resources |
| This tool's coverage | None | Partial (DiagnosticReport/Observation read + write-back; ServiceRequest itself not yet implemented) |
| Typical real-world use | Lab/imaging/pharmacy instrument interfaces | Modern app-to-app integrations, patient/population APIs |
