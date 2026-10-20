"""Workspace view — the app's standing compliance posture (ADR-012).

The UI is a compliance workspace for one app: *frameworks* with a readiness
percentage, *tests* mapped to those frameworks with a status, *integrations*
that unlock tests, and a getting-started checklist. This module turns the latest saved run (plus
the assessment scope stored with it) into that view.

A test is one catalog rule. Its status comes from the latest run:
  passing / failing / needs_review  — the run's verdict
  not_applicable                    — left out of this app's plan (with why)
  not_run                           — no run yet
The workspace shows only the tests in this app's plan: a not-applicable test
never appears in lists or counts, only in the plan it was left out of.
A rule maps to every framework its ``source`` citation names, so one test can
count toward ONC (g)(10) and HIPAA at the same time.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from . import fix_where as fw
from .rules_engine.catalog import Rule, load_rules
from .viewmodel import CATEGORY_LABELS, SEVERITY_ORDER, SUBJECTS

STATUS_FROM_VERDICT = {"pass": "passing", "fail": "failing", "needs_human": "needs_review"}
STATUS_LABELS = {
    "failing": "Failing",
    "needs_review": "Needs review",
    "passing": "Passing",
    "not_applicable": "Not applicable",
    "not_run": "Not run",
}
STATUS_ORDER = {s: i for i, s in enumerate(STATUS_LABELS)}


@dataclass(frozen=True)
class Framework:
    key: str
    name: str
    summary: str
    matches: Callable[[str], bool]


def _cites(*needles: str) -> Callable[[str], bool]:
    return lambda source: any(n in source for n in needles)


FRAMEWORKS: list[Framework] = [
    Framework("onc_g10", "ONC (g)(10)",
              "The certified standardized API criterion Epic's FHIR API is built to. "
              "Apps are expected to work with what it guarantees.",
              _cites("(g)(10)")),
    Framework("smart", "SMART App Launch",
              "How your app signs in to Epic: OAuth 2.0, PKCE, scopes and identity tokens.",
              _cites("SMART", "RFC", "OAuth", "OpenID")),
    Framework("us_core", "US Core / FHIR R4",
              "The shape of the patient data you read and write.",
              _cites("US Core", "HL7 FHIR", "FHIR R4")),
    Framework("epic", "Epic requirements",
              "The Epic on FHIR APIs your app uses, and what Epic and its customers ask for in app review.",
              _cites("Epic")),
    Framework("hipaa", "HIPAA Security Rule",
              "Encryption, audit logging and BAAs. Every health-system security review asks for these.",
              _cites("HIPAA")),
    Framework("patient_safety", "Patient safety",
              "Wrong-patient guards and safe clinical photography: two identifiers before anything "
              "is saved, consent, and images clinicians can rely on.",
              _cites("Joint Commission", "wrong-patient", "RACGP", "Katragadda")),
    Framework("hecvat", "Security questionnaire (HECVAT)",
              "What a health system's vendor security questionnaire asks: assurance reports, "
              "incidents, data use, subprocessors and AI.",
              _cites("HECVAT", "HITRUST")),
    Framework("mobile", "Mobile app security",
              "OWASP MASVS and native-app OAuth (RFC 8252): data on the phone, tokens, permissions.",
              _cites("MASVS", "RFC 8252")),
    Framework("ai_dsi", "AI transparency (ONC HTI-1)",
              "What the decision-support rules ask of AI output in the EHR: source attributes, "
              "risk management and feedback.",
              _cites("(b)(11)")),
    Framework("fda", "FDA device rules",
              "Whether the software is a medical device, and what FDA expects of AI that analyses clinical data.",
              _cites("FDA")),
]
FRAMEWORKS_BY_KEY = {f.key: f for f in FRAMEWORKS}


def frameworks_for(rule: Rule) -> list[str]:
    return [f.key for f in FRAMEWORKS if f.matches(rule.source)]


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #
def build_tests(run: dict[str, Any] | None, in_plan: set[str] | None = None) -> list[dict[str, Any]]:
    """Every catalog rule as a test, with its status in ``run`` (latest run).

    ``in_plan`` is the current plan's test ids. Before any run, a test outside
    it is not applicable rather than "not run".
    """
    findings = {f.rule_id: f for f in run["report"].findings} if run else {}
    sample = bool(run) and run.get("run_mode") == "mock"
    assessment = (run or {}).get("config", {}).get("assessment") or {}
    in_scope = {c["rule_id"]: c["reason"] for c in assessment.get("included", [])}
    out_of_scope = {c["rule_id"]: c["reason"] for c in assessment.get("excluded", [])}

    tests = []
    for rule in load_rules():
        f = findings.get(rule.id)
        if f is not None:
            status = STATUS_FROM_VERDICT[f.verdict]
        elif run is None:
            status = "not_run" if in_plan is None or rule.id in in_plan else "not_applicable"
        else:
            status = "not_applicable"
        subject = SUBJECTS.get(rule.subject, ("", ""))
        where = fw.where_to_fix(rule, f, sample=sample)
        tests.append({
            "id": rule.id,
            "title": rule.description,
            "status": status,
            "status_label": STATUS_LABELS[status],
            "severity": rule.severity,
            "category": rule.category,
            "category_label": CATEGORY_LABELS.get(rule.category, rule.category.replace("_", " ").title()),
            "type_label": "AI review" if rule.check_type == "llm" else "Automated",
            "frameworks": frameworks_for(rule),
            "source": rule.source,
            "subject": rule.subject,
            "subject_label": subject[0],
            "subject_detail": subject[1],
            "evidence": f.evidence if f else "",
            "citation": f.citation if f else "",
            "remediation": (f.remediation_hint if f else "") or rule.remediation_hint,
            # Who must act on a failing / needs-review result (ADR-018).
            "fix_where": where,
            "fix_where_label": fw.label(where),
            "fix_where_action": fw.action(where),
            "blocking": fw.is_blocking(rule.severity, where, f.verdict if f else ""),
            # Why it is (or isn't) part of this app's assessment.
            "reason": in_scope.get(rule.id) or out_of_scope.get(rule.id)
                      or ("" if f or run is None else "Not part of this assessment."),
        })
    tests.sort(key=lambda t: (STATUS_ORDER[t["status"]], SEVERITY_ORDER.get(t["severity"], 9), t["id"]))
    return tests


def _progress(tests: list[dict[str, Any]]) -> dict[str, Any]:
    counts = {s: 0 for s in STATUS_LABELS}
    for t in tests:
        counts[t["status"]] += 1
    in_scope = counts["passing"] + counts["failing"] + counts["needs_review"]
    return {
        "counts": counts,
        "in_scope": in_scope,
        # Only passing tests count as done; "needs review" is not done.
        "percent": round(100 * counts["passing"] / in_scope) if in_scope else None,
    }


def build_frameworks(tests: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for fw in FRAMEWORKS:
        fw_tests = [t for t in tests if fw.key in t["frameworks"]]
        out.append({"key": fw.key, "name": fw.name, "summary": fw.summary,
                    "tests": fw_tests, **_progress(fw_tests)})
    return out


# --------------------------------------------------------------------------- #
# Workspace (Home)
# --------------------------------------------------------------------------- #
def build_workspace(
    run: dict[str, Any] | None,
    *,
    intake: dict[str, Any],
    integrations: dict[str, dict[str, Any]],
    history: list[dict[str, Any]],
    in_plan: set[str] | None = None,
) -> dict[str, Any]:
    # Only the tests that apply to this app: the rest are noise to the user.
    tests = [t for t in build_tests(run, in_plan) if t["status"] != "not_applicable"]
    progress = _progress(tests)
    counts = progress["counts"]
    attention = [t for t in tests if t["status"] in ("failing", "needs_review")]
    # What the user can act on first; Epic's own failures and sample data last.
    attention.sort(key=lambda t: t["fix_where"] in fw.NOT_ACTIONABLE)

    checklist = [
        {"title": "Describe your app",
         "detail": "Tell the assistant what you're building. It works out which tests apply.",
         "done": bool(intake), "href": "/assess", "cta": "Start"},
        {"title": "Connect the Epic sandbox",
         "detail": "Add your non-production client ID from fhir.epic.com to test real sign-in and data.",
         "done": integrations["epic"]["status"] != "not_connected", "href": "/integrations", "cta": "Connect"},
        {"title": "Run your first assessment",
         "detail": "Sign in to the Epic sandbox and run your test plan against it.",
         "done": run is not None, "href": "/assess", "cta": "Run"},
        {"title": "Fix failing tests",
         "detail": "Each failing test shows how to fix it.",
         "done": run is not None and counts["failing"] == 0, "href": "/tests?status=failing", "cta": "Review"},
        {"title": "Resolve tests that need review",
         "detail": "Answer the open question, connect the missing service, or confirm by hand.",
         "done": run is not None and counts["needs_review"] == 0, "href": "/tests?status=needs_review", "cta": "Review"},
    ]
    if run is not None:
        checklist.append({
            "title": "Share the readiness report with the health system",
            "detail": "Send it to the health system's Epic team with your security questionnaire.",
            "done": False, "href": f"/runs/{run['run_id']}", "cta": "Open report",
        })

    return {
        "run": run,
        # Built-in sample responses, not Epic: shown only until a real run exists.
        "sample": bool(run) and run.get("run_mode") == "mock",
        "intake": intake,
        "tests": tests,
        "frameworks": build_frameworks(tests),
        "attention": attention,
        "checklist": checklist,
        "checklist_done": sum(1 for c in checklist if c["done"]),
        "trend": _trend(history, intake.get("app_name", "")),
        **progress,
    }


def _trend(history: list[dict[str, Any]], app_name: str) -> list[dict[str, Any]]:
    """Readiness of this app's recent runs, oldest first, for the Home chart."""
    points = []
    for r in history:
        if app_name and r.get("app_name") != app_name:
            continue
        total = r["pass_count"] + r["fail_count"] + r["needs_human_count"]
        points.append({
            "run_id": r["run_id"],
            "when": r["created_at"][:16].replace("T", " "),
            "percent": round(100 * r["pass_count"] / total) if total else 0,
            "fail": r["fail_count"],
        })
    return list(reversed(points[:12]))


def stale_reasons(
    run: dict[str, Any] | None,
    *,
    intake: dict[str, Any],
    evidence: list[dict[str, Any]],
    rules_fingerprint: str,
) -> tuple[list[str], set[str]]:
    """What changed since ``run``: (plain-language reasons, ids of tests whose
    evidence changed). Empty when the results are current."""
    if run is None:
        return [], set()
    cfg = run.get("config", {})
    reasons: list[str] = []
    if cfg.get("rules_fingerprint") != rules_fingerprint:
        reasons.append("the test rules were updated")
    before = {(e["rule_id"], e["id"]) for e in cfg.get("evidence") or []}
    now = {(e["rule_id"], e["id"]) for e in evidence}
    changed = {rule_id for rule_id, _ in before ^ now}
    if changed:
        n = len(changed)
        reasons.append(f"evidence was added or removed on {n} test{'s' if n != 1 else ''}")
    ran_with = (cfg.get("assessment") or {}).get("intake") or {}
    skip = {"description", "run_mode", "fhir_base_url", "oauth_client_id", "app_redirect_uri",
            "test_patient_query", "encounter_required"}
    if ran_with and intake and any(ran_with.get(k) != v for k, v in intake.items() if k not in skip):
        reasons.append("your app's answers changed")
    return reasons, changed


def history_of_test(runs: list[dict[str, Any]], rule_id: str) -> list[dict[str, Any]]:
    """Status of one test across recent full runs, newest first."""
    out = []
    for run in runs:
        rerun = run.get("config", {}).get("rerun_of")
        if rerun and rule_id not in rerun["rule_ids"]:
            continue  # a single-test re-run of another test: nothing new for this one
        f = next((f for f in run["report"].findings if f.rule_id == rule_id), None)
        status = STATUS_FROM_VERDICT[f.verdict] if f else "not_applicable"
        out.append({"run_id": run["run_id"], "when": run["created_at"][:16].replace("T", " "),
                    "status": status, "status_label": STATUS_LABELS[status]})
    return out
