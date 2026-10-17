"""Smoke test: the whole tool, end to end, on sample Epic responses (ADR-014).

This is what "demo data" used to be in the web UI. It checks that the tool
itself works — chat -> plan -> run -> report -> workspace pages -> CLI — with
no network, no Epic credentials and no Claude key, so it can gate CI.

It does NOT say anything about an app's Epic integration; only a run against
the Epic sandbox does that.

    pytest -m smoke
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from epic_compliance.intake_chat import ChatState

pytestmark = pytest.mark.smoke

REPO = Path(__file__).parent.parent

#: Two apps that exercise different halves of the catalog.
APPS = {
    "clinician_mobile": [
        "We're building DermScan, a mobile app dermatologists at Advocate Health use to "
        "photograph a lesion, save the photo to the chart and get an AI read.",
        "TLS 1.3, audit logging on and kept 6 years, BAA signed with all vendors",
    ],
    "patient_web": [
        "A web app with a Django backend that patients use via MyChart to read labs "
        "and write results back",
        "Not sure yet",
    ],
}


@pytest.fixture
def client(tmp_path, monkeypatch):
    """The web app on an empty DB, offline: sample responses, no Claude."""
    from epic_compliance import config

    monkeypatch.setenv("EPIC_COMPLIANCE_DB", str(tmp_path / "runs.db"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("RUN_MODE", "mock")
    monkeypatch.setattr(config, "_config", None)  # re-read the env above
    from fastapi.testclient import TestClient

    from epic_compliance.api import app

    return TestClient(app)


def _plan_form(state: ChatState) -> dict[str, str]:
    """What the chat's "Generate test plan" form posts, on sample data."""
    form = {k: ("on" if v is True else str(v))
            for k, v in state.intake.model_dump().items() if v is not False}
    return {**form, "run_mode": "mock", "state": state.model_dump_json()}


@pytest.mark.parametrize("app_kind", APPS)
def test_describe_plan_run_and_report(client, app_kind):
    from epic_compliance import store

    # 1. Describe the app; the offline chat asks follow-ups until it's ready.
    assert "What are you building?" in client.get("/assess").text
    for message in APPS[app_kind]:
        state = store.latest_chat_state() or ""
        assert client.post("/assess/chat", data={"message": message, "state": state}).status_code == 200
    chat = ChatState.load(store.latest_chat_state())
    assert chat.ready, chat.messages[-1]["content"]

    # 2. The plan lists what will run, and why.
    form = _plan_form(chat)
    plan = client.post("/assess/plan", data=form)
    assert plan.status_code == 200 and "Why:" in plan.text

    # 3. Run it; every planned check produces a finding.
    run = client.post("/assess/run", data=form, follow_redirects=False)
    assert run.status_code == 303
    run_id = run.headers["location"].rsplit("/", 1)[-1]
    detail = client.get(f"/api/runs/{run_id}").json()
    planned = {c["rule_id"] for c in detail["config"]["assessment"]["included"]}
    ran = {f["rule_id"] for f in detail["findings"]}
    assert planned and ran == planned
    assert {f["verdict"] for f in detail["findings"]} <= {"pass", "fail", "needs_human"}

    # 4. Every page renders with the run in place.
    pages = ["/", "/frameworks", "/tests", "/tests?status=failing", "/integrations",
             "/runs", f"/runs/{run_id}", f"/runs/{run_id}/export", f"/runs/{run_id}/print",
             "/assess", "/config", "/healthz", "/api/runs"]
    from epic_compliance.workspace import FRAMEWORKS
    pages += [f"/frameworks/{f.key}" for f in FRAMEWORKS]
    pages += [f"/tests/{rid}" for rid in sorted(planned)]
    for page in pages:
        assert client.get(page).status_code == 200, page

    # 5. Re-running the saved plan adds a second run.
    assert client.post("/assess/rerun", data={"next": "/"}, follow_redirects=False).status_code == 303
    assert len(client.get("/api/runs").json()) == 2


def test_cli_run(tmp_path):
    """`python -m epic_compliance run` completes on sample data."""
    env = {**os.environ, "RUN_MODE": "mock", "ANTHROPIC_API_KEY": "",
           "EPIC_COMPLIANCE_DB": str(tmp_path / "runs.db")}
    out = subprocess.run([sys.executable, "-m", "epic_compliance", "run"], cwd=REPO, env=env,
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    assert "Run ID:" in out.stdout
