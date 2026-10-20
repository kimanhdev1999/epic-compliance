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
    oauth_client_id = ""
    oauth_redirect_uri = ""
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
            "done": False,
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

    def test_summary_waits_until_claude_has_no_more_questions(self, monkeypatch):
        import json
        from types import SimpleNamespace

        import anthropic

        def reply(done):
            return {
                "updates": {k: "" for k in ("app_name", "health_system", "users", "platform",
                                            "tls_version", "audit_logging", "audit_retention_days",
                                            "baa_status")}
                | {k: "unknown" for k in ("reads_chart", "saves_images", "writes_back", "third_party_ai")},
                "covered": ["users", "platform", "data", "security"],
                "reply": "Do you have a BAA with AWS?" if not done else "That's everything.",
                "suggestions": ["Yes", "Not yet"] if not done else [],
                "done": done,
            }
        outs = iter([reply(False), reply(True)])

        class FakeMessages:
            def create(self, **kw):
                return SimpleNamespace(stop_reason="end_turn", content=[
                    SimpleNamespace(type="text", text=json.dumps(next(outs)))])

        monkeypatch.setattr(anthropic, "Anthropic", lambda **kw: SimpleNamespace(messages=FakeMessages()))
        state = next_turn(ChatState.start(), "TLS: not sure", api_key="sk-test")
        assert not state.ready and state.suggestions == ["Yes", "Not yet"]  # still asking
        state = next_turn(state, "Not yet", api_key="sk-test")
        assert state.ready and not state.suggestions

    def test_old_saved_state_without_asking_still_loads(self):
        raw = ChatState.start().model_dump_json().replace(',"asking":false', "")
        assert ChatState.load(raw).asking is False

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

    def test_plan_runs_against_the_epic_sandbox_only(self, client):
        resp = client.post("/assess/plan", data=self.FORM)
        assert "Back to chat" in resp.text
        assert "Demo data" not in resp.text and 'name="run_mode" value="live"' in resp.text

    def test_plan_puts_the_epic_setup_steps_first(self, client, monkeypatch):
        from epic_compliance import pipeline
        monkeypatch.setattr(pipeline, "get_live_token", lambda: None)
        text = client.post("/assess/plan", data=self.FORM).text
        assert text.index("Before you run") < text.index("What will be checked")
        assert 'id="test_patient_query"' in text and "Sign in to Epic sandbox" in text
        assert 'id="run-btn" class="btn big" disabled' in text  # the page script unlocks it
        assert client.get("/launch/status").json() == {"signed_in": False}

        monkeypatch.setattr(pipeline, "get_live_token", lambda: object())
        text = client.post("/assess/plan", data=self.FORM).text
        assert 'id="step-signin" class="done"' in text and 'id="signin"' not in text
        assert client.get("/launch/status").json() == {"signed_in": True}

    LIVE = {**FORM, "run_mode": "live", "oauth_client_id": "abc"}

    def test_live_run_without_sign_in_goes_back_to_the_setup_steps(self, client, monkeypatch):
        from epic_compliance import pipeline, store
        monkeypatch.setattr(pipeline, "get_live_token", lambda: None)
        resp = client.post("/assess/run", data=self.LIVE, follow_redirects=False)
        assert resp.headers["location"] == "/assess/plan?notice=signin"
        assert store.list_runs() == []
        page = client.get("/assess/plan?notice=signin")
        assert "Sign in to the Epic sandbox before running" in page.text and 'value="abc"' in page.text

    def test_clinician_live_run_needs_a_test_patient(self, client, monkeypatch):
        from epic_compliance import pipeline
        monkeypatch.setattr(pipeline, "get_live_token", lambda: object())
        resp = client.post("/assess/run", data=self.LIVE, follow_redirects=False)
        assert resp.headers["location"] == "/assess/plan?notice=patient"

    def test_rerun_without_sign_in_does_not_overwrite_results(self, client, monkeypatch):
        from epic_compliance import pipeline, store
        client.post("/assess/run", data=self.FORM)  # a sample-data run to keep
        store.save_last_intake({**store.get_last_intake(), "run_mode": "live"})
        monkeypatch.setattr(pipeline, "get_live_token", lambda: None)
        resp = client.post("/assess/rerun", data={"next": "/tests"}, follow_redirects=False)
        assert resp.headers["location"] == "/assess/plan?notice=signin"
        assert len(store.list_runs()) == 1

    def test_plan_page_without_saved_answers_goes_to_the_chat(self, client):
        assert client.get("/assess/plan", follow_redirects=False).headers["location"] == "/assess"

    def test_sign_in_expires_with_epics_token(self, monkeypatch):
        from types import SimpleNamespace

        from epic_compliance import pipeline
        now = [1000.0]
        monkeypatch.setattr(pipeline.time, "time", lambda: now[0])
        pipeline.set_live_token(SimpleNamespace(expires_in=3600))
        assert pipeline.get_live_token() is not None
        now[0] += 3600
        assert pipeline.get_live_token() is None

    def test_sign_in_uses_the_client_id_from_the_plan_page(self, client, monkeypatch):
        from types import SimpleNamespace
        from urllib.parse import parse_qs, urlparse

        from epic_compliance import store
        monkeypatch.setattr("epic_compliance.api.fetch_smart_configuration",
                            lambda base, mock: SimpleNamespace(
                                authorization_endpoint="https://epic.test/authorize",
                                token_endpoint="https://epic.test/token"))
        resp = client.get("/launch", params={"client_id": "abc-123", "profile": "clinician_standalone"},
                          follow_redirects=False)
        assert resp.status_code == 302
        q = parse_qs(urlparse(resp.headers["location"]).query)
        assert q["client_id"] == ["abc-123"] and "user/" in q["scope"][0]
        # Saved, so the plan page, Settings and Integrations show the same ID.
        assert store.get_config_defaults()["oauth_client_id"] == "abc-123"
        assert 'value="abc-123"' in client.post("/assess/plan", data=self.FORM).text

    def test_sign_in_without_a_client_id_explains_what_to_add(self, client, monkeypatch):
        monkeypatch.setattr("epic_compliance.api._epic_connection",
                            lambda: {"client_id": "", "redirect_uri": "", "fhir_base_url": ""})
        resp = client.get("/launch", follow_redirects=False)
        assert resp.status_code == 400 and "client ID" in resp.text

    def test_intake_defaults_to_the_epic_sandbox(self):
        from epic_compliance.assessment import Intake
        assert Intake().run_mode == "live" and Intake.from_form({}).run_mode == "live"

    def test_plan_lists_selected_and_skipped_checks(self, client):
        resp = client.post("/assess/plan", data=self.FORM)
        assert resp.status_code == 200
        assert "CLIN-005" in resp.text and "Why:" in resp.text
        # Only the tests that apply are shown; the skipped ones aren't listed.
        assert "skipped for this app" not in resp.text and "CLIN-009" not in resp.text

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


class TestSavedConversation:
    """The chat is saved locally and earlier answers can be edited (ADR-013)."""

    @pytest.fixture
    def client(self, tmp_path, monkeypatch):
        monkeypatch.setenv("EPIC_COMPLIANCE_DB", str(tmp_path / "runs.db"))
        monkeypatch.setattr("epic_compliance.api.get_config", lambda: _NoKeyConfig())
        from fastapi.testclient import TestClient

        from epic_compliance.api import app

        return TestClient(app)

    def _say(self, client, text):
        from epic_compliance import store
        state = store.latest_chat_state() or ""
        return client.post("/assess/chat", data={"message": text, "state": state})

    def _saved(self):
        from epic_compliance import store
        return ChatState.load(store.latest_chat_state())

    def test_conversation_survives_reload(self, client):
        self._say(client, "A mobile app dermatologists use to photograph lesions")
        page = client.get("/assess")
        assert "photograph lesions" in page.text and "What are you building?" not in page.text
        assert "New conversation" in page.text and 'hx-post="/assess/edit"' in page.text

    def test_edit_rewinds_to_before_the_message(self, client):
        self._say(client, "A mobile app clinicians use to view the chart")
        self._say(client, "It also writes visit notes back")
        assert self._saved().intake.writes_back

        # Edit the second answer (index 3: opener, user, assistant, user).
        resp = client.post("/assess/edit", data={"index": 3, "message": "Not sure yet"})
        assert resp.status_code == 200
        state = self._saved()
        assert not state.intake.writes_back          # the edited answer is gone
        assert state.intake.platform == "mobile"     # earlier answers kept
        assert [m["content"] for m in state.messages if m["role"] == "user"] == \
            ["A mobile app clinicians use to view the chart", "Not sure yet"]

    def test_edit_first_message_starts_from_scratch(self, client):
        self._say(client, "A mobile app clinicians use")
        client.post("/assess/edit", data={"index": 1, "message": "A web app patients use via MyChart"})
        i = self._saved().intake
        assert i.users == "patients"
        assert i.description == "A web app patients use via MyChart"

    def test_edit_ignores_bad_index(self, client):
        self._say(client, "A mobile app clinicians use")
        before = self._saved()
        client.post("/assess/edit", data={"index": 0, "message": "x"})  # assistant message
        client.post("/assess/edit", data={"index": 99, "message": "x"})
        assert self._saved() == before

    def test_new_conversation_keeps_the_old_one(self, client):
        from epic_compliance import store
        self._say(client, "A mobile app clinicians use")
        resp = client.post("/assess/new", follow_redirects=False)
        assert resp.status_code == 303
        assert "What are you building?" in client.get("/assess").text
        assert any("A mobile app clinicians use" in raw for raw in store.chat_snapshots())

    def test_home_counts_a_described_app_before_any_run(self, client, monkeypatch):
        from epic_compliance.config import get_config
        self._say(client, "We're building DermScan, a mobile app clinicians use")
        monkeypatch.setattr("epic_compliance.api.get_config", get_config)  # Home needs the full config
        assert "DermScan" in client.get("/").text

    def test_plan_and_back_to_chat_save_a_conversation_from_an_old_tab(self, client):
        from epic_compliance import store
        from epic_compliance.intake_chat import next_turn
        old_tab = next_turn(ChatState.start(), "A mobile app clinicians use", api_key="")
        client.post("/assess/plan", data={**TestAssessmentRoutes.FORM, "state": old_tab.model_dump_json()})
        assert self._saved() == old_tab
        assert len(store.chat_snapshots()) == 1
        client.post("/assess/resume", data={"state": old_tab.model_dump_json()})
        assert len(store.chat_snapshots()) == 1  # already saved, not duplicated
