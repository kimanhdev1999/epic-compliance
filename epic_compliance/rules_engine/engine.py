"""Validation engine — runs all rules against collected evidence, aggregates findings."""
from __future__ import annotations

import uuid
from typing import Any

from ..models import Finding, Report
from .catalog import load_rules, Rule
from .automated_checks import AUTOMATED_CHECKS
from .llm_evaluator import evaluate_with_llm


def _build_evidence_for_llm(rule: Rule, evidence: dict[str, Any]) -> str:
    """Build a human-readable evidence string for LLM rules from collected data."""
    # For security rules we summarize available config context
    parts = [f"Rule: {rule.description}", f"Evidence needed: {rule.evidence_needed}"]
    # Include any relevant top-level evidence keys
    for key in ["smart_configuration", "token_response"]:
        if key in evidence:
            parts.append(f"{key}: {evidence[key]}")
    # Add a note that this is mock/sandbox data when applicable
    parts.append("NOTE: Evidence collected from mock/sandbox environment. Actual production configuration must be reviewed.")
    return "\n".join(str(p) for p in parts)


def run_pipeline(evidence: dict[str, Any], api_key: str = "", mock_llm: bool = True) -> Report:
    """Run all rules against evidence, return a Report with all findings."""
    rules = load_rules()
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
                evidence_str = _build_evidence_for_llm(rule, evidence)
                findings.append(evaluate_with_llm(
                    rule=rule,
                    evidence_str=evidence_str,
                    api_key=api_key,
                    mock=mock_llm,
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
