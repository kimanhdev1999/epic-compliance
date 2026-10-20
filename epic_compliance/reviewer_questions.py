"""Reviewer questions — what a health system's or Epic's reviewer asks the vendor (ADR-019).

The automated tests (``rules/*.json``) prove what Epic and the app's registration
do. Most of a real review is questions about the vendor's own practices instead:
security questionnaire topics, Epic's app-review topics and, for some apps, FDA
status. ``rules/reviewer/questions.json`` is a curated library of them. Each
entry cites a real source and carries applicability tags.

This module loads the library and picks the questions for an app. Claude picks
and tailors them when a key is set (``plan_generator``). This tag match is the
offline fallback and the floor Claude can't go below: a question whose tags all
hold, or that we can't rule out yet, is in the plan.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, field_validator

from .paths import resource_root

if TYPE_CHECKING:
    from .assessment import Intake

LIBRARY_PATH = resource_root() / "rules" / "reviewer" / "questions.json"

#: Tag -> (phrase when it holds, phrase when it doesn't), finishing "the app ...".
TAGS: dict[str, tuple[str, str]] = {
    "all": ("", ""),
    "patient_app": ("is used by patients", "isn't used by patients"),
    "clinician_app": ("is used by clinicians", "isn't used by clinicians"),
    "mobile": ("is a native mobile app", "isn't a native mobile app"),
    "web": ("runs in a web browser", "doesn't run in a web browser"),
    "backend": ("has its own backend server", "has no backend server"),
    "writes_back": ("writes to the Epic chart", "doesn't write to the chart"),
    "images": ("captures clinical photos", "doesn't capture clinical photos"),
    "ai_analysis": ("analyses or classifies clinical data with AI", "doesn't analyse clinical data with AI"),
    "ai_vendor": ("sends PHI to a third-party AI service", "doesn't send PHI to a third-party AI service"),
    "stores_phi": ("stores PHI outside Epic", "doesn't store PHI outside Epic"),
}

TOPICS: dict[str, str] = {
    "epic_app": "Epic app registration & sign-in",
    "access": "Access control",
    "audit": "Audit logging",
    "encryption": "Encryption",
    "device": "Data on the device",
    "images": "Clinical photos",
    "patient_safety": "Patient safety",
    "vendors": "Vendors & agreements",
    "assurance": "Security assurance",
    "incident": "Incidents & breach notification",
    "data_lifecycle": "Privacy & data use",
    "continuity": "Backup & recovery",
    "ai": "AI",
    "regulatory": "Regulatory",
}


class Source(BaseModel):
    name: str
    section: str = ""
    url: str = ""
    # True only if someone read the source and it supports the question.
    # Unverified sources are shown as "source to confirm", never hidden.
    verified: bool = False


class ReviewerQuestion(BaseModel):
    id: str
    topic: str
    question: str
    why: str
    # The question applies when ANY of its tags holds ("all" = always) and every
    # ``requires`` tag holds too (the camera-roll question needs a mobile app).
    tags: list[str]
    requires: list[str] = []
    severity: Literal["critical", "high", "medium", "low"]
    # What answers it: a statement in the chat, or a document (policy, report).
    proof: Literal["answer", "document"] = "answer"
    sources: list[Source]
    # An automated test that already asks this (e.g. SEC-001 asks about TLS).
    covered_by: str = ""

    @field_validator("tags", "requires")
    @classmethod
    def _known_tags(cls, v: list[str]) -> list[str]:
        unknown = set(v) - set(TAGS)
        if unknown:
            raise ValueError(f"unknown tags: {sorted(unknown)}")
        return v

    @property
    def category(self) -> str:
        """The test category this question shows under, one per topic."""
        return f"rq_{self.topic}"

    def as_rule(self) -> dict:
        """The question as a catalog rule, so it runs, scores and reports like any test."""
        return {
            "id": self.id,
            "category": self.category,
            "subject": "app_attested",
            "source": "; ".join(s.name for s in self.sources) or "Source to confirm",
            "severity": self.severity,
            "evidence_needed": ("A document (policy, report or certificate)" if self.proof == "document"
                                else "Your answer, as a statement, file or link"),
            "check_type": "llm",
            "description": self.question,
            "remediation_hint": f"Answer it under Evidence with a statement, file or link. Why reviewers ask: {self.why}",
        }

    @field_validator("topic")
    @classmethod
    def _known_topic(cls, v: str) -> str:
        if v not in TOPICS:
            raise ValueError(f"unknown topic {v!r}")
        return v

    @property
    def verified(self) -> bool:
        return any(s.verified for s in self.sources)


@lru_cache(maxsize=1)
def load_library() -> tuple[ReviewerQuestion, ...]:
    data = json.loads(LIBRARY_PATH.read_text())
    questions = tuple(ReviewerQuestion(**q) for q in data)
    ids = [q.id for q in questions]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate reviewer question ids")
    return questions


def app_facts(intake: "Intake") -> dict[str, bool | None]:
    """What the intake tells us about each tag: True, False, or None (unknown)."""
    platform = intake.platform
    return {
        "all": True,
        "patient_app": intake.users == "patients",
        "clinician_app": intake.users == "clinicians",
        "mobile": platform == "mobile",
        "web": platform in ("web_browser", "web_server"),
        # A mobile app usually has a backend too, but the intake doesn't ask.
        "backend": True if platform == "web_server" else (False if platform == "web_browser" else None),
        "writes_back": intake.writes_back or intake.saves_images,
        "images": intake.saves_images,
        "ai_vendor": intake.third_party_ai,
        # Not asked yet: keep these questions in rather than guess they don't apply.
        "ai_analysis": None,
        "stores_phi": None,
    }


@dataclass
class PlannedQuestion:
    question: ReviewerQuestion
    included: bool
    reason: str
    # Tailored wording from Claude; None means the library text is used.
    text: str | None = None
    ai_suggested: bool = False

    @property
    def wording(self) -> str:
        return self.text or self.question.question


def _phrases(tags: list[str], facts: dict[str, bool | None], value: bool | None, negate: bool = False) -> str:
    return " and ".join(TAGS[t][1 if negate else 0] for t in tags if facts.get(t) is value)


def decide(q: ReviewerQuestion, facts: dict[str, bool | None], covered: set[str] = frozenset()) -> tuple[bool, str]:
    """Whether ``q`` applies to an app with these facts, and why."""
    if q.covered_by and q.covered_by in covered:
        return False, f"Already asked by {q.covered_by}."
    if no := _phrases(q.requires, facts, False, negate=True):
        return False, f"Doesn't apply: the app {no}."
    unsure = [_phrases(q.requires, facts, None)]
    if "all" in q.tags:
        yes = ""
    elif yes := _phrases(q.tags, facts, True):
        pass
    elif unknown := _phrases(q.tags, facts, None):
        unsure.append(unknown.replace(" and ", " or "))
    else:
        return False, f"Doesn't apply: the app {_phrases(q.tags, facts, False, negate=True)}."
    unsure = " and ".join(u for u in unsure if u)
    if unsure:
        lead = f"The app {yes}. " if yes else ""
        return True, f"{lead}We don't know yet whether the app {unsure}, so it's included."
    return True, f"Applies because the app {yes}." if yes else "Asked of every app that connects to Epic."


def select(intake: "Intake", covered: set[str] = frozenset()) -> list[PlannedQuestion]:
    """Every library question, included or not, each with the reason.
    ``covered`` is the automated tests already in the plan."""
    facts = app_facts(intake)
    return [PlannedQuestion(q, *decide(q, facts, covered)) for q in load_library()]
