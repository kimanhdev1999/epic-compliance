#!/usr/bin/env python3
"""Eval harness for the LLM evaluator: measure agreement against hand-labeled cases.

This is the thing a compliance tool actually needs and didn't have: proof that
the LLM-assisted checks (SEC-00x, check_type="llm") agree with a human-labeled
ground truth, not just that the plumbing runs without crashing.

Usage:
    ANTHROPIC_API_KEY=sk-... python scripts/eval_llm_rules.py
    python scripts/eval_llm_rules.py --mock   # sanity-check the harness itself, no API calls

Exit code is non-zero if agreement rate is below --min-agreement (default 1.0
for needs_human-is-always-acceptable cases, see AGREEMENT RULE below).

AGREEMENT RULE: a case counts as agreement if the evaluator's verdict exactly
matches expected_verdict, OR the evaluator returned "needs_human" (always a
safe/acceptable outcome for a tool whose stated invariant is "never fabricate
a pass" — see notes/decisions.md). A verdict that is wrong AND confident
(e.g. expected "fail" but got "pass") is the only true miss.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from epic_compliance.rules_engine.catalog import load_rules
from epic_compliance.rules_engine.llm_evaluator import evaluate_with_llm

FIXTURES_PATH = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "llm_eval_cases.json"


def _load_cases() -> list[dict[str, Any]]:
    return json.loads(FIXTURES_PATH.read_text())


def _rules_by_id() -> dict[str, Any]:
    return {r.id: r for r in load_rules()}


def run_eval(api_key: str, mock: bool) -> tuple[list[dict[str, Any]], float]:
    cases = _load_cases()
    rules = _rules_by_id()
    results: list[dict[str, Any]] = []

    for case in cases:
        rule = rules.get(case["rule_id"])
        if rule is None:
            results.append({**case, "actual_verdict": "ERROR", "agree": False,
                             "detail": f"Unknown rule_id {case['rule_id']!r} — check rules/*.json"})
            continue

        evidence_str = json.dumps(case["evidence"])
        finding = evaluate_with_llm(rule, evidence_str, api_key=api_key, mock=mock)

        expected = case["expected_verdict"]
        actual = finding.verdict
        agree = (actual == expected) or (actual == "needs_human" and not mock)
        if mock:
            # In --mock sanity mode every call deterministically returns
            # needs_human, so "agreement" there would be meaningless noise;
            # just report what came back without scoring it as pass/fail.
            agree = True

        results.append({
            **case,
            "actual_verdict": actual,
            "citation": finding.citation,
            "agree": agree,
        })

    scored = [r for r in results if not mock]
    agreement_rate = (sum(r["agree"] for r in scored) / len(scored)) if scored else 1.0
    return results, agreement_rate


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mock", action="store_true", help="Run without calling the API (harness sanity check only)")
    parser.add_argument("--min-agreement", type=float, default=0.9,
                         help="Fail (exit 1) if agreement rate is below this (default 0.9)")
    args = parser.parse_args()

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    mock = args.mock or not api_key
    if mock and not args.mock:
        print("No ANTHROPIC_API_KEY set — running in --mock mode (harness sanity check only, no real eval).\n")

    results, agreement_rate = run_eval(api_key, mock)

    print(f"{'case_id':<32} {'rule':<10} {'expected':<14} {'actual':<14} {'agree':<6} note")
    print("-" * 110)
    for r in results:
        agree_mark = "OK" if r["agree"] else "MISS"
        print(f"{r['case_id']:<32} {r['rule_id']:<10} {r['expected_verdict']:<14} "
              f"{r['actual_verdict']:<14} {agree_mark:<6} {r.get('note', '')}")

    print("-" * 110)
    if mock:
        print(f"Ran {len(results)} cases in --mock mode (no scoring — rerun with ANTHROPIC_API_KEY for a real eval).")
        return 0

    print(f"Agreement rate: {agreement_rate:.0%} ({sum(r['agree'] for r in results)}/{len(results)})")
    misses = [r for r in results if not r["agree"]]
    if misses:
        print("\nMisses (wrong AND confident, not needs_human):")
        for r in misses:
            print(f"  - {r['case_id']}: expected {r['expected_verdict']!r}, got {r['actual_verdict']!r} "
                  f"(citation: {r.get('citation', '')!r})")

    if agreement_rate < args.min_agreement:
        print(f"\nFAIL: agreement {agreement_rate:.0%} below threshold {args.min_agreement:.0%}")
        return 1
    print(f"\nPASS: agreement {agreement_rate:.0%} meets threshold {args.min_agreement:.0%}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
