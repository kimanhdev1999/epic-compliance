"""LLM evaluator — sends {requirement, evidence} to Claude API, returns strict JSON verdict.

Invariants:
- verdict is always one of pass | fail | needs_human
- citation must reference actual evidence text — never empty
- All LLM verdicts carry the needs_human escape hatch in their allowed values

The interface is mockable: set mock=True to return a deterministic needs_human verdict
without calling the API. Pipeline runs fully without an API key in mock mode.
"""
from __future__ import annotations

import json
import re
from typing import Literal

from pydantic import BaseModel

from ..models import Finding
from .catalog import Rule


LLM_MODEL = "claude-sonnet-4-6"

SYSTEM_PROMPT = """You are a healthcare compliance expert evaluating whether a FHIR app meets a specific ONC (g)(10) certification requirement.

You will receive:
- requirement: the compliance rule text
- evidence: configuration or runtime data collected from the app

Respond ONLY with a JSON object in this exact format (no prose, no markdown fences):
{"verdict": "<pass|fail|needs_human>", "citation": "<quote the specific evidence text that supports your verdict>"}

Rules:
- verdict must be one of: pass, fail, needs_human
- citation must quote from the evidence string provided — never fabricate evidence
- Use needs_human when evidence is insufficient to determine compliance
- Never return verdict without citation"""


class LLMVerdict(BaseModel):
    verdict: Literal["pass", "fail", "needs_human"]
    citation: str


def _parse_llm_response(content: str) -> LLMVerdict:
    """Extract and validate JSON from Claude response."""
    # Try direct parse first
    try:
        data = json.loads(content.strip())
        return LLMVerdict(**data)
    except Exception:
        pass
    # Extract JSON object from text
    match = re.search(r'\{[^{}]+\}', content, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group())
            return LLMVerdict(**data)
        except Exception:
            pass
    raise ValueError(f"Could not parse LLM response as verdict JSON: {content!r}")


def evaluate_with_llm(rule: Rule, evidence_str: str, api_key: str, mock: bool = False) -> Finding:
    """Call Claude to evaluate a security/narrative rule against collected evidence."""
    if mock or not api_key:
        # Mock mode: return needs_human so pipeline runs without API key
        return Finding(
            rule_id=rule.id,
            verdict="needs_human",
            evidence=evidence_str,
            remediation_hint=rule.remediation_hint,
            check_type="llm",
            severity=rule.severity,
            category=rule.category,
            citation=f"[MOCK] Evidence not evaluated — run with ANTHROPIC_API_KEY for live assessment. Evidence snippet: {evidence_str[:120]}",
        )

    import anthropic  # import only when needed

    client = anthropic.Anthropic(api_key=api_key)
    user_message = json.dumps({
        "requirement": rule.description,
        "evidence": evidence_str,
    })

    message = client.messages.create(
        model=LLM_MODEL,
        max_tokens=512,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )

    raw_content = message.content[0].text if message.content else ""
    verdict_obj = _parse_llm_response(raw_content)

    if not verdict_obj.citation:
        # Invariant: never allow a verdict without citation
        verdict_obj.citation = f"[WARNING: LLM provided no citation] Raw response: {raw_content[:200]}"
        verdict_obj.verdict = "needs_human"

    return Finding(
        rule_id=rule.id,
        verdict=verdict_obj.verdict,
        evidence=evidence_str,
        remediation_hint=rule.remediation_hint,
        check_type="llm",
        severity=rule.severity,
        category=rule.category,
        citation=verdict_obj.citation,
    )
