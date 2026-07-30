"""Presentation helpers — turn a Report into template-ready view data.

Joins each Finding with its Rule (for the requirement text + source citation),
groups by category, and sorts so the most actionable items surface first
(failures before needs-human before passes, then by severity).
"""
from __future__ import annotations

from typing import Any

from .models import Report
from .rules_engine.catalog import load_rules

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}
VERDICT_ORDER = {"fail": 0, "needs_human": 1, "pass": 2}

CATEGORY_LABELS = {
    "auth": "Authorization & SMART",
    "fhir": "FHIR / US Core Conformance",
    "security": "Security & Privacy",
}


def _sort_key(item: dict[str, Any]) -> tuple[int, int]:
    return (
        VERDICT_ORDER.get(item["verdict"], 9),
        SEVERITY_ORDER.get(item["severity"], 9),
    )


def build_report_view(report: Report) -> dict[str, Any]:
    """Return {run_id, counts, total, score, categories:[{key,label,items,counts}]}."""
    rules = {r.id: r for r in load_rules()}
    categories: dict[str, list[dict[str, Any]]] = {}

    for f in report.findings:
        rule = rules.get(f.rule_id)
        category = f.category or (rule.category if rule else "other")
        item = {
            "rule_id": f.rule_id,
            "verdict": f.verdict,
            "evidence": f.evidence,
            "remediation_hint": f.remediation_hint,
            "check_type": f.check_type,
            "severity": f.severity,
            "category": category,
            "citation": f.citation,
            "requirement": rule.description if rule else "",
            "source": rule.source if rule else "",
            "evidence_needed": rule.evidence_needed if rule else "",
        }
        categories.setdefault(category, []).append(item)

    cat_views: list[dict[str, Any]] = []
    for key in sorted(categories.keys()):
        items = sorted(categories[key], key=_sort_key)
        c_counts = {"pass": 0, "fail": 0, "needs_human": 0}
        for it in items:
            c_counts[it["verdict"]] = c_counts.get(it["verdict"], 0) + 1
        cat_views.append(
            {
                "key": key,
                "label": CATEGORY_LABELS.get(key, key.replace("_", " ").title()),
                "items": items,
                "counts": c_counts,
            }
        )

    counts = report.summary()["counts"]
    total = sum(counts.values())
    # Simple readiness score: passes / (total - needs_human), guarded for /0.
    decided = counts["pass"] + counts["fail"]
    score = round(100 * counts["pass"] / decided) if decided else None

    return {
        "run_id": report.run_id,
        "counts": counts,
        "total": total,
        "score": score,
        "categories": cat_views,
    }


def find_item(report: Report, rule_id: str) -> dict[str, Any] | None:
    """Return the enriched view item for a single rule_id, or None."""
    view = build_report_view(report)
    for cat in view["categories"]:
        for item in cat["items"]:
            if item["rule_id"] == rule_id:
                return item
    return None
