"""Evidence attached to tests, and the missing-answer guard (ADR-015)."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from epic_compliance import evidence as ev
from epic_compliance import store
from epic_compliance.rules_engine import engine
from epic_compliance.rules_engine.catalog import load_rules

RULES = {r.id: r for r in load_rules()}
NO_ANSWERS = {"app_config": {"_unavailable": "No app_config fields attested"}}


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setenv("EPIC_COMPLIANCE_DB", str(tmp_path / "runs.db"))


@pytest.fixture
def claude(monkeypatch):
    """Fake Claude: records what it was sent, answers with ``reply``."""
    import anthropic
    calls: list[dict] = []
    state = {"reply": {"verdict": "pass", "citation": "[1] Statement: logs kept 6 years (attested)"}}

    class Messages:
        def create(self, **kw):
            calls.append(kw)
            return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(state["reply"]))])

    monkeypatch.setattr(anthropic, "Anthropic", lambda **kw: SimpleNamespace(messages=Messages()))
    return SimpleNamespace(calls=calls, state=state)


def _run(evidence, attached=None):
    report = engine.run_pipeline(evidence, api_key="sk-test", mock_llm=False,
                                 rule_ids={"SEC-001", "SEC-002"}, attached_evidence=attached)
    return {f.rule_id: f for f in report.findings}


class TestMissingAnswerGuard:
    def test_no_answer_and_no_evidence_is_needs_review_without_claude(self, claude):
        findings = _run(NO_ANSWERS)
        assert findings["SEC-002"].verdict == "needs_human"
        assert "No answer given for audit_logging_enabled" in findings["SEC-002"].evidence
        assert findings["SEC-001"].verdict == "needs_human"
        assert claude.calls == []

    def test_an_answer_goes_to_claude(self, claude):
        findings = _run({"app_config": {"audit_logging_enabled": "yes"}})
        assert findings["SEC-002"].verdict == "pass" and len(claude.calls) == 1

    def test_attached_evidence_goes_to_claude_even_without_an_answer(self, claude):
        ev.add_note("SEC-002", "All PHI access is logged to CloudWatch and kept 6 years.")
        findings = _run(NO_ANSWERS, ev.by_rule(store.list_evidence()))
        assert findings["SEC-002"].verdict == "pass"
        sent = json.loads(claude.calls[0]["messages"][0]["content"][0]["text"])
        assert "kept 6 years" in sent["developer_evidence"]

    def test_prompt_says_missing_is_never_a_fail(self, claude):
        _run({"app_config": {"audit_logging_enabled": "yes"}})
        assert "NEVER a fail" in claude.calls[0]["system"]

    def test_every_attested_rule_names_real_answer_fields(self):
        real = {"transport_tls_version", "audit_logging_enabled", "audit_log_retention_days",
                "baa_in_place", "covered_entity_relationship"}
        for rule in RULES.values():
            assert set(engine._attested_fields(rule)) <= real, rule.id


class TestEvidenceForClaude:
    def test_text_files_inline_images_and_pdfs_as_blocks(self):
        ev.add_file("SEC-002", "audit.log", b"2026-10-19 READ Patient/123 by Practitioner/9\n")
        ev.add_file("SEC-002", "retention.png", b"\x89PNG fake")
        ev.add_file("SEC-002", "policy.pdf", b"%PDF-1.4 fake")
        ev.add_link("SEC-002", "https://example.com/soc2")
        text, blocks = ev.for_claude(store.list_evidence("SEC-002"))
        assert "READ Patient/123" in text and "Link (not opened by the reviewer)" in text
        kinds = [b["type"] for b in blocks if b["type"] != "text"]
        assert kinds == ["image", "document"]


class TestEvidenceValidation:
    @pytest.mark.parametrize("name,data,msg", [
        ("run.exe", b"MZ", "use a text"),
        ("notes.txt", b"", "empty"),
        ("notes.txt", b"\xff\xfe\x00bad", "UTF-8"),
        ("big.log", b"x" * (ev.MAX_FILE_BYTES + 1), "over"),
    ])
    def test_rejected_files(self, name, data, msg):
        with pytest.raises(ev.EvidenceError, match=msg):
            ev.add_file("SEC-002", name, data)

    def test_filename_cannot_escape_the_evidence_folder(self):
        ev.add_file("SEC-002", "../../etc/passwd.txt", b"hello")
        item = store.list_evidence("SEC-002")[0]
        assert item["filename"] == "passwd.txt"
        assert ev.file_path(item).parent == store.evidence_dir()

    def test_links_must_be_http(self):
        with pytest.raises(ev.EvidenceError):
            ev.add_link("SEC-002", "javascript:alert(1)")


class TestEvidenceRoutes:
    @pytest.fixture
    def client(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "")
        from fastapi.testclient import TestClient

        from epic_compliance.api import app
        return TestClient(app)

    def test_drawer_has_the_evidence_form(self, client):
        page = client.get("/tests/SEC-002")
        assert 'hx-post="/tests/SEC-002/evidence"' in page.text and "No real patient data" in page.text

    def test_add_list_download_and_remove(self, client):
        resp = client.post("/tests/SEC-002/evidence",
                           data={"note": "Logs kept 6 years", "link": "https://example.com/dash"},
                           files=[("files", ("audit.log", b"READ Patient/1", "text/plain"))])
        assert resp.status_code == 200 and "Added." in resp.text
        assert "Logs kept 6 years" in resp.text and "audit.log" in resp.text

        file_item = next(e for e in store.list_evidence("SEC-002") if e["kind"] == "file")
        dl = client.get(f"/evidence/{file_item['id']}/file")
        assert dl.content == b"READ Patient/1" and dl.headers["content-disposition"].startswith("attachment")

        client.post(f"/evidence/{file_item['id']}/delete")
        assert ev.file_path(file_item) is None
        assert client.get(f"/evidence/{file_item['id']}/file").status_code == 404
        assert len(store.list_evidence("SEC-002")) == 2

    def test_errors_are_shown_in_the_section(self, client):
        resp = client.post("/tests/SEC-002/evidence", data={"link": "ftp://x"})
        assert "Links must start with" in resp.text and store.list_evidence() == []
        assert "Add a statement, a file or a link" in client.post("/tests/SEC-002/evidence").text

    def test_unknown_test_is_404(self, client):
        assert client.post("/tests/NOPE-1/evidence", data={"note": "x"}).status_code == 404

    def test_run_records_the_evidence_and_the_report_lists_it(self, client):
        client.post("/tests/SEC-002/evidence", data={"note": "Logs kept 6 years"})
        form = {"users": "clinicians", "platform": "mobile", "reads_chart": "on", "run_mode": "mock"}
        run = client.post("/assess/run", data=form, follow_redirects=False)
        run_id = run.headers["location"].rsplit("/", 1)[-1]
        assert [e["text"] for e in client.get(f"/api/runs/{run_id}").json()["config"]["evidence"]] == \
            ["Logs kept 6 years"]
        for page in (f"/runs/{run_id}", f"/runs/{run_id}/export"):
            assert "Evidence provided" in client.get(page).text
