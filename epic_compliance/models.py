"""Shared domain models."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class Finding(BaseModel):
    """One structured finding emitted by any check (automated or LLM)."""

    rule_id: str
    verdict: Literal["pass", "fail", "needs_human"]
    evidence: str
    remediation_hint: str = ""
    check_type: Literal["automated", "llm"] = "automated"
    severity: str = "medium"
    category: str = ""
    citation: str = ""  # required for LLM verdicts; populated when available


class Report(BaseModel):
    """Aggregated findings report."""

    run_id: str
    findings: list[Finding] = Field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        counts: dict[str, int] = {"pass": 0, "fail": 0, "needs_human": 0}
        for f in self.findings:
            counts[f.verdict] += 1
        by_category: dict[str, list[Finding]] = {}
        for f in self.findings:
            by_category.setdefault(f.category, []).append(f)
        return {"counts": counts, "by_category": {k: [f.model_dump() for f in v] for k, v in by_category.items()}}
