"""Reviewer-question library and its selection (ADR-019)."""
from __future__ import annotations

from epic_compliance.assessment import Intake, build_plan
from epic_compliance.reviewer_questions import TOPICS, load_library, select
from epic_compliance.rules_engine.engine import run_pipeline

# A mobile app clinicians use to photograph skin lesions into the chart.
DERMSCAN = Intake(app_name="DermScan", users="clinicians", platform="mobile", saves_images=True)
# A browser-only patient app that only reads the chart.
PATIENT_WEB = Intake(app_name="MyLabs", users="patients", platform="web_browser")


def _included(intake: Intake) -> dict[str, str]:
    return {p.question.id: p.reason for p in select(intake) if p.included}


class TestLibrary:
    def test_loads_with_unique_ids_and_known_topics(self):
        qs = load_library()
        assert len(qs) > 100
        assert len({q.id for q in qs}) == len(qs)
        assert {q.topic for q in qs} <= set(TOPICS)

    def test_every_question_names_a_source(self):
        assert all(q.sources and all(s.name for s in q.sources) for q in load_library())

    def test_unverified_sources_are_the_exception(self):
        unverified = [q.id for q in load_library() if not q.verified]
        assert len(unverified) <= 10, unverified


class TestSelection:
    def test_a_mobile_lesion_photo_app_gets_the_photo_and_device_questions(self):
        got = _included(DERMSCAN)
        # Camera roll, EXIF/GPS, FDA status, patient ID before capture, body site.
        for qid in ("MOB-02", "IMG-05", "FDA-01", "IMG-01", "IMG-08"):
            assert qid in got, qid

    def test_a_web_patient_app_gets_no_mobile_or_photo_questions(self):
        got = _included(PATIENT_WEB)
        assert not [q for q in got if q.startswith(("MOB-", "IMG-", "NATIVE-"))]
        assert "EPIC-DUQ" in got  # Epic's Data Use Questionnaire is for patient apps

    def test_every_decision_has_a_reason(self):
        assert all(p.reason for p in select(DERMSCAN))

    def test_unknown_facts_include_the_question_and_say_so(self):
        # The intake doesn't ask about AI analysis yet: FDA's device questions stay in.
        reason = _included(DERMSCAN)["FDA-02"]
        assert "don't know yet" in reason and "analyses" in reason

    def test_questions_an_automated_test_already_asks_are_left_out(self):
        plan = build_plan(DERMSCAN)
        assert "SEC-001" in plan.rule_ids and "EPIC-TLS" not in plan.rule_ids
        epic_tls = next(q for q in plan.questions if q.question.id == "EPIC-TLS")
        assert epic_tls.reason == "Already asked by SEC-001."


class TestPlanAndRun:
    def test_plan_runs_the_applicable_questions_as_tests(self):
        plan = build_plan(DERMSCAN)
        assert {"MOB-02", "IMG-01"} <= plan.rule_ids
        assert {q["id"] for q in plan.to_dict()["questions"]} >= {"MOB-02", "IMG-01"}

    def test_an_unanswered_question_needs_review_without_calling_claude(self):
        report = run_pipeline({}, api_key="sk-not-used", mock_llm=False, launch_profile="clinician_standalone",
                              rule_ids={"MOB-02"})
        (f,) = report.findings
        assert f.verdict == "needs_human" and f.evidence.startswith("Not answered yet")
