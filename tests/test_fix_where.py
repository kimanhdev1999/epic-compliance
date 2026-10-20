"""Where to fix it (ADR-018): every Failed or Needs review result names who must act."""
from __future__ import annotations

import pytest

from epic_compliance import fix_where as fw
from epic_compliance.models import Finding, Report
from epic_compliance.rules_engine.catalog import load_rules

RULES = {r.id: r for r in load_rules()}


def _f(rule_id: str, verdict: str, evidence: str = "", citation: str = "", fix_where: str = "") -> Finding:
    rule = RULES[rule_id]
    return Finding(rule_id=rule_id, verdict=verdict, evidence=evidence, citation=citation, fix_where=fix_where,
                   severity=rule.severity, category=rule.category)


def where(rule_id: str, verdict: str, **kw) -> str:
    sample = kw.pop("sample", False)
    return fw.where_to_fix(RULES[rule_id], _f(rule_id, verdict, **kw), sample=sample)


@pytest.mark.parametrize("rule_id, verdict, kw, expected", [
    # Epic's own behaviour: nothing for the user to fix.
    ("AUTH-010", "fail", {"evidence": "No jwks_uri in .well-known/smart-configuration"}, "epic"),
    ("FHIR-004", "fail", {"evidence": "resources_missing_profile=['Encounter']"}, "epic"),
    # What the client ID is granted: fhir.epic.com.
    ("AUTH-003", "fail", {"evidence": "missing scopes"}, "registration"),
    ("CLIN-008", "fail", {"evidence": "AllergyIntolerance status=403"}, "registration"),
    # The tool's own sample write: refused = registration, mishandled = Epic.
    ("CLIN-005", "fail", {"evidence": "POST Media status=403"}, "registration"),
    ("CLIN-007", "fail", {"evidence": "stored without encounter reference"}, "epic"),
    # Stated answers.
    ("SEC-001", "fail", {"evidence": "TLS 1.0", "citation": "app_config says TLS 1.0"}, "your_app"),
    ("SEC-001", "needs_human", {"evidence": "nothing answered"}, "your_answers"),
    # Caused by this tool's setup.
    ("SEC-002", "needs_human", {"citation": "[MOCK] Evidence not evaluated"}, "tool_setup"),
    ("CLIN-002", "needs_human", {"evidence": "Skipped: no test patient was given."}, "tool_setup"),
    ("AUTH-007", "needs_human", {"evidence": "auth_probe unavailable"}, "tool_setup"),
])
def test_derived_from_subject_and_verdict(rule_id, verdict, kw, expected):
    assert where(rule_id, verdict, **kw) == expected


def test_a_pass_has_nothing_to_fix():
    assert where("AUTH-001", "pass") == ""


def test_sample_data_wins_over_everything():
    assert where("CLIN-008", "fail", evidence="403", sample=True) == "sample"


def test_a_check_that_knows_the_cause_overrides_the_default():
    assert where("CLIN-001", "fail", fix_where="tool_setup") == "tool_setup"


def test_clin_001_zero_matches_blames_the_test_patient_not_the_registration():
    from epic_compliance.rules_engine.automated_checks import AUTOMATED_CHECKS

    evidence = {"clinician": {"patient_search": {"query": "family=Nobody", "status_code": 200, "match_count": 0}}}
    finding = AUTOMATED_CHECKS["CLIN-001"](evidence, RULES["CLIN-001"])
    assert finding.verdict == "fail" and finding.fix_where == "tool_setup"


def test_epic_and_sample_failures_never_block():
    assert fw.is_blocking("critical", "registration", "fail")
    assert not fw.is_blocking("critical", "epic", "fail")
    assert not fw.is_blocking("critical", "sample", "fail")
    assert not fw.is_blocking("medium", "registration", "fail")
    assert not fw.is_blocking("critical", "tool_setup", "needs_human")


def test_report_counts_only_fixable_blockers():
    from epic_compliance.viewmodel import build_report_view

    report = Report(run_id="r", findings=[
        _f("AUTH-010", "fail", "No jwks_uri"),          # high, Epic's sandbox
        _f("CLIN-008", "fail", "status=403"),            # high, registration
    ])
    view = build_report_view(report)
    assert view["blocking"] == 1
    assert {it["rule_id"]: it["fix_where"] for it in view["failed"]} == {"AUTH-010": "epic", "CLIN-008": "registration"}
    assert build_report_view(report, sample=True)["blocking"] == 0


class TestSampleRuns:
    @pytest.fixture
    def client(self, tmp_path, monkeypatch):
        monkeypatch.setenv("EPIC_COMPLIANCE_DB", str(tmp_path / "runs.db"))
        from fastapi.testclient import TestClient

        from epic_compliance.api import app

        return TestClient(app)

    def _save(self, run_id: str, mode: str, created_at: str) -> None:
        from epic_compliance import store

        store.save_run(Report(run_id=run_id, findings=[_f("AUTH-010", "fail", "No jwks_uri")]),
                       {"run_mode": mode, "fhir_base_url": "https://fhir.example"})
        with store._connect() as conn:
            conn.execute("UPDATE runs SET created_at = ? WHERE run_id = ?", (created_at, run_id))

    def test_a_sample_run_never_replaces_a_real_one(self, client):
        from epic_compliance import store

        self._save("real", "live", "2026-10-01T00:00:00+00:00")
        self._save("sample", "mock", "2026-10-02T00:00:00+00:00")  # newer, but sample data
        assert store.latest_run()["run_id"] == "real"
        assert "sample-banner" not in client.get("/tests").text

    def test_only_sample_runs_are_shown_under_a_banner(self, client):
        from epic_compliance import store

        self._save("sample", "mock", "2026-10-02T00:00:00+00:00")
        assert store.latest_run()["run_id"] == "sample"
        page = client.get("/tests").text
        assert "Sample data — not a real result" in page
        assert 'data-where="sample"' in page
        drawer = client.get("/tests/AUTH-010", headers={"HX-Request": "true"}).text
        assert "Sample data" not in drawer and "class=\"reason" not in drawer  # requirements R2

    def test_tests_page_filters_and_labels_by_where_to_fix(self, client):
        self._save("real", "live", "2026-10-01T00:00:00+00:00")
        page = client.get("/tests?where=epic").text
        assert '<option value="epic" selected>' in page
        assert 'data-where="epic"' in page and "Epic&#39;s sandbox" in page
        drawer = client.get("/tests/AUTH-010", headers={"HX-Request": "true"}).text
        assert "Where to fix it &middot; Epic&#39;s sandbox" in drawer
