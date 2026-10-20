"""Validation engine — runs all rules against collected evidence, aggregates findings."""
from __future__ import annotations

import uuid
from typing import Any

from ..models import Finding, Report
from .catalog import is_reviewer_question, load_rules, Rule
from .automated_checks import AUTOMATED_CHECKS
from .llm_evaluator import evaluate_with_llm
from .. import evidence as dev_evidence


def _build_evidence_for_llm(rule: Rule, evidence: dict[str, Any]) -> str:
    """Build a human-readable evidence string for LLM rules from collected data."""
    if is_reviewer_question(rule):
        # Judged only on what the developer attached; Epic's data says nothing about it.
        return "\n".join([f"Reviewer question: {rule.description}", f"Sources: {rule.source}",
                          f"Expected proof: {rule.evidence_needed}", rule.remediation_hint])
    # For security rules we summarize available config context
    parts = [f"Rule: {rule.description}", f"Evidence needed: {rule.evidence_needed}"]
    # Include any relevant top-level evidence keys
    for key in ["smart_configuration", "token_response", "app_config"]:
        if key in evidence:
            parts.append(f"{key}: {evidence[key]}")
    parts.append("NOTE: Evidence collected from Epic's non-production sandbox. Your production configuration must still be reviewed.")
    return "\n".join(str(p) for p in parts)


#: What each attested answer is called in the chat, for messages people read.
ANSWER_LABELS = {
    "transport_tls_version": "your minimum TLS version",
    "audit_logging_enabled": "whether PHI access is audit-logged",
    "audit_log_retention_days": "how long audit logs are kept",
    "baa_in_place": "which BAAs are signed",
    "covered_entity_relationship": "your relationship to the health system",
}


def _attested_fields(rule: Rule) -> list[str]:
    """The app_config answers a rule judges, from its evidence_needed text."""
    for part in rule.evidence_needed.split(";"):
        head, _, fields = part.partition(":")
        if head.strip() == "app_config":
            return [f.strip() for f in fields.split(",") if f.strip()]
    return []


def _nothing_to_judge(rule: Rule, evidence: dict[str, Any], attached: list[dict[str, Any]]) -> Finding | None:
    """needs_human, without asking Claude, when an attested rule has no answer
    and no attached evidence. Missing information is unknown, not a failure."""
    if is_reviewer_question(rule) and not attached:
        return Finding(
            rule_id=rule.id,
            verdict="needs_human",
            evidence=("Not answered yet. A health-system or Epic reviewer will ask this. Answer it under "
                      "Evidence with a statement, file or link, then re-run."),
            remediation_hint=rule.remediation_hint,
            check_type="llm",
            severity=rule.severity,
            category=rule.category,
            citation="Not evaluated: no answer attached yet.",
        )
    fields = _attested_fields(rule)
    app_config = evidence.get("app_config") or {}
    if not fields or attached or any(app_config.get(f) for f in fields):
        return None
    return Finding(
        rule_id=rule.id,
        verdict="needs_human",
        evidence=(f"Not checked yet: the chat has no answer for "
                  f"{' or '.join(ANSWER_LABELS.get(f, f) for f in fields)} (you may have said "
                  f"\"not sure\"), and no evidence is attached to this test. Add evidence here, or "
                  f"answer in the chat, then re-run."),
        remediation_hint=rule.remediation_hint,
        check_type="llm",
        severity=rule.severity,
        category=rule.category,
        citation="Not evaluated: nothing to judge. Answer in the chat or attach evidence to this test, then re-run.",
    )


def run_pipeline(
    evidence: dict[str, Any],
    api_key: str = "",
    mock_llm: bool = True,
    launch_profile: str = "patient_launch",
    rule_ids: set[str] | None = None,
    attached_evidence: dict[str, list[dict[str, Any]]] | None = None,
) -> Report:
    """Run the rules that apply to ``launch_profile``, return a Report.

    ``rule_ids`` narrows the run to an assessment plan's checks; None runs all.
    ``attached_evidence`` maps rule id -> evidence the developer attached
    (ADR-015); Claude reviews it for the AI-reviewed rules.
    """
    attached_evidence = attached_evidence or {}
    rules = [
        r for r in load_rules()
        if r.applies_to(launch_profile) and (rule_ids is None or r.id in rule_ids)
    ]
    findings: list[Finding] = []
    run_id = str(uuid.uuid4())

    for rule in rules:
        try:
            if rule.check_type == "automated":
                check_fn = AUTOMATED_CHECKS.get(rule.id)
                if check_fn is None:
                    # SEAM: unknown automated rule — emit needs_human
                    findings.append(Finding(
                        rule_id=rule.id,
                        verdict="needs_human",
                        evidence=f"No automated check implemented for rule {rule.id}",
                        remediation_hint=rule.remediation_hint,
                        severity=rule.severity,
                        category=rule.category,
                        citation="No check function registered — add to AUTOMATED_CHECKS registry.",
                    ))
                else:
                    findings.append(check_fn(evidence, rule))
            elif rule.check_type == "llm":
                attached = attached_evidence.get(rule.id, [])
                skipped = _nothing_to_judge(rule, evidence, attached)
                if skipped is not None:
                    findings.append(skipped)
                    continue
                summary, blocks = dev_evidence.for_claude(attached) if attached else ("", [])
                findings.append(evaluate_with_llm(
                    rule=rule,
                    evidence_str=_build_evidence_for_llm(rule, evidence),
                    api_key=api_key,
                    mock=mock_llm,
                    developer_evidence=summary,
                    attachments=blocks,
                ))
            else:
                findings.append(Finding(
                    rule_id=rule.id,
                    verdict="needs_human",
                    evidence=f"Unknown check_type={rule.check_type!r}",
                    remediation_hint=rule.remediation_hint,
                    severity=rule.severity,
                    category=rule.category,
                ))
        except Exception as exc:
            findings.append(Finding(
                rule_id=rule.id,
                verdict="needs_human",
                evidence=f"Check raised exception: {exc}",
                remediation_hint="Investigate check implementation.",
                severity=rule.severity,
                category=rule.category,
            ))

    return Report(run_id=run_id, findings=findings)
