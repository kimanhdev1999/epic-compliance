"""Assessment flow: intake answers -> test plan -> run -> report."""
from __future__ import annotations

import pytest

from epic_compliance.assessment import SELECTORS, Intake, build_plan
from epic_compliance.intake_chat import ChatState, next_turn
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


class _NoKeyConfig:
    anthropic_api_key = ""
    fhir_base_url = "https://example.test/FHIR/R4"


class TestChatIntake:
    """Offline (no API key) path: keyword parser + scripted follow-ups."""

    def _say(self, state, text):
        return next_turn(state, text, api_key="")

    def test_opening_message_and_empty_input(self):
        state = ChatState.start()
        assert state.messages[0]["role"] == "assistant" and not state.ready
        assert self._say(state, "   ") == state

    def test_description_fills_topics_then_asks_what_is_missing(self):
        state = self._say(ChatState.start(),
                          "We're building DermScan, a mobile app dermatologists at Advocate Health "
                          "use to photograph a lesion, save the photo to the chart and get an AI read.")
        i = state.intake
        assert (i.users, i.platform) == ("clinicians", "mobile")
        assert i.saves_images and i.third_party_ai
        assert i.health_system == "Advocate Health" and i.app_name == "DermScan"
        assert not i.reads_chart  # "save the photo to the chart" is a write, not a read
        assert set(state.known) == {"users", "platform", "data"}
        assert "security" in state.messages[-1]["content"].lower()
        assert state.suggestions

    def test_not_sure_on_security_completes_the_intake(self):
        state = self._say(ChatState.start(), "A mobile app clinicians use to view the chart")
        state = self._say(state, "Not sure yet")
        assert state.ready and state.intake.tls_version == ""
        assert not state.suggestions

    def test_unrecognised_answer_reasks(self):
        state = self._say(ChatState.start(), "It helps with skin checks")
        assert "users" not in state.known
        state = self._say(state, "hmm")
        assert state.messages[-1]["content"].startswith("Sorry")

    def test_security_answers_are_parsed(self):
        state = self._say(ChatState.start(), "A web app with a Django backend that patients use via MyChart to read labs")
        assert state.intake.users == "patients" and state.intake.platform == "web_server"
        state = self._say(state, "TLS 1.3, audit logging on and kept 6 years, BAA signed with all vendors")
        i = state.intake
        assert state.ready
        assert (i.tls_version, i.audit_logging, i.audit_retention_days, i.baa_status) == ("TLS 1.3", "yes", "2190", "all")

    def test_negation(self):
        state = self._say(ChatState.start(), "A mobile app for clinicians that reads the chart, no AI involved")
        assert state.intake.third_party_ai is False and state.intake.reads_chart is True

    def test_llm_failure_falls_back_to_parser(self, monkeypatch):
        import epic_compliance.intake_chat as ic

        def boom(state, key):
            raise RuntimeError("network down")
        monkeypatch.setattr(ic, "_llm_turn", boom)
        state = next_turn(ChatState.start(), "A mobile app clinicians use", api_key="sk-test")
        assert state.intake.platform == "mobile" and state.messages[-1]["role"] == "assistant"

    def test_llm_turn_applies_structured_updates(self, monkeypatch):
        import json
        from types import SimpleNamespace

        import anthropic

        out = {
            "updates": {
                "app_name": "DermScan", "health_system": "", "users": "clinicians",
                "platform": "mobile", "reads_chart": "no", "saves_images": "yes",
                "writes_back": "unknown", "third_party_ai": "yes", "tls_version": "",
                "audit_logging": "", "audit_retention_days": "", "baa_status": "",
            },
            "covered": ["users", "platform", "data"],
            "reply": "Which health system is this for, and do you know your TLS version?",
            "suggestions": ["Not sure yet"],
        }
        sent = {}

        class FakeMessages:
            def create(self, **kw):
                sent.update(kw)
                return SimpleNamespace(stop_reason="end_turn",
                                       content=[SimpleNamespace(type="text", text=json.dumps(out))])

        monkeypatch.setattr(anthropic, "Anthropic", lambda **kw: SimpleNamespace(messages=FakeMessages()))
        state = next_turn(ChatState.start(), "A mobile derm photo app with AI", api_key="sk-test")

        assert sent["messages"][0]["role"] == "user"  # canned opener dropped
        assert sent["output_config"]["format"]["type"] == "json_schema"
        i = state.intake
        assert (i.app_name, i.users, i.saves_images, i.third_party_ai, i.reads_chart) == \
            ("DermScan", "clinicians", True, True, False)
        assert not state.ready and state.suggestions == ["Not sure yet"]
        assert state.messages[-1]["content"] == out["reply"]

    def test_state_round_trips_and_bad_state_restarts(self):
        state = self._say(ChatState.start(), "A mobile app clinicians use")
        assert ChatState.load(state.model_dump_json()) == state
        assert ChatState.load("{not json").messages == ChatState.start().messages


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

    def test_intake_page_is_a_chat(self, client):
        resp = client.get("/assess")
        assert resp.status_code == 200
        assert "What are you building?" in resp.text and 'id="chat-form"' in resp.text

    def test_chat_turn_asks_a_follow_up(self, client, monkeypatch):
        monkeypatch.setattr("epic_compliance.api.get_config", lambda: _NoKeyConfig())
        resp = client.post("/assess/chat", data={
            "message": "A mobile app dermatologists use to photograph lesions and save them to the chart",
            "state": "",
        })
        assert resp.status_code == 200
        assert "security" in resp.text.lower()  # users, platform, data known -> asks security
        assert "Generate test plan" not in resp.text

    def test_plan_lists_run_choice_and_back_to_chat(self, client):
        resp = client.post("/assess/plan", data=self.FORM)
        assert "Where should we test?" in resp.text and "Back to chat" in resp.text

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
