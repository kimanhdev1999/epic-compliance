"""Assessment flow: intake answers -> test plan -> run -> report."""
from __future__ import annotations

import pytest

from epic_compliance.assessment import SELECTORS, Intake, build_plan
from epic_compliance.rules_engine.catalog import load_rules


def _ids(plan) -> set[str]:
    return plan.rule_ids


class TestBuildPlan:
    def test_every_catalog_rule_has_a_selector(self):
        # A new rule without a selector is included by default, but it should be
        # a deliberate choice — keep SELECTORS in step with the catalog.
        assert {r.id for r in load_rules()} <= set(SELECTORS)

    def test_clinician_mobile_photo_app(self):
        plan = build_plan(Intake(users="clinicians", platform="mobile", saves_images=True))
        ids = _ids(plan)
        assert plan.launch_profile == "clinician_standalone"
        assert {"CLIN-001", "CLIN-002", "CLIN-005", "CLIN-006", "CLIN-007", "CLIN-011", "CLIN-012"} <= ids
        assert not ids & {"WRITE-001", "WRITE-002", "WRITE-003", "WRITE-004", "AUTH-009"}
        assert not ids & {"CLIN-009", "CLIN-010"}  # no notes written back

    def test_clinician_web_app_skips_mobile_registration_checks(self):
        ids = _ids(build_plan(Intake(users="clinicians", platform="web_server")))
        assert not ids & {"CLIN-011", "CLIN-012"}

    def test_patient_app_writing_results(self):
        plan = build_plan(Intake(users="patients", writes_back=True))
        ids = _ids(plan)
        assert plan.launch_profile == "patient_launch"
        assert {"AUTH-009", "WRITE-001", "WRITE-002", "WRITE-004"} <= ids
        assert "WRITE-003" not in ids  # no images
        assert not any(i.startswith("CLIN-") for i in ids)

    def test_read_only_off_skips_fhir_reads(self):
        ids = _ids(build_plan(Intake(reads_chart=False)))
        assert not any(i.startswith("FHIR-") for i in ids)

    def test_security_checks_always_planned(self):
        ids = _ids(build_plan(Intake()))
        assert {"SEC-001", "SEC-002", "SEC-003"} <= ids

    def test_excluded_checks_carry_a_reason(self):
        plan = build_plan(Intake(users="clinicians"))
        assert plan.excluded and all(c.reason for c in plan.excluded)

    def test_overrides_map_answers_to_config(self):
        o = build_plan(Intake(
            platform="mobile", third_party_ai=True, health_system="Advocate Health",
            baa_status="health_system_only", tls_version="TLS 1.2",
        )).config_overrides()
        assert o["app_client_type"] == "public"
        assert o["transport_tls_version"] == "TLS 1.2"
        assert "third-party AI" in o["covered_entity_relationship"]
        assert "not yet" in o["baa_in_place"]

    def test_from_form_unchecked_boxes_are_false(self):
        i = Intake.from_form({"users": "patients", "saves_images": "on"})
        assert i.saves_images is True and i.reads_chart is False and i.writes_back is False


class TestAssessmentRoutes:
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

    def test_intake_page_renders(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
        assert "Describe your app" in resp.text

    def test_plan_lists_selected_and_skipped_checks(self, client):
        resp = client.post("/assess/plan", data=self.FORM)
        assert resp.status_code == 200
        assert "CLIN-005" in resp.text and "Why:" in resp.text
        assert "skipped for this app" in resp.text

    def test_run_executes_only_planned_checks(self, client):
        resp = client.post("/assess/run", data=self.FORM, follow_redirects=False)
        assert resp.status_code == 303
        run_id = resp.headers["location"].rsplit("/", 1)[-1]

        detail = client.get(f"/api/runs/{run_id}").json()
        ran = {f["rule_id"] for f in detail["findings"]}
        planned = {c["rule_id"] for c in detail["config"]["assessment"]["included"]}
        assert ran == planned
        assert not ran & {"CLIN-009", "WRITE-001"}

        page = client.get(f"/runs/{run_id}")
        assert page.status_code == 200
        assert "DermScan" in page.text and "Assessment scope" in page.text

        runs = client.get("/api/runs").json()
        assert runs[0]["app_name"] == "DermScan"
