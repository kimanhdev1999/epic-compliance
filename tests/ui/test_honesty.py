"""The UI must never overstate readiness. Each test is one rule an Epic
reviewer would hold us to; they run against a finished sample-data run.

Rules the app breaks today are marked `known(...)` (strict xfail): fix the
app and the test XPASSes, which fails the suite until the marker is removed,
so a fixed rule can't silently regress.
"""
from __future__ import annotations

import re

import httpx
import pytest

# Subjects that pass for any app pointed at Epic, so they say nothing about yours.
NOT_THE_APP = ("subject-epic_platform", "subject-tool_launch")


def known(reason: str):
    return pytest.mark.xfail(strict=True, reason=f"open UX finding: {reason}")


@pytest.fixture
def report(visit, server, sample_run):
    v = visit(server)
    v.page.goto(f"/runs/{sample_run}")
    return v


@pytest.fixture
def printed(visit, server, sample_run):
    v = visit(server)
    v.page.goto(f"/runs/{sample_run}/print")
    return v


@pytest.fixture(scope="module")
def run_json(server, sample_run) -> dict:
    return httpx.get(f"{server}/api/runs/{sample_run}").json()


# --- Scores and summaries ----------------------------------------------------

@known('readiness % includes Epic-server and tool-login checks')
def test_readiness_counts_only_your_app(report):
    """Checks that pass for any app (Epic's server, this tool's own login)
    mustn't lift the readiness % a reviewer reads as "your app is ready"."""
    page = report.page
    findings = page.locator("details.finding")
    mine = passed = 0
    for i in range(findings.count()):
        f = findings.nth(i)
        if any(f.locator(f".badge.{s}").count() for s in NOT_THE_APP):
            continue
        mine += 1
        passed += f.get_attribute("data-verdict") == "pass"
    expected = round(100 * passed / mine) if mine else None
    shown = page.locator(".ring span").first.inner_text().replace("%", "").strip()
    assert shown == str(expected), f"readiness shows {shown}%, your app's own checks give {expected}%"


@known('green "No failures" while items still need review')
def test_no_failures_only_when_nothing_is_open(report, run_json):
    """A green "No failures" next to unresolved critical items reads as "done"."""
    open_items = run_json["summary"]["counts"].get("needs_human", 0)
    shows = report.page.locator(".sev-bar .seg.none").count() > 0
    assert not (shows and open_items), f'"No failures" shown with {open_items} items still needing review'


# --- Sample data -------------------------------------------------------------

@pytest.mark.parametrize("path", ["/runs/{run}", "/runs/{run}/print", "/", "/tests"])
def test_sample_data_is_unmissable(visit, server, sample_run, path):
    """Results from sample responses must carry a banner, not a muted subtitle."""
    v = visit(server)
    v.page.goto(path.format(run=sample_run))
    banner = v.page.locator("[role=alert], [role=status], .banner, [data-demo-banner]").filter(
        has_text=re.compile(r"demo|sample", re.I))
    assert banner.count(), f"{path}: sample-data results with no visible banner"


@known('a sample run ticks Connect / Run / Fix on Home')
def test_checklist_ignores_sample_runs(visit, server, sample_run):
    """A sample run proves nothing about Epic, so it can't tick setup steps."""
    v = visit(server)
    v.page.goto("/")
    ticked = v.page.locator(".checklist li.done").all_inner_texts()
    wrong = [re.sub(r"^\W+", "", t).split("\n")[0] for t in ticked if re.search(r"Connect the Epic|Run your first|Fix failing", t)]
    assert not wrong, f"ticked by a sample-data run: {wrong}"


# --- Exported report ---------------------------------------------------------

@known('export is titled "... Compliance Report"')
def test_export_doesnt_claim_certification(printed):
    title = printed.page.locator("h1").first.inner_text()
    assert not re.search(r"compliance report|certif", title, re.I) or re.search(r"not .*certif", title, re.I), title


@known('export drops the what-this-tests labels')
def test_export_says_what_each_check_tests(printed, run_json):
    """Without "Tests Epic" labels, Epic's passes read as the app's passes."""
    text = printed.text()
    assert "Tests Epic" in text, "export drops the what-this-tests labels the UI shows"


# --- Wording -----------------------------------------------------------------

@known('"How to fix" shown on passed checks')
def test_how_to_fix_only_on_open_items(report, printed):
    on_report = report.page.locator("details.finding[data-verdict=pass] .remediation").count()
    on_print = printed.page.locator(".finding.pass .fix").count()
    assert (on_report, on_print) == (0, 0), (
        f'"How to fix" on passed checks: {on_report} in the report, {on_print} in the export')


@pytest.mark.parametrize("path", [
    "/", "/tests", "/runs/{run}",
    pytest.param("/runs/{run}/print", marks=known('export says "Needs Human"')),
])
def test_one_name_for_each_status(visit, server, sample_run, path):
    """The UI says "Needs review"; other names for the same status confuse."""
    v = visit(server)
    v.page.goto(path.format(run=sample_run))
    assert not re.search(r"needs human", v.text(), re.I), f'{path} says "Needs human"'


LEAKS = {
    "python dict": re.compile(r"\{'[a-z_]+':"),
    "OS error": re.compile(r"\[Errno \d+\]"),
    "unfilled placeholder": re.compile(r"\b[A-Z]{2,}(?:_[A-Z]{2,}){1,}\b(?!=)"),
}


@pytest.mark.parametrize("leak", LEAKS)
def test_no_internals_in_findings(report, leak):
    """Requirement titles and the review list are read by people, not developers."""
    page = report.page
    text = "\n".join(page.locator(".finding .req, .priority .body").all_inner_texts())
    hits = sorted(set(m.group(0) for m in LEAKS[leak].finditer(text)))
    assert not hits, f"{leak} shown to the reader: {hits[:5]}"


# --- Flows -------------------------------------------------------------------

@known('offline chat reads "look up a patient" as patient users')
def test_chat_hears_clinicians(visit, server, sample_run):
    """The sample app is for nurses who look up a patient: users are clinicians."""
    v = visit(server)
    v.page.goto("/assess")
    row = v.page.locator(".understood dt", has_text="Signs in").locator("xpath=following-sibling::dd[1]")
    assert "Clinician" in row.inner_text(), f'"Signs in" recorded as {row.inner_text()!r}'


def test_signin_never_dead_ends(visit, server, sample_run):
    """Every "Sign in" link lands on a page of the app, not a bare error."""
    v = visit(server)
    v.page.goto("/integrations")
    hrefs = v.page.eval_on_selector_all(
        "a", "els => els.filter(e => /sign in/i.test(e.textContent)).map(e => e.getAttribute('href'))")
    for href in hrefs:
        resp = httpx.get(server + href)
        assert resp.status_code < 400 or "<nav" in resp.text, f"{href} -> {resp.status_code}: {resp.text[:80]}"


@pytest.mark.parametrize("path", ["/", "/tests", "/frameworks", "/runs/{run}", "/assess/plan"])
def test_only_applicable_tests_are_shown(visit, server, sample_run, path):
    """The user sees the tests their app is judged on, never the ones that don't apply."""
    v = visit(server)
    v.page.goto(path.format(run=sample_run))
    text = v.text()
    assert not re.search(r"not applicable|\bN/A\b|skipped (for this app|as not applicable)", text, re.I), (
        f"{path} lists tests that don't apply")
