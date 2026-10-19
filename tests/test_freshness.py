"""Results say when they're out of date, and the report's score matches Home."""
from __future__ import annotations

import pytest

from epic_compliance.workspace import stale_reasons

RUN = {"config": {"rules_fingerprint": "abc", "evidence": [{"rule_id": "SEC-002", "id": "e1"}],
                  "assessment": {"intake": {"users": "clinicians", "platform": "mobile", "run_mode": "live"}}}}
NOW = {"users": "clinicians", "platform": "mobile", "run_mode": "mock"}


def test_current_results_have_no_reasons():
    assert stale_reasons(RUN, intake=NOW, evidence=[{"rule_id": "SEC-002", "id": "e1"}],
                         rules_fingerprint="abc") == ([], set())


def test_each_kind_of_change_is_reported():
    reasons, changed = stale_reasons(
        RUN, intake={**NOW, "platform": "web_server"},
        evidence=[{"rule_id": "SEC-002", "id": "e1"}, {"rule_id": "SEC-001", "id": "e2"}],
        rules_fingerprint="new")
    assert reasons == ["the test rules were updated", "evidence was added or removed on 1 test",
                       "your app's answers changed"]
    assert changed == {"SEC-001"}


def test_no_run_means_nothing_to_be_stale():
    assert stale_reasons(None, intake=NOW, evidence=[], rules_fingerprint="x") == ([], set())


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("EPIC_COMPLIANCE_DB", str(tmp_path / "runs.db"))
    from fastapi.testclient import TestClient

    from epic_compliance.api import app
    return TestClient(app)


FORM = {"users": "clinicians", "platform": "mobile", "reads_chart": "on", "run_mode": "mock"}


def test_banner_appears_after_evidence_is_added_and_goes_after_a_rerun(client):
    run = client.post("/assess/run", data=FORM, follow_redirects=False)
    assert "out of date" not in client.get("/tests").text
    client.post("/tests/SEC-002/evidence", data={"note": "Logs kept 6 years"})
    tests = client.get("/tests").text
    assert "These results are out of date" in tests and "evidence was added or removed on 1 test" in tests
    assert "Evidence for this test changed after the last run" in client.get("/tests/SEC-002").text
    client.post("/assess/rerun", data={"next": "/tests"})
    assert "out of date" not in client.get("/tests").text
    assert run.status_code == 303


def test_report_score_counts_review_as_not_done(client):
    run = client.post("/assess/run", data=FORM, follow_redirects=False)
    run_id = run.headers["location"].rsplit("/", 1)[-1]
    detail = client.get(f"/api/runs/{run_id}").json()
    verdicts = [f["verdict"] for f in detail["findings"]]
    expected = round(100 * verdicts.count("pass") / len(verdicts))
    page = client.get(f"/runs/{run_id}").text
    assert f"--p: {expected}" in page and f"{verdicts.count('pass')} of {len(verdicts)} checks passing" in page
