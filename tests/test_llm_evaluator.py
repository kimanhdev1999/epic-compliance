"""Tests for the LLM evaluator: mock mode, response parsing, and invariants.

No network calls here — the live Anthropic path is exercised against a stub
client (monkeypatched onto the `anthropic` module) so these stay fast and
free. Accuracy against real Claude responses is covered separately by the
eval harness (scripts/eval_llm_rules.py + tests/test_llm_eval_harness.py).
"""
from __future__ import annotations

import sys
import types

import pytest

from epic_compliance.rules_engine.catalog import Rule
from epic_compliance.rules_engine.llm_evaluator import (
    _parse_llm_response,
    evaluate_with_llm,
)


def _rule(**overrides) -> Rule:
    base = dict(
        id="SEC-001",
        category="security",
        source="HIPAA",
        severity="critical",
        evidence_needed="app_config: transport_tls_version",
        check_type="llm",
        description="All data in transit must be encrypted with TLS 1.2 or higher",
        remediation_hint="Enable TLS 1.2+.",
    )
    base.update(overrides)
    return Rule(**base)


class _StubContent:
    def __init__(self, text: str) -> None:
        self.text = text


class _StubMessage:
    def __init__(self, text: str) -> None:
        self.content = [_StubContent(text)]


class _StubMessages:
    def __init__(self, response_text: str) -> None:
        self._response_text = response_text
        self.last_call: dict | None = None

    def create(self, **kwargs):
        self.last_call = kwargs
        return _StubMessage(self._response_text)


class _StubAnthropicClient:
    def __init__(self, api_key: str, response_text: str = "") -> None:
        self.api_key = api_key
        self.messages = _StubMessages(response_text)


def _install_stub_anthropic(monkeypatch, response_text: str) -> _StubAnthropicClient:
    """Monkeypatch the `anthropic` module so evaluate_with_llm's lazy import
    picks up a stub client instead of hitting the network."""
    captured: dict[str, _StubAnthropicClient] = {}

    def _factory(api_key: str) -> _StubAnthropicClient:
        client = _StubAnthropicClient(api_key, response_text)
        captured["client"] = client
        return client

    stub_module = types.SimpleNamespace(Anthropic=_factory)
    monkeypatch.setitem(sys.modules, "anthropic", stub_module)
    return captured


# --------------------------------------------------------------------------- #
# Mock mode
# --------------------------------------------------------------------------- #
def test_mock_mode_returns_needs_human_without_api_key():
    finding = evaluate_with_llm(_rule(), "some evidence", api_key="", mock=True)
    assert finding.verdict == "needs_human"
    assert finding.check_type == "llm"
    assert finding.rule_id == "SEC-001"
    assert "MOCK" in finding.citation


def test_no_api_key_forces_mock_even_if_mock_flag_false():
    finding = evaluate_with_llm(_rule(), "some evidence", api_key="", mock=False)
    assert finding.verdict == "needs_human"
    assert "MOCK" in finding.citation


def test_mock_mode_never_calls_anthropic(monkeypatch):
    # If evaluate_with_llm tried to import/call anthropic in mock mode, this
    # stub would raise since Anthropic() here always blows up.
    def _boom(api_key: str):
        raise AssertionError("anthropic.Anthropic() should not be called in mock mode")

    stub_module = types.SimpleNamespace(Anthropic=_boom)
    monkeypatch.setitem(sys.modules, "anthropic", stub_module)
    finding = evaluate_with_llm(_rule(), "evidence", api_key="sk-test", mock=True)
    assert finding.verdict == "needs_human"


# --------------------------------------------------------------------------- #
# Response parsing
# --------------------------------------------------------------------------- #
def test_parse_direct_json():
    verdict = _parse_llm_response('{"verdict": "pass", "citation": "TLS 1.3 enforced"}')
    assert verdict.verdict == "pass"
    assert verdict.citation == "TLS 1.3 enforced"


def test_parse_json_embedded_in_prose():
    text = 'Sure, here is my assessment:\n{"verdict": "fail", "citation": "TLS 1.0 allowed"}\nHope that helps.'
    verdict = _parse_llm_response(text)
    assert verdict.verdict == "fail"
    assert verdict.citation == "TLS 1.0 allowed"


def test_parse_malformed_json_raises():
    with pytest.raises(ValueError):
        _parse_llm_response("not json at all")


def test_parse_invalid_verdict_value_raises():
    with pytest.raises(ValueError):
        _parse_llm_response('{"verdict": "maybe", "citation": "unclear"}')


# --------------------------------------------------------------------------- #
# Live path invariants (stubbed client, no network)
# --------------------------------------------------------------------------- #
def test_live_pass_with_citation(monkeypatch):
    _install_stub_anthropic(
        monkeypatch, '{"verdict": "pass", "citation": "transport_tls_version: 1.3"}'
    )
    finding = evaluate_with_llm(
        _rule(), '{"transport_tls_version": "1.3"}', api_key="sk-test", mock=False
    )
    assert finding.verdict == "pass"
    assert finding.citation == "transport_tls_version: 1.3"
    assert finding.check_type == "llm"


def test_live_missing_citation_forces_needs_human(monkeypatch):
    _install_stub_anthropic(monkeypatch, '{"verdict": "pass", "citation": ""}')
    finding = evaluate_with_llm(_rule(), "evidence", api_key="sk-test", mock=False)
    # Invariant: a verdict without a citation can never stand as pass/fail.
    assert finding.verdict == "needs_human"
    assert "no citation" in finding.citation.lower()


def test_live_malformed_response_raises(monkeypatch):
    _install_stub_anthropic(monkeypatch, "I refuse to answer in JSON.")
    with pytest.raises(ValueError):
        evaluate_with_llm(_rule(), "evidence", api_key="sk-test", mock=False)


def test_live_sends_requirement_and_evidence(monkeypatch):
    captured = _install_stub_anthropic(
        monkeypatch, '{"verdict": "needs_human", "citation": "insufficient evidence"}'
    )
    evaluate_with_llm(_rule(), "the-exact-evidence-string", api_key="sk-test", mock=False)
    call = captured["client"].messages.last_call
    assert call is not None
    sent = call["messages"][0]["content"]
    assert "the-exact-evidence-string" in sent
    assert _rule().description in sent
