"""Chat intake — the user describes their app in words; we ask follow-ups.

Step 1 of the assessment is a conversation, not a form. Each turn takes the
transcript plus what is known so far (``ChatState``) and returns the next
assistant message, the updated ``Intake`` and whether enough is known to build
a test plan.

Four topics must be covered before a plan is offered (``TOPICS``): who signs
in, what kind of app it is, what it does with patient data, and the security
posture ("not sure" is a valid answer there — it leaves SEC-* needs_human).

With an Anthropic API key, Claude reads the conversation and returns
structured updates (``_llm_turn``). Without one, or if the call fails, a
keyword parser and scripted questions do the same job (``_rule_turn``), so the
flow works offline like the rest of mock mode.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from pydantic import BaseModel, Field

from .assessment import BAA_LABELS, PLATFORM_LABELS, RUN_FIELDS, USERS_LABELS, Intake
from .rules_engine.llm_evaluator import LLM_MODEL

log = logging.getLogger(__name__)

TOPICS = ("users", "platform", "data", "security")

OPENING = (
    "Tell me about your app: what it does, who uses it, and which health system "
    "you're connecting to Epic for. A couple of sentences is plenty."
)

#: The follow-up asked when a topic is still unknown, with quick replies.
QUESTIONS: dict[str, tuple[str, list[str]]] = {
    "users": (
        "Who signs in to the app: clinicians at the health system, or patients?",
        ["Clinicians", "Patients"],
    ),
    "platform": (
        "What kind of app is it: a native mobile app, a browser-only web app, "
        "or a web app with its own backend server?",
        ["Native mobile app", "Browser-only web app", "Web app with a backend server"],
    ),
    "data": (
        "What does it do with patient data? For example: reads the chart, saves photos "
        "to the chart, writes notes or results back, or sends data to a third-party AI "
        "or cloud service.",
        ["Only reads the chart", "Reads the chart and saves photos",
         "Reads and writes notes back", "Also sends data to a third-party AI"],
    ),
    "security": (
        "Last one: do you know your security setup? The minimum TLS version, whether "
        "every PHI access is audit-logged, and whether BAAs are signed with the health "
        "system and every vendor that sees PHI. \"Not sure\" is a fine answer; "
        "those items will be flagged for review instead of guessed.",
        ["Not sure yet", "TLS 1.2+, audit logging on, BAAs signed with everyone"],
    ),
}

MAX_MESSAGE_CHARS = 2000
MAX_TURNS = 40


class ChatState(BaseModel):
    """Everything a turn needs; round-trips through the page as JSON."""

    messages: list[dict[str, str]] = Field(default_factory=list)
    intake: Intake = Field(default_factory=Intake)
    known: list[str] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)

    @property
    def ready(self) -> bool:
        return all(t in self.known for t in TOPICS)

    @classmethod
    def start(cls) -> "ChatState":
        return cls(messages=[{"role": "assistant", "content": OPENING}],
                   intake=Intake(reads_chart=False))

    @classmethod
    def load(cls, raw: str) -> "ChatState":
        try:
            return cls.model_validate_json(raw) if raw else cls.start()
        except ValueError:
            return cls.start()


def summary(intake: Intake) -> list[tuple[str, str]]:
    """What we understood, as (label, value) rows for the confirmation card."""
    data = [label for label, on in (
        ("reads the chart", intake.reads_chart),
        ("saves photos", intake.saves_images),
        ("writes notes / results back", intake.writes_back),
        ("sends PHI to a third-party AI / cloud", intake.third_party_ai),
    ) if on]
    rows = []
    if intake.app_name:
        rows.append(("App", intake.app_name))
    if intake.health_system:
        rows.append(("Health system", intake.health_system))
    rows += [
        ("Signs in", USERS_LABELS[intake.users]),
        ("App type", PLATFORM_LABELS[intake.platform]),
        ("Patient data", (lambda d: d[:1].upper() + d[1:])(", ".join(data)) or "None"),
        ("TLS", intake.tls_version or "Not sure"),
        ("Audit logging", intake.audit_logging or "Not sure"),
        ("BAA", BAA_LABELS.get(intake.baa_status, "Not sure")),
    ]
    return rows


def next_turn(state: ChatState, message: str, api_key: str = "") -> ChatState:
    """Add the user's message and the assistant's reply; return the new state."""
    message = message.strip()[:MAX_MESSAGE_CHARS]
    if not message:
        return state
    state = state.model_copy(deep=True)
    state.messages.append({"role": "user", "content": message})
    if not state.intake.description:
        state.intake.description = message
    state.messages = state.messages[-MAX_TURNS:]

    if api_key:
        try:
            return _llm_turn(state, api_key)
        except Exception as exc:  # any API trouble -> keep the conversation going offline
            log.warning("intake chat: Claude call failed, using keyword parser: %s", exc)
    return _rule_turn(state, message)


def rewind(current: ChatState, index: int, snapshots: list[ChatState]) -> ChatState | None:
    """The state just before ``current.messages[index]`` (a user message), so it
    can be replaced with an edited one. ``snapshots`` are the saved states.

    Prefers a saved snapshot whose transcript is exactly that prefix, since its
    intake reflects only the earlier answers. If none matches (older turns were
    trimmed), it falls back to the prefix with the current intake.
    """
    if not (0 <= index < len(current.messages)) or current.messages[index]["role"] != "user":
        return None
    prefix = current.messages[:index]
    if index == 1 and prefix[0]["content"] == OPENING:
        return ChatState.start()
    for snap in snapshots:
        if snap.messages == prefix:
            return snap.model_copy(deep=True)
    return current.model_copy(deep=True, update={"messages": prefix, "suggestions": []})


def _finish(state: ChatState, reply: str, suggestions: list[str]) -> ChatState:
    state.messages.append({"role": "assistant", "content": reply})
    state.suggestions = suggestions
    return state


def _next_question(state: ChatState) -> tuple[str, list[str]] | None:
    for topic in TOPICS:
        if topic not in state.known:
            return QUESTIONS[topic]
    return None


READY_REPLY = (
    "Thanks, that's everything I need. Check the summary below. If anything is "
    "wrong, just tell me and I'll update it. Otherwise, generate your test plan."
)


# --------------------------------------------------------------------------- #
# Claude
# --------------------------------------------------------------------------- #
SYSTEM_PROMPT = """\
You run the intake for an Epic integration readiness assessment. A developer \
describes the app they are integrating with a health system's Epic EHR, and you \
work out four things so the tool can choose which compliance checks to run:

1. users: who signs in. "clinicians" (staff sign in with Epic, then pick the \
patient) or "patients" (the patient signs in, e.g. via MyChart, and sees only \
their own record).
2. platform: "mobile" (native iOS/Android), "web_browser" (browser-only app, no \
backend that holds secrets) or "web_server" (web app with its own backend).
3. data: what the app does with patient data. reads_chart, saves_images (photos \
filed in the chart), writes_back (notes or results written to the chart), \
third_party_ai (PHI sent to an outside AI, inference, storage or analytics service).
4. security: tls_version ("TLS 1.3", "TLS 1.2", "TLS 1.0/1.1 still enabled"), \
audit_logging ("yes", "partial", "no"), audit_retention_days (digits), \
baa_status ("all", "health_system_only", "none"). "Not sure" is acceptable: \
leave those fields empty and still mark the topic covered.

Each turn:
- Put only facts the user actually stated or clearly implied into `updates`. \
Use "" for strings and "unknown" for yes/no fields you have no new information \
about. A later correction overrides an earlier answer.
- List in `covered` every topic the conversation has now settled.
- `reply`: if a topic is still open, acknowledge briefly and ask ONE short, \
specific follow-up about the most important open topic. If the description was \
ambiguous (e.g. "a patient app" that clinicians actually use), ask about that. \
If all four are covered, reply with one sentence saying you have what you need \
and they can generate the test plan or correct anything. No markdown.
- `suggestions`: 2-4 short quick-reply options that answer your question, or [] \
when nothing is open.
Do not give compliance advice in the chat; the report does that."""

_YES_NO = {"type": "string", "enum": ["yes", "no", "unknown"]}
_STR = {"type": "string"}
OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "updates": {
            "type": "object",
            "properties": {
                "app_name": _STR,
                "health_system": _STR,
                "users": {"type": "string", "enum": ["clinicians", "patients", ""]},
                "platform": {"type": "string", "enum": ["mobile", "web_browser", "web_server", ""]},
                "reads_chart": _YES_NO,
                "saves_images": _YES_NO,
                "writes_back": _YES_NO,
                "third_party_ai": _YES_NO,
                "tls_version": {"type": "string", "enum": ["TLS 1.3", "TLS 1.2", "TLS 1.0/1.1 still enabled", ""]},
                "audit_logging": {"type": "string", "enum": ["yes", "partial", "no", ""]},
                "audit_retention_days": _STR,
                "baa_status": {"type": "string", "enum": ["all", "health_system_only", "none", ""]},
            },
            "required": [
                "app_name", "health_system", "users", "platform", "reads_chart",
                "saves_images", "writes_back", "third_party_ai", "tls_version",
                "audit_logging", "audit_retention_days", "baa_status",
            ],
            "additionalProperties": False,
        },
        "covered": {"type": "array", "items": {"type": "string", "enum": list(TOPICS)}},
        "reply": _STR,
        "suggestions": {"type": "array", "items": _STR},
    },
    "required": ["updates", "covered", "reply", "suggestions"],
    "additionalProperties": False,
}


def _llm_turn(state: ChatState, api_key: str) -> ChatState:
    import anthropic  # import only when needed

    client = anthropic.Anthropic(api_key=api_key)
    known = state.intake.model_dump(exclude={"description", *RUN_FIELDS})
    # The API wants the first message from the user: drop the canned opener.
    messages = [m for m in state.messages if m["content"] != OPENING]
    messages.append({"role": "user", "content":
                     f"[Intake so far: {json.dumps(known)}; topics covered: {state.known}]"})

    response = client.messages.create(
        model=LLM_MODEL,
        max_tokens=2048,
        system=SYSTEM_PROMPT,
        messages=messages,
        output_config={"format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
    )
    if response.stop_reason in ("refusal", "max_tokens"):
        raise RuntimeError(f"stop_reason={response.stop_reason}")
    out = json.loads(next(b.text for b in response.content if b.type == "text"))

    updates: dict[str, Any] = {}
    for key, value in out["updates"].items():
        if value in ("", "unknown"):
            continue
        updates[key] = (value == "yes") if value in ("yes", "no") else value
    state.intake = state.intake.model_copy(update=updates)
    state.known = [t for t in TOPICS if t in state.known or t in out["covered"]]

    if state.ready:
        return _finish(state, out["reply"] or READY_REPLY, [])
    return _finish(state, out["reply"], out["suggestions"][:4])


# --------------------------------------------------------------------------- #
# Offline keyword parser
# --------------------------------------------------------------------------- #
def _has(text: str, pattern: str) -> bool:
    return re.search(pattern, text) is not None


def _negated(text: str, pattern: str) -> bool:
    return _has(text, rf"\b(no|not|never|without|doesn't|does not|don't|do not|won't)\b[^.]{{0,40}}{pattern}")


def _parse(state: ChatState, text: str, asked: str | None) -> set[str]:
    """Apply what ``text`` says to ``state.intake``; return the topics it settled."""
    t = text.lower()
    i = state.intake
    settled: set[str] = set()

    if _has(t, r"\b(patients? (use|log|sign)|patient[- ]facing|for patients|mychart|consumers?)\b") \
            or (asked == "users" and _has(t, r"\bpatients?\b")):
        i.users, settled = "patients", settled | {"users"}
    elif _has(t, r"\b(clinicians?|doctors?|physicians?|nurses?|providers?|dermatologists?|"
                 r"radiologists?|surgeons?|care team|staff|md)\b"):
        i.users, settled = "clinicians", settled | {"users"}

    if _has(t, r"\b(mobile|ios|android|iphone|ipad|phone|app store|native)\b"):
        i.platform, settled = "mobile", settled | {"platform"}
    elif _has(t, r"\b(backend|back-end|server|django|rails|node|express|fastapi|spring)\b"):
        i.platform, settled = "web_server", settled | {"platform"}
    elif _has(t, r"\b(browser|web ?app|website|spa|single[- ]page|react|angular|vue)\b"):
        i.platform, settled = "web_browser", settled | {"platform"}

    data_patterns = {
        "reads_chart": r"\b((?<!ai )(?<!a )read|reads|reading|view|views|pull|pulls|fetch|look up|history|problem list|"
                       r"problems|medications?|meds|allerg\w*|labs?|vitals|observations?|demographics|"
                       r"chart data|patient data)\b",
        "saves_images": r"\b(photo\w*|images?|pictures?|camera|imaging|snapshots?)\b",
        "writes_back": r"\b(write|writes|writing|written|notes?|results? back|post|file[sd]? (it|them)?\s*"
                       r"(back|in|into|to)|document(s|ation)?|report back)\b",
        "third_party_ai": r"\b(ai|a\.i\.|ml|machine learning|model|inference|llm|gpt|openai|claude|"
                          r"vendor|cloud|aws|azure|gcp|third[- ]party)\b",
    }
    hits = 0
    for field, pattern in data_patterns.items():
        if _negated(t, pattern):
            setattr(i, field, False)
            hits += 1
        elif _has(t, pattern):
            setattr(i, field, True)
            hits += 1
    if hits or (asked == "data" and _has(t, r"\b(nothing|none|no data)\b")):
        settled.add("data")

    sec = 0
    if m := re.search(r"tls\s*v?(1\.[0-3])", t):
        i.tls_version = "TLS 1.0/1.1 still enabled" if m.group(1) in ("1.0", "1.1") else f"TLS {m.group(1)}"
        sec += 1
    if _has(t, r"audit"):
        i.audit_logging = "no" if _negated(t, r"audit") else ("partial" if _has(t, r"partial|some") else "yes")
        sec += 1
    if m := re.search(r"(\d+)\s*(day|year)s?\b[^.]{0,30}(retention|retain|kept|keep)|"
                      r"(retention|retain|kept|keep)[^.]{0,30}?(\d+)\s*(day|year)s?", t):
        num, unit = (m.group(1), m.group(2)) if m.group(1) else (m.group(5), m.group(6))
        i.audit_retention_days = str(int(num) * (365 if unit == "year" else 1))
    if _has(t, r"\bbaas?\b|business associate"):
        if _negated(t, r"(baa|business associate)"):
            i.baa_status = "none"
        elif _has(t, r"every|all|everyone|vendors?"):
            i.baa_status = "all"
        else:
            i.baa_status = "health_system_only"
        sec += 1
    if sec or (asked == "security" and _has(t, r"not sure|don't know|dont know|unsure|no idea|skip|later")):
        settled.add("security")

    if m := re.search(r"\b(?:with|at|for)\s+((?:[A-Z][\w&'.-]*\s?){1,4}"
                      r"(?:Health|Hospital|Medical Center|Clinic|Healthcare|Health System))", text):
        i.health_system = m.group(1).strip()
    if m := re.search(r"\b(?:called|named|building|built)\s+([A-Z][\w-]+)", text):
        i.app_name = m.group(1)
    return settled


def _rule_turn(state: ChatState, message: str) -> ChatState:
    asked = next((t for t in TOPICS if t not in state.known), None)
    settled = _parse(state, message, asked)
    state.known = [t for t in TOPICS if t in state.known or t in settled]

    if state.ready:
        return _finish(state, READY_REPLY, [])
    question, suggestions = _next_question(state)  # type: ignore[misc]
    if asked and asked not in settled and not settled:
        question = "Sorry, I didn't catch that. " + question
    elif len(state.messages) == 2:
        question = "Thanks, got it. " + question
    return _finish(state, question, suggestions)
