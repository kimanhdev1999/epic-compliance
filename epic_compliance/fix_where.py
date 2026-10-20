"""Where to fix it: who must act on a Failed or Needs review result.

A check that knows the cause sets ``Finding.fix_where`` (e.g. CLIN-001 when the
test patient matched nobody). Otherwise it is derived from what the rule tests
(its ``subject``) and the verdict, which also covers runs saved before this
field existed. See notes/decisions.md ADR-018.
"""
from __future__ import annotations

import re

from .models import Finding
from .rules_engine.catalog import Rule
from .rules_engine.llm_evaluator import NOT_EVALUATED

#: key → (label, what the user does). Order is the order filters list them.
FIX_WHERE = {
    "your_app": (
        "Your app",
        "Change your app's code or configuration, then re-run.",
    ),
    "registration": (
        "Your Epic app registration",
        "Fix it on fhir.epic.com (APIs, scopes, redirect URI), then sign in to Epic again and re-run.",
    ),
    "your_answers": (
        "Your answers or proof",
        "Answer the question in the chat or attach proof to this test, then re-run.",
    ),
    "tool_setup": (
        "This tool's setup",
        "Fix the sign-in, test patient, Claude key or validator, then re-run.",
    ),
    "epic": (
        "Epic's sandbox",
        "Nothing for you to fix: this is Epic's own behaviour. It's shown for information and never blocks.",
    ),
    "sample": (
        "Sample data",
        "Not a real result: this run used the tool's built-in sample responses, not Epic.",
    ),
}

#: Results the user can't act on. They never count as blocking.
NOT_ACTIONABLE = {"epic", "sample"}

_REFUSED = re.compile(r"\b(401|403)\b")


def where_to_fix(rule: Rule | None, finding: Finding | None, *, sample: bool = False) -> str:
    """The FIX_WHERE key for a result, or "" when it passed (nothing to fix)."""
    if finding is None or finding.verdict == "pass":
        return ""
    if sample:
        return "sample"
    if finding.fix_where:
        return finding.fix_where
    subject = rule.subject if rule else ""

    if finding.verdict == "needs_human":
        # "[MOCK]" is how runs saved before this wording said the same.
        if finding.citation.startswith((NOT_EVALUATED, "[MOCK]")):
            return "tool_setup"  # Claude isn't connected, so the answer was never judged
        if subject == "app_attested":
            return "your_answers"
        # Not signed in, no test patient, validator down, a probe only the launch suite runs.
        return "tool_setup"

    # Failed: what the rule tests decides who must act.
    if subject in ("epic_platform", "tool_launch"):
        return "epic"
    if subject == "app_attested":
        return "your_app"
    if subject == "tool_sample":
        # The tool's own sample is valid by construction: a refusal is the registration,
        # anything else is how Epic handled it.
        return "registration" if _REFUSED.search(finding.evidence) else "epic"
    return "registration"


def is_blocking(severity: str, fix_where: str, verdict: str) -> bool:
    """A failure the user must fix before submitting."""
    return verdict == "fail" and severity in ("critical", "high") and fix_where not in NOT_ACTIONABLE


def label(fix_where: str) -> str:
    return FIX_WHERE.get(fix_where, ("", ""))[0]


def action(fix_where: str) -> str:
    return FIX_WHERE.get(fix_where, ("", ""))[1]
