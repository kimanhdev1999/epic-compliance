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


def test_run_step_opens_the_test_plan_once_the_app_is_described():
    # After signing in, "Run" must lead to the plan's Run button, not back to the chat.
    epic = {"epic": {"status": "connected"}}
    run_step = lambda w: next(c for c in w["checklist"] if c["cta"] == "Run")
    described = ws.build_workspace(None, intake={"platform": "mobile"}, integrations=epic, history=[])
    assert run_step(described)["href"] == "/assess/plan#setup"
    blank = ws.build_workspace(None, intake={}, integrations=epic, history=[])
    assert run_step(blank)["href"] == "/assess"


class TestRoutes:
    @pytest.fixture
    def client(self, tmp_path, monkeypatch):
        monkeypatch.setenv("EPIC_COMPLIANCE_DB", str(tmp_path / "runs.db"))
        from fastapi.testclient import TestClient

        from epic_compliance.api import app

        return TestClient(app)

    FORM = {
        "app_name": "DermScan", "health_system": "Example Health",
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

        page = client.get("/tests")
        assert page.status_code == 200
        # Only tests in this app's plan are listed: CLIN-009 (writes notes) isn't.
        assert 'data-status="not_applicable"' not in page.text and "Not applicable" not in page.text
        assert "CLIN-009" not in page.text and client.get("/tests/CLIN-009").status_code == 404
        drawer = client.get("/tests/CLIN-002", headers={"HX-Request": "true"})
        assert "<html" not in drawer.text  # partial for the drawer
        assert "<html" in client.get("/tests/CLIN-002").text  # full page without HTMX

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
        assert "Re-check this test" in drawer.text and 'name="rule_id" value="CLIN-001"' in drawer.text

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

    def test_rerun_one_test_folds_into_a_run_saved_while_it_ran(self, client, monkeypatch):
        """A live re-check takes seconds; a full run saved meanwhile must not be dropped."""
        import uuid

        from epic_compliance import api, store

        client.post("/assess/run", data=self.FORM)
        real = api.run_full_pipeline

        def pipeline_while_another_run_saves(*args, **kwargs):
            latest = store.latest_run()
            findings = [f.model_copy(update={"verdict": "fail"}) if f.rule_id == "CLIN-002" else f
                        for f in latest["report"].findings]
            store.save_run(latest["report"].model_copy(update={"run_id": str(uuid.uuid4()), "findings": findings}),
                           latest["config"])
            return real(*args, **kwargs)

        monkeypatch.setattr(api, "run_full_pipeline", pipeline_while_another_run_saves)
        client.post("/assess/rerun", data={"next": "/tests", "rule_id": "CLIN-001"})
        runs = store.recent_runs()
        run, newer = runs[0], runs[1]
        assert run["config"]["rerun_of"]["run_id"] == newer["run_id"]
        assert next(f for f in run["report"].findings if f.rule_id == "CLIN-002").verdict == "fail"

    def test_recheck_run_page_shows_just_that_test(self, client):
        from epic_compliance import store

        client.post("/assess/run", data=self.FORM)
        base = store.latest_run()
        client.post("/assess/rerun", data={"next": "/tests", "rule_id": "CLIN-001"})
        run = store.latest_run()

        page = client.get(f"/runs/{run['run_id']}")
        assert "Re-check of CLIN-001" in page.text and 'id="f-CLIN-001"' in page.text
        assert 'id="f-CLIN-002"' not in page.text  # the carried-over results stay out of the way
        assert f'href="/runs/{base["run_id"]}"' in page.text and "/tests?open=CLIN-001" in page.text

        full = client.get(f"/runs/{run['run_id']}?all=1")
        assert 'id="f-CLIN-002"' in full.text and "re-checked only CLIN-001" in full.text
        assert "Re-check of" not in client.get(f"/runs/{base['run_id']}").text  # full runs unchanged

        drawer = client.get("/tests/CLIN-001", headers={"HX-Request": "true"})
        assert drawer.text.count('class="kind">Re-check<') == 1

    def test_rerun_rejects_offsite_redirects_and_needs_an_intake(self, client):
        resp = client.post("/assess/rerun", data={"next": "//evil.example"}, follow_redirects=False)
        assert resp.headers["location"] == "/assess"  # no intake yet
        client.post("/assess/run", data=self.FORM)
        resp = client.post("/assess/rerun", data={"next": "//evil.example"}, follow_redirects=False)
        assert resp.headers["location"] == "/"

    def test_integrations_list_what_each_unlocks(self, client, monkeypatch):
        resp = client.get("/integrations")
        assert resp.status_code == 200
        for name in ("Epic sandbox", "Answer review", "HL7 FHIR validator", "ONC (g)(10) test kit"):
            assert name in resp.text
        assert "SEC-001" in resp.text  # unlocked by Claude
