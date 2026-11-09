"""Assessment flow: intake answers -> test plan -> run -> report."""
from __future__ import annotations

import pytest

from epic_compliance.assessment import SELECTORS, Intake, build_plan
from epic_compliance.intake_chat import ANSWER_FIELDS, ChatState, data_use_text, next_turn
from epic_compliance.rules_engine.catalog import is_reviewer_question, load_rules


def _ids(plan) -> set[str]:
    return plan.rule_ids


class TestBuildPlan:
    def test_every_catalog_rule_has_a_selector(self):
        # A new rule without a selector is included by default, but it should be
        # a deliberate choice — keep SELECTORS in step with the catalog. Reviewer
        # questions are selected by their library tags instead (ADR-019).
        assert {r.id for r in load_rules() if not is_reviewer_question(r)} <= set(SELECTORS)

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
            platform="mobile", third_party_ai=True, health_system="Example Health",
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
                          "We're building DermScan, a mobile app dermatologists at Example Health "
                          "use to photograph a lesion, save the photo to the chart and get an AI read.")
        i = state.intake
        assert (i.users, i.platform) == ("clinicians", "mobile")
        assert i.saves_images and i.third_party_ai
        assert i.health_system == "Example Health" and i.app_name == "DermScan"
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
        "app_name": "DermScan", "health_system": "Example Health",
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

    def test_plan_offers_only_the_epic_run(self, client, monkeypatch):
        from epic_compliance import pipeline
        monkeypatch.setattr(pipeline, "get_live_token", lambda: None)
        text = client.post("/assess/plan", data=self.FORM).text
        assert text.index("Before you run") < text.index("What will be checked")
        # Sample data is for the test suite only (ADR-014); the page never offers it.
        assert 'value="mock"' not in text and "sample data" not in text.lower()
        assert 'id="run-btn" class="btn big" disabled' in text and "Sign in to Epic sandbox" in text
        # No test-patient step: Epic's sandbox patient is the default, editable under More settings.
        assert 'id="step-patient"' not in text and 'value="identifier=203713"' in text
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

    def test_epic_runs_default_to_the_sandbox_patient_and_sample_runs_dont(self):
        from epic_compliance.assessment import SANDBOX_TEST_PATIENT
        live = Intake(users="clinicians", run_mode="live")
        assert build_plan(live).config_overrides()["test_patient_query"] == SANDBOX_TEST_PATIENT
        custom = live.model_copy(update={"test_patient_query": "identifier=1"})
        assert build_plan(custom).config_overrides()["test_patient_query"] == "identifier=1"
        sample = Intake(users="clinicians", run_mode="mock")
        assert build_plan(sample).config_overrides()["test_patient_query"] == ""

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

    def test_sign_in_without_a_client_id_goes_where_the_client_id_is_entered(self, client, monkeypatch):
        monkeypatch.setattr("epic_compliance.api._epic_connection",
                            lambda: {"client_id": "", "redirect_uri": "", "fhir_base_url": ""})
        # No app described yet: Settings. With a plan: its Epic setup steps.
        assert client.get("/launch", follow_redirects=False).headers["location"] == "/config"
        client.post("/assess/run", data=self.FORM)
        assert client.get("/launch", follow_redirects=False).headers["location"] == "/assess/plan#setup"

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
        assert self._saved() == old_tab.model_copy(update={"planned": True})
        assert len(store.chat_snapshots()) == 1
        client.post("/assess/resume", data={"state": old_tab.model_dump_json()})
        assert len(store.chat_snapshots()) == 1  # already saved, not duplicated


class TestNoEpicConnection:
    """An app that reads the patient off the Epic screen and has another system
    file its documents doesn't connect to Epic: no sign-in, read or write tests."""

    SCREEN_APP = ("BelleOne is a native mobile app clinicians use. The clinician scans the patient "
                  "header on their Epic screen with the app's camera to capture the name, MRN and "
                  "CSN. The app sends a PDF by sFTP to the health system's OnBase folder.")

    def test_plan_drops_epic_api_tests_and_keeps_security(self):
        plan = build_plan(Intake(users="clinicians", platform="mobile", epic_connection="none",
                                 third_party_ai=True))
        ids = _ids(plan)
        assert not {i for i in ids if i.split("-")[0] in ("AUTH", "CLIN", "FHIR", "WRITE")}
        assert {"SEC-001", "SEC-002", "SEC-003"} <= ids
        auth = next(c for c in plan.checks if c.rule.id == "AUTH-001")
        assert not auth.included and "doesn't connect to Epic" in auth.reason

    def test_screen_scraping_question_is_asked_and_api_questions_are_not(self):
        plan = build_plan(Intake(users="clinicians", platform="mobile", epic_connection="none"))
        qs = {q.question.id: q for q in plan.questions}
        assert qs["EPIC-EXCHANGE-METHOD"].included and "Epic screen" in qs["EPIC-EXCHANGE-METHOD"].reason
        assert not qs["EPIC-NATIVE-PKCE"].included and not qs["EPIC-SCOPE-MIN"].included
        assert not build_plan(Intake(users="clinicians", platform="mobile")).checks[0].reason.startswith("Doesn't apply")

    def test_run_makes_no_epic_calls(self, monkeypatch):
        from epic_compliance import pipeline
        from epic_compliance.config import config_with_overrides

        def no_epic(*a, **k):
            raise AssertionError("the run called Epic")
        monkeypatch.setattr(pipeline, "fetch_smart_configuration", no_epic)
        monkeypatch.setattr(pipeline, "get_live_token", no_epic)
        plan = build_plan(Intake(users="clinicians", platform="mobile", epic_connection="none",
                                 tls_version="TLS 1.2", run_mode="live"))
        evidence = pipeline.collect_evidence(config_with_overrides(plan.config_overrides()))
        assert evidence["token_response"] == {"_unavailable": pipeline.NO_EPIC_CONNECTION_REASON}
        assert "sign in" not in str(evidence).lower()
        assert evidence["app_config"]["transport_tls_version"] == "TLS 1.2"

    def test_answer_review_decides_epic_connection(self, monkeypatch):
        import json
        from types import SimpleNamespace

        import anthropic

        from epic_compliance.intake_chat import read_app_texts
        sent = {}
        out = {"epic_connection": "none", "reads_chart": "no", "saves_images": "no",
               "writes_back": "no", "writes_pdf": "no", "third_party_ai": "yes"}

        class FakeMessages:
            def create(self, **kw):
                sent.update(kw)
                return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(out))])

        monkeypatch.setattr(anthropic, "Anthropic", lambda **kw: SimpleNamespace(messages=FakeMessages()))
        read = read_app_texts(self.SCREEN_APP, "It reads the MRN off the Epic screen.", api_key="sk-test")
        assert read["epic_connection"] == "none" and read["third_party_ai"] and not read["reads_chart"]
        assert "epic_connection" in sent["output_config"]["format"]["schema"]["required"]
        assert self.SCREEN_APP in sent["messages"][0]["content"]
        out["epic_connection"] = "unknown"
        assert read_app_texts("An app.", "", api_key="sk-test")["epic_connection"] is None

    @pytest.mark.parametrize("text, expected", [
        ("Clinicians sign in with Epic and the app reads their medications.", "api"),
        ("It's a SMART on FHIR app launched from Hyperspace.", "api"),
        ("A patient app that patients open from MyChart.", "api"),
        ("The clinician photographs the Epic screen to read the MRN.", "none"),
        ("It doesn't connect to Epic; OnBase files the PDF.", "none"),
        ("It doesn't use FHIR.", None),
        ("A dermatology app for clinicians.", None),
    ])
    def test_offline_reading_of_epic_connection(self, text, expected):
        from epic_compliance.intake_chat import _epic_connection_in
        assert _epic_connection_in(text.lower()) == expected

    def test_chat_hears_the_screen_scan(self):
        state = next_turn(ChatState.start(), self.SCREEN_APP, api_key="")
        assert state.intake.epic_connection == "none"

    def test_apps_that_sign_in_still_connect(self):
        state = next_turn(ChatState.start(), "DermScan, a mobile app clinicians use to read the "
                          "chart and save lesion photos to the chart", api_key="")
        assert state.intake.epic_connection == "api"


class TestPdfDocument:
    """An app that files a PDF (photo + conclusion) in the chart is tested as a
    document write, not a photo upload, even when the first message said photos."""

    def _chat(self, *messages):
        state = ChatState.start()
        for m in messages:
            state = next_turn(state, m, api_key="")
        return state.intake

    def test_pdf_answer_corrects_an_earlier_photo_answer(self):
        i = self._chat(
            "A mobile dermatology app that saves lesion photos to the chart",
            "No the clinicians just see the photos and conclusion of the app, then the app "
            "export the photo and conclusion as a pdf then send to the chart",
        )
        assert i.writes_pdf and i.writes_back and not i.saves_images
        ids = _ids(build_plan(i))
        assert {"CLIN-009", "CLIN-010"} <= ids and not {"CLIN-005", "CLIN-006"} & ids
        assert build_plan(i).config_overrides()["note_format"] == "pdf"

    def test_photos_also_saved_separately_stay_in_the_plan(self):
        i = self._chat("A mobile app clinicians use. It sends a PDF report to the chart "
                       "and also saves the photo separately to the chart")
        assert i.writes_pdf and i.saves_images

    def test_pdf_filed_by_another_system_is_not_a_write(self):
        i = self._chat("BelleOne, a native mobile app clinicians use. It builds a visit summary PDF. "
                       "The PDF goes by sFTP to the health system's OnBase drop-off folder and "
                       "OnBase files it; BelleOne never writes to Epic.")
        assert not (i.writes_pdf or i.writes_back)

    def test_negation_stays_in_its_clause(self):
        i = self._chat("A mobile app clinicians use. It never writes to Epic; it only reads "
                       "the MRN, visit number, name and date of birth.")
        assert i.reads_chart and not i.writes_back

    def test_writes_pdf_implies_a_document_write(self):
        assert Intake(writes_pdf=True).writes_back
        assert Intake.from_form({"writes_pdf": "on"}).writes_back
        assert build_plan(Intake()).config_overrides()["note_format"] == "text"

    def test_summary_names_the_pdf(self):
        from epic_compliance.intake_chat import summary
        data = dict(summary(Intake(writes_pdf=True)))["Patient data"]
        assert "PDF" in data and "notes / results" not in data

    def test_llm_pdf_answer_is_a_document_write(self, monkeypatch):
        import json
        from types import SimpleNamespace

        import anthropic
        out = {"updates": {"saves_images": "no", "writes_pdf": "yes", "writes_back": "no"},
               "covered": ["data"], "reply": "Who signs in?", "suggestions": [], "done": False}

        class FakeMessages:
            def create(self, **kw):
                return SimpleNamespace(stop_reason="end_turn",
                                       content=[SimpleNamespace(type="text", text=json.dumps(out))])

        monkeypatch.setattr(anthropic, "Anthropic", lambda **kw: SimpleNamespace(messages=FakeMessages()))
        i = next_turn(ChatState.start(), "It sends a PDF to the chart", api_key="sk-test").intake
        assert i.writes_pdf and i.writes_back and not i.saves_images

    def test_llm_schema_asks_about_pdfs(self):
        from epic_compliance.intake_chat import OUTPUT_SCHEMA, SYSTEM_PROMPT
        assert "writes_pdf" in OUTPUT_SCHEMA["properties"]["updates"]["required"]
        assert "writes_pdf" in SYSTEM_PROMPT

    def test_note_write_sends_a_real_pdf(self):
        import base64

        from epic_compliance.fhir.writeback import build_consult_note
        att = build_consult_note("p1", pdf=True)["content"][0]["attachment"]
        data = base64.b64decode(att["data"])
        assert att["contentType"] == "application/pdf"
        assert data.startswith(b"%PDF-") and data.rstrip().endswith(b"%%EOF")
        assert build_consult_note("p1")["content"][0]["attachment"]["contentType"] == "text/plain"

    def test_epics_refusal_reason_is_recorded(self):
        from epic_compliance.pipeline import _outcome_text
        body = {"resourceType": "OperationOutcome",
                "issue": [{"code": "forbidden", "diagnostics": "Client not authorized"},
                          {"code": "not-supported", "details": {"text": "Media create not supported"}}]}
        assert _outcome_text(body) == "Client not authorized; Media create not supported"
        assert _outcome_text({"_raw": "Method Not Allowed"}) == "Method Not Allowed"
        assert _outcome_text({"resourceType": "DocumentReference", "id": "x"}) == ""
        assert _outcome_text(None) == ""
class TestStepsAfterDescribing:
    """Once the app is described, steps 2 and 3 open from the step bar, and the
    summary offers to edit the description instead of generating the plan again."""

    @pytest.fixture
    def client(self, tmp_path, monkeypatch):
        monkeypatch.setenv("EPIC_COMPLIANCE_DB", str(tmp_path / "runs.db"))
        monkeypatch.setattr("epic_compliance.api.get_config", lambda: _NoKeyConfig())
        from fastapi.testclient import TestClient

        from epic_compliance.api import app

        return TestClient(app)

    def _finish(self, client) -> ChatState:
        from epic_compliance import store
        state = ChatState.start()
        for text in ["DermScan, a mobile app clinicians use to save lesion photos to the chart",
                     "Not sure", "Not sure", "Not sure"]:
            client.post("/assess/chat", data={"message": text, "state": state.model_dump_json()})
            state = ChatState.load(store.latest_chat_state())
            if state.ready:
                return state
        pytest.fail("chat never finished")

    def _generate(self, client, state):
        client.post("/assess/plan", data={**TestAssessmentRoutes.FORM, "state": state.model_dump_json()})

    def test_step_two_opens_once_the_app_is_described(self, client):
        assert 'href="/assess/plan"' not in client.get("/assess").text
        self._finish(client)
        page = client.get("/assess").text
        assert 'href="/assess/plan"' in page and "Generate test plan" in page
        assert 'href="/runs/' not in page  # no run yet, so no step 3 link

    def test_step_two_plans_from_the_finished_chat(self, client):
        from epic_compliance import store
        store.save_last_intake({**Intake(users="patients", platform="web_browser").model_dump(),
                                "oauth_client_id": "abc"})
        self._finish(client)
        resp = client.get("/assess/plan")
        assert resp.status_code == 200
        assert "CLIN-001" in resp.text  # the chat's clinician app, not the old patient app
        assert 'value="abc"' in resp.text  # where to run still comes from the last run

    def test_summary_has_separate_generate_and_edit_buttons(self, client):
        from epic_compliance import store
        state = self._finish(client)
        for planned in (False, True):
            if planned:
                self._generate(client, state)
                assert ChatState.load(store.latest_chat_state()).planned
            page = client.get("/assess").text
            assert "Generate test plan" in page
            assert "data-edit-answers" in page and ">Edit answers<" in page
            assert 'hx-post="/assess/answers"' in page
            assert "Edit app description" not in page and "confirm(" not in page.split("understood")[1].split("</form>")[0]

    def _answers(self, state: ChatState, **changes) -> dict[str, str]:
        """The Edit answers form as the browser posts it, with the patient-data
        box prefilled the way the page fills it."""
        data = {**state.intake.model_dump(), "data_use": data_use_text(state.intake), **changes}
        return {k: v for k, v in data.items() if k in ANSWER_FIELDS}

    def test_edit_answers_sets_fields_and_reads_the_data_description(self, client):
        from epic_compliance import store
        state = self._finish(client)
        assert state.intake.saves_images  # the chat heard "save lesion photos to the chart"
        before = len(state.messages)
        desc = ("DermScan reads the patient's MRN and visit. The PDF goes by sFTP to the "
                "health system's OnBase folder; DermScan never writes to Epic.")
        data_use = ("It reads the patient's MRN, visit number, name and date of birth from Epic. "
                    "The clinician takes a lesion photo. The app builds a PDF and sends it by sFTP "
                    "to the health system's OnBase folder, and OnBase files it; the app never "
                    "writes to Epic.")
        page = client.get("/assess").text
        assert 'name="data_use"' in page and "It saves photos to the chart as images." in page
        resp = client.post("/assess/answers", data=self._answers(
            state, description=desc, data_use=data_use, health_system="Example Health"))
        saved = ChatState.load(store.latest_chat_state())
        i = saved.intake
        assert i.description == desc and i.health_system == "Example Health" and i.data_use == data_use
        assert i.reads_chart and not (i.saves_images or i.writes_back or i.writes_pdf)
        assert "Currently read as</span> Reads the chart" in resp.text
        assert (i.users, i.platform) == (state.intake.users, state.intake.platform)
        assert saved.ready and saved.known == state.known  # nothing is asked again
        # One note in the chat saying what changed, and no Edit link on it.
        assert len(saved.messages) == before + 2 and saved.messages[-2]["kind"] == "answers"
        assert "Patient data: Reads the chart" in saved.messages[-2]["content"]
        assert 'class="changed">Patient data<' in resp.text and "Saved. Changed:" in resp.text
        ids = _ids(build_plan(i))
        assert not {"WRITE-001", "WRITE-002", "CLIN-005", "CLIN-009"} & ids
        assert "OnBase folder; DermScan never writes to Epic." in client.get("/assess/plan").text  # the plan quotes the new description

    def test_edit_answers_with_nothing_changed_adds_no_note(self, client):
        from epic_compliance import store
        state = self._finish(client)
        client.post("/assess/answers", data=self._answers(state))
        saved = ChatState.load(store.latest_chat_state())
        # The untouched starting sentence isn't the user's words and changes nothing.
        assert saved.messages == state.messages and saved.intake == state.intake

    def test_texts_are_read_only_when_they_change(self, client, monkeypatch):
        from epic_compliance import intake_chat, store
        calls = []
        monkeypatch.setattr(intake_chat, "read_app_texts", lambda d, u, key="": calls.append(d) or {})
        state = self._finish(client)
        client.post("/assess/answers", data=self._answers(state, app_name="DermScan Pro"))
        assert calls == [] and ChatState.load(store.latest_chat_state()).intake.app_name == "DermScan Pro"

    SCREEN_DESC = ("DermScan is a mobile app clinicians use. The clinician scans the patient header on "
                   "their Epic screen with the camera. A PDF goes by sFTP to the health system's OnBase folder.")
    SMART_DESC = ("DermScan is a mobile app clinicians use. They sign in with Epic (SMART on FHIR), pick "
                  "the patient and save lesion photos to the chart.")

    def test_description_decides_whether_the_epic_sandbox_is_needed(self, client):
        from epic_compliance import store
        state = self._finish(client)
        assert state.intake.epic_connection == "api"
        resp = client.post("/assess/answers", data=self._answers(state, description=self.SCREEN_DESC))
        saved = ChatState.load(store.latest_chat_state())
        assert saved.intake.epic_connection == "none"
        assert 'class="changed">Connects to Epic<' in resp.text
        # And back: a description that signs in with Epic needs the sandbox again.
        client.post("/assess/answers", data=self._answers(saved, description=self.SMART_DESC))
        assert ChatState.load(store.latest_chat_state()).intake.epic_connection == "api"

    def test_connects_to_epic_picked_by_hand_wins_over_the_reading(self, client):
        from epic_compliance import store
        state = self._finish(client)
        client.post("/assess/answers", data=self._answers(
            state, description=self.SMART_DESC, epic_connection="none"))
        assert ChatState.load(store.latest_chat_state()).intake.epic_connection == "none"

    def test_a_description_that_doesnt_say_keeps_the_answer(self, client):
        from epic_compliance import store
        state = self._finish(client)
        client.post("/assess/answers", data=self._answers(state, epic_connection="none"))
        saved = ChatState.load(store.latest_chat_state())
        client.post("/assess/answers", data=self._answers(saved, description="DermScan helps dermatologists."))
        assert ChatState.load(store.latest_chat_state()).intake.epic_connection == "none"

    @pytest.mark.parametrize("bad", [{"users": "robots"}, {"audit_retention_days": "ten"},
                                     {"tls_version": "TLS 9"}, {"baa_status": "maybe"},
                                     {"epic_connection": "maybe"}])
    def test_edit_answers_rejects_invalid_values(self, client, bad):
        from epic_compliance import store
        state = self._finish(client)
        client.post("/assess/answers", data=self._answers(state, **bad))
        assert ChatState.load(store.latest_chat_state()).intake == state.intake

    def test_app_that_doesnt_connect_runs_without_epic_sign_in(self, client):
        from epic_compliance import store
        state = self._finish(client)
        assert 'name="epic_connection" value="api" checked' in client.get("/assess").text
        resp = client.post("/assess/answers", data=self._answers(state, epic_connection="none"))
        assert 'class="changed">Connects to Epic<' in resp.text
        assert ChatState.load(store.latest_chat_state()).intake.epic_connection == "none"
        plan_page = client.get("/assess/plan").text
        assert "No Epic sign-in needed" in plan_page and "Sign in to Epic sandbox" not in plan_page
        assert 'id="run-btn" class="btn big">Run' in plan_page  # not disabled
        from epic_compliance.api import _not_ready_to_run
        assert _not_ready_to_run(Intake(run_mode="live", epic_connection="none")) == ""

    def test_epic_setup_is_hidden_when_the_app_doesnt_connect(self, client, monkeypatch):
        from epic_compliance.config import get_config
        cfg = get_config().model_copy(update={"anthropic_api_key": "", "oauth_client_id": ""})
        monkeypatch.setattr("epic_compliance.api.get_config", lambda: cfg)  # Integrations reads every field
        state = self._finish(client)
        # Before: the app connects, so Epic setup is asked for everywhere.
        assert "Connect the Epic sandbox" in client.get("/").text
        assert "Not needed" not in client.get("/integrations").text
        client.post("/assess/answers", data=self._answers(state, epic_connection="none"))
        home = client.get("/").text
        assert "Connect the Epic sandbox" not in home and "nothing to sign in to" in home
        integrations = client.get("/integrations").text
        assert integrations.count(">Not needed<") == 3  # Epic sandbox, FHIR validator, ONC test kit
        assert "Sign in with SMART" not in integrations
        assert integrations.index("Answer review") < integrations.index("Epic sandbox")  # not-needed last
        settings = client.get("/config").text
        assert "doesn't connect to Epic's API" in settings and "<summary>Epic connection settings</summary>" in settings

    def test_run_pages_dont_say_epic_sandbox_for_an_app_that_doesnt_connect(self, monkeypatch):
        from epic_compliance.api import _run_uses_epic
        assert _run_uses_epic({"config": {"assessment": {"intake": {"epic_connection": "none"}}}}) is False
        assert _run_uses_epic({"config": {"assessment": {"intake": {}}}}) is True  # older runs
        assert _run_uses_epic({"config": {}}) is True and _run_uses_epic(None) is True

    def test_editing_an_answer_keeps_the_edit_button(self, client):
        from epic_compliance import store
        self._generate(client, self._finish(client))
        client.post("/assess/edit", data={"index": 1, "message": "DermScan, a mobile app clinicians use"})
        assert ChatState.load(store.latest_chat_state()).planned

    def test_step_three_links_to_the_latest_run(self, client, monkeypatch):
        from epic_compliance import api
        monkeypatch.setattr(api, "_nav", lambda: {"app_name": "x", "health_system": "", "failing": 0,
                                                  "review": 0, "latest_run_id": "run123"})
        self._finish(client)
        assert 'href="/runs/run123"' in client.get("/assess").text
