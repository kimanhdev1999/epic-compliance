"""Workspace UI (ADR-012): Home, Frameworks, Tests, Integrations."""
from __future__ import annotations

import pytest

from epic_compliance import workspace as ws
from epic_compliance.rules_engine.catalog import load_rules


def test_every_rule_maps_to_a_framework():
    unmapped = [r.id for r in load_rules() if not ws.frameworks_for(r)]
    assert unmapped == []


def test_one_rule_can_count_toward_several_frameworks():
    sec1 = next(r for r in load_rules() if r.id == "SEC-001")  # ONC (g)(10) + HIPAA
    assert {"onc_g10", "hipaa"} <= set(ws.frameworks_for(sec1))


def test_no_run_means_every_test_not_run():
    tests = ws.build_tests(None)
    assert tests and {t["status"] for t in tests} == {"not_run"}


class TestRoutes:
    @pytest.fixture
    def client(self, tmp_path, monkeypatch):
        monkeypatch.setenv("EPIC_COMPLIANCE_DB", str(tmp_path / "runs.db"))
        from fastapi.testclient import TestClient

        from epic_compliance.api import app

        return TestClient(app)

    FORM = {
        "app_name": "DermScan", "health_system": "Advocate Health",
        "users": "clinicians", "platform": "mobile",
        "reads_chart": "on", "saves_images": "on", "run_mode": "mock",
    }

    def test_empty_workspace_shows_getting_started(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
        assert "Get started" in resp.text and "Describe your app" in resp.text
        assert "Needs attention" not in resp.text  # nothing has run

    def test_after_a_run_home_frameworks_and_tests_reflect_it(self, client):
        client.post("/assess/run", data=self.FORM)
        home = client.get("/")
        assert "DermScan" in home.text and "Needs attention" in home.text
        assert "Re-run tests" in home.text

        page = client.get("/tests?status=not_applicable")
        assert page.status_code == 200
        # A test the plan left out shows as Not applicable, with the plan's reason.
        assert 'data-status="not_applicable"' in page.text
        drawer = client.get("/tests/CLIN-009", headers={"HX-Request": "true"})
        assert "Not applicable" in drawer.text and "write notes back" in drawer.text
        assert "<html" not in drawer.text  # partial for the drawer
        assert "<html" in client.get("/tests/CLIN-009").text  # full page without HTMX

        fw = client.get("/frameworks/patient_safety")
        assert fw.status_code == 200 and "CLIN-002" in fw.text
        assert client.get("/frameworks/nope").status_code == 404
        assert client.get("/tests/NOPE-1").status_code == 404

    def test_rerun_uses_the_saved_plan_and_returns_to_the_page(self, client):
        client.post("/assess/run", data=self.FORM)
        before = len(client.get("/api/runs").json())
        resp = client.post("/assess/rerun", data={"next": "/tests"}, follow_redirects=False)
        assert resp.status_code == 303 and resp.headers["location"] == "/tests"
        runs = client.get("/api/runs").json()
        assert len(runs) == before + 1 and runs[0]["app_name"] == "DermScan"

    def test_rerun_one_test_keeps_the_other_results(self, client):
        from epic_compliance import store

        client.post("/assess/run", data=self.FORM)
        base = store.latest_run()
        drawer = client.get("/tests/CLIN-001", headers={"HX-Request": "true"})
        assert "Re-run this test" in drawer.text and 'name="rule_id" value="CLIN-001"' in drawer.text

        resp = client.post("/assess/rerun", data={"next": "/tests?open=CLIN-001", "rule_id": "CLIN-001"},
                           follow_redirects=False)
        assert resp.status_code == 303 and resp.headers["location"] == "/tests?open=CLIN-001"
        run = store.latest_run()
        assert run["run_id"] != base["run_id"]
        assert run["config"]["rerun_of"] == {"run_id": base["run_id"], "rule_ids": ["CLIN-001"]}
        # Every test from the full run is still there, in the same order.
        assert [f.rule_id for f in run["report"].findings] == [f.rule_id for f in base["report"].findings]
        # A test that wasn't re-run gets no new history entry.
        assert len(ws.history_of_test(store.recent_runs(), "CLIN-002")) == 1
        assert len(ws.history_of_test(store.recent_runs(), "CLIN-001")) == 2

    def test_rerun_rejects_offsite_redirects_and_needs_an_intake(self, client):
        resp = client.post("/assess/rerun", data={"next": "//evil.example"}, follow_redirects=False)
        assert resp.headers["location"] == "/assess"  # no intake yet
        client.post("/assess/run", data=self.FORM)
        resp = client.post("/assess/rerun", data={"next": "//evil.example"}, follow_redirects=False)
        assert resp.headers["location"] == "/"

    def test_integrations_list_what_each_unlocks(self, client, monkeypatch):
        resp = client.get("/integrations")
        assert resp.status_code == 200
        for name in ("Epic sandbox", "Claude", "HL7 FHIR validator", "ONC (g)(10) test kit"):
            assert name in resp.text
        assert "SEC-001" in resp.text  # unlocked by Claude
