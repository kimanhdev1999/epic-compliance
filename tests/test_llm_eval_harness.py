"""Validates the eval harness's scoring logic itself (scripts/eval_llm_rules.py).

Does not call a real LLM or check accuracy against Claude — that's what
`python scripts/eval_llm_rules.py` (with ANTHROPIC_API_KEY set) is for, run by
a human as part of landing an LLM-rule change. This test only proves the
harness correctly scores a known set of verdicts, using a stubbed
evaluate_with_llm so it's fast, free, and deterministic in CI.
"""
from __future__ import annotations

import json

from scripts import eval_llm_rules
from epic_compliance.models import Finding


def test_fixtures_file_is_well_formed():
    cases = eval_llm_rules._load_cases()
    assert len(cases) >= 5
    for case in cases:
        assert case["rule_id"] in {"SEC-001", "SEC-002", "SEC-003"}
        assert case["expected_verdict"] in {"pass", "fail", "needs_human"}
        assert isinstance(case["evidence"], dict)


def test_fixtures_cover_all_llm_rules_in_catalog():
    rules = eval_llm_rules._rules_by_id()
    llm_rule_ids = {r.id for r in rules.values() if r.check_type == "llm"}
    cases = eval_llm_rules._load_cases()
    covered = {c["rule_id"] for c in cases}
    missing = llm_rule_ids - covered
    assert not missing, f"LLM rules with no eval fixture: {missing}"


def test_agreement_scoring_counts_exact_matches_and_needs_human_as_safe(monkeypatch):
    # Stub evaluate_with_llm to return a scripted verdict per case_id so this
    # test exercises run_eval's scoring logic, not the real LLM.
    scripted = {
        "sec001-tls13-pass": "pass",       # matches expected -> agree
        "sec001-tls12-pass": "needs_human",  # mismatch but needs_human -> agree (safe)
        "sec001-tls10-fail": "pass",       # mismatch AND confident -> miss
    }

    def fake_cases():
        return [
            {"case_id": cid, "rule_id": "SEC-001", "evidence": {}, "expected_verdict": "pass" if cid != "sec001-tls10-fail" else "fail", "note": ""}
            for cid in scripted
        ]

    def fake_evaluate_with_llm(rule, evidence_str, api_key, mock=False):
        case_id = [c for c in fake_cases() if json.dumps(c["evidence"]) == evidence_str]
        return Finding(
            rule_id=rule.id,
            verdict="pass",  # overwritten below via monkeypatch of the dict lookup
            evidence=evidence_str,
            check_type="llm",
            citation="stub",
        )

    monkeypatch.setattr(eval_llm_rules, "_load_cases", fake_cases)

    call_count = {"n": 0}
    ordered_ids = list(scripted.keys())

    def scripted_evaluate(rule, evidence_str, api_key, mock=False):
        cid = ordered_ids[call_count["n"]]
        call_count["n"] += 1
        return Finding(
            rule_id=rule.id,
            verdict=scripted[cid],
            evidence=evidence_str,
            check_type="llm",
            citation="stub",
        )

    monkeypatch.setattr(eval_llm_rules, "evaluate_with_llm", scripted_evaluate)

    results, agreement_rate = eval_llm_rules.run_eval(api_key="sk-fake", mock=False)

    assert len(results) == 3
    agree_by_id = {r["case_id"]: r["agree"] for r in results}
    assert agree_by_id["sec001-tls13-pass"] is True
    assert agree_by_id["sec001-tls12-pass"] is True
    assert agree_by_id["sec001-tls10-fail"] is False
    # 2 of 3 agree
    assert abs(agreement_rate - (2 / 3)) < 1e-9


def test_run_eval_handles_unknown_rule_id(monkeypatch):
    def fake_cases():
        return [{"case_id": "bogus", "rule_id": "SEC-999", "evidence": {}, "expected_verdict": "pass", "note": ""}]

    monkeypatch.setattr(eval_llm_rules, "_load_cases", fake_cases)
    results, agreement_rate = eval_llm_rules.run_eval(api_key="sk-fake", mock=False)
    assert results[0]["actual_verdict"] == "ERROR"
    assert results[0]["agree"] is False


def test_mock_mode_does_not_score(monkeypatch):
    results, agreement_rate = eval_llm_rules.run_eval(api_key="", mock=True)
    assert agreement_rate == 1.0
    assert all(r["agree"] for r in results)
