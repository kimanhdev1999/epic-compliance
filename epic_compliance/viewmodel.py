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
    "fhir_conformance": "FHIR / US Core Conformance",
    "security": "Security & Privacy",
    "clinician_workflow": "Clinician Workflow (standalone)",
    "write_back": "Writing to the Chart",
}

VERDICT_LABELS = {"pass": "Passed", "fail": "Failed", "needs_human": "Needs review"}


#: What each rule's verdict is evidence *about*. key → (badge, explanation).
SUBJECTS = {
    "epic_platform": (
        "Tests Epic",
        "Checks Epic's own server behaviour or data. Passes for any app pointed at this Epic.",
    ),
    "app_registration": (
        "Tests your Epic registration",
        "Uses your app's client ID. Proves what the registration grants, not your backend's code.",
    ),
    "tool_sample": (
        "Tests the write path",
        "This tool sends its own sample resource through your registration. Proves Epic accepts it, not that your app sends valid ones.",
    ),
    "tool_launch": (
        "Tests this tool's login",
        "Exercises this tool's own SMART login, not your app's.",
    ),
    "app_attested": (
        "Judges your stated answers",
        "Claude judges your answers in the chat and the evidence you attach. "
        "It doesn't connect to your systems to verify them.",
    ),
}


def _sort_key(item: dict[str, Any]) -> tuple[int, int]:
    return (
        VERDICT_ORDER.get(item["verdict"], 9),
        SEVERITY_ORDER.get(item["severity"], 9),
    )


def build_report_view(report: Report, evidence: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Return {run_id, counts, total, score, categories:[{key,label,items,counts}]}.

    ``evidence`` is the run's snapshot of developer-attached evidence (ADR-015);
    each finding lists the items attached to its test.
    """
    rules = {r.id: r for r in load_rules()}
    categories: dict[str, list[dict[str, Any]]] = {}
    provided: dict[str, list[dict[str, Any]]] = {}
    for e in evidence or []:
        provided.setdefault(e["rule_id"], []).append(e)

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
            "subject": rule.subject if rule else "",
            "subject_label": SUBJECTS.get(rule.subject, ("", ""))[0] if rule else "",
            "subject_detail": SUBJECTS.get(rule.subject, ("", ""))[1] if rule else "",
            "provided_evidence": provided.get(f.rule_id, []),
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
    # Readiness: passing checks out of every check run, the same as Home.
    # Checks waiting on review count as not done, so the headline isn't inflated.
    score = round(100 * counts["pass"] / total) if total else None

    all_items = [it for cat in cat_views for it in cat["items"]]
    by_severity = lambda its: sorted(its, key=lambda it: SEVERITY_ORDER.get(it["severity"], 9))
    failed = by_severity([it for it in all_items if it["verdict"] == "fail"])
    review = by_severity([it for it in all_items if it["verdict"] == "needs_human"])
    failed_by_severity = {s: 0 for s in SEVERITY_ORDER}
    for it in failed:
        failed_by_severity[it["severity"]] = failed_by_severity.get(it["severity"], 0) + 1

    return {
        "run_id": report.run_id,
        "counts": counts,
        "total": total,
        "score": score,
        "categories": cat_views,
        # Report-page triage: what blocks go-live first, then what a person must confirm.
        "failed": failed,
        "review": review,
        "failed_by_severity": failed_by_severity,
        "blocking": sum(1 for it in failed if it["severity"] in ("critical", "high")),
    }


def find_item(report: Report, rule_id: str) -> dict[str, Any] | None:
    """Return the enriched view item for a single rule_id, or None."""
    view = build_report_view(report)
    for cat in view["categories"]:
        for item in cat["items"]:
            if item["rule_id"] == rule_id:
                return item
    return None
