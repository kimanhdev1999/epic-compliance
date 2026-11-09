"""Walk every page, empty and with results, at desktop and phone widths.

Each page must load without errors, fit a phone screen, and have a screenshot
in UI_SHOTS_DIR so a reviewer (human or agent) can look without clicking.
"""
from __future__ import annotations

import re

import httpx
import pytest

from .conftest import VIEWPORTS

EMPTY_PAGES = ["/", "/assess", "/tests", "/frameworks", "/integrations", "/runs", "/config"]

# {run} is the sample run id; {rule} a failing or needs-review test from it.
RESULT_PAGES = [
    "/", "/tests", "/frameworks", "/frameworks/smart", "/runs", "/integrations",
    "/assess", "/assess/plan", "/runs/{run}", "/runs/{run}/print", "/tests/{rule}",
]

# Open UX findings, per (path, viewport). Strict xfail: fixing one fails the
# suite until its entry is removed, so the fix can't silently regress.
KNOWN = {
    ("/runs/{run}/print", "phone"): "export page scrolls sideways 2px on phones",
}

# Links that leave the app or start a sign-in; checked by the honesty tests instead.
NOT_CRAWLED = re.compile(r"^/(launch|callback|evidence/)|/export$")


def _shot_name(prefix: str, path: str) -> str:
    return prefix + (re.sub(r"[^a-z0-9]+", "-", path.lower()).strip("-") or "home")


def _check(v, path: str, name: str) -> None:
    resp = v.page.goto(path)
    v.page.wait_for_load_state("networkidle")
    v.shot(name)
    assert resp.status < 400, f"{path} returned {resp.status}"
    assert not v.console_errors, f"{path}: browser errors {v.console_errors}"
    assert not v.bad_responses, f"{path}: failed requests {v.bad_responses}"
    if v.viewport == "phone":
        assert v.overflow_px() <= 1, f"{path} scrolls sideways by {v.overflow_px()}px at phone width"


@pytest.fixture(scope="module")
def a_rule(server, sample_run) -> str:
    run = httpx.get(f"{server}/api/runs/{sample_run}").json()
    open_items = sorted(f["rule_id"] for f in run["findings"] if f["verdict"] != "pass")
    return (open_items or [run["findings"][0]["rule_id"]])[0]


@pytest.mark.parametrize("viewport", VIEWPORTS)
@pytest.mark.parametrize("path", EMPTY_PAGES)
def test_empty_page(visit, empty_server, path, viewport):
    _check(visit(empty_server, viewport), path, _shot_name("empty-", path))


@pytest.mark.parametrize("viewport", VIEWPORTS)
@pytest.mark.parametrize("path", RESULT_PAGES)
def test_result_page(request, visit, server, sample_run, a_rule, path, viewport):
    if reason := KNOWN.get((path, viewport)):
        request.applymarker(pytest.mark.xfail(strict=True, reason=f"open UX finding: {reason}"))
    path = path.format(run=sample_run, rule=a_rule)
    _check(visit(server, viewport), path, _shot_name("result-", path.replace(sample_run, "run")))


def test_no_dead_links(visit, server, sample_run, a_rule):
    """Every in-app link on the main pages leads somewhere that loads."""
    v = visit(server)
    hrefs: set[str] = set()
    for path in RESULT_PAGES:
        v.page.goto(path.format(run=sample_run, rule=a_rule))
        hrefs |= set(v.page.eval_on_selector_all("a[href^='/']", "els => els.map(e => e.getAttribute('href'))"))
    dead = []
    for href in sorted(h.split("#")[0] for h in hrefs):
        if href and not NOT_CRAWLED.match(href):
            status = httpx.get(server + href, follow_redirects=True).status_code
            if status >= 400:
                dead.append(f"{status} {href}")
    assert not dead, f"dead links: {dead}"


@pytest.mark.parametrize("viewport", VIEWPORTS)
def test_edit_answers_in_place_on_the_summary(visit, server, sample_run, viewport):
    """The summary card's "Edit answers" turns the same card into a form; saving
    sets the answer, keeps the rest, highlights what changed, and asks nothing again."""
    v = visit(server, viewport)
    page = v.page
    page.goto("/assess")
    page.wait_for_load_state("networkidle")
    card = page.locator("[data-understood]")
    assert card.locator("button[type=submit]", has_text="Generate test plan").count() == 1
    dialogs: list[str] = []
    page.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))

    card.locator("[data-edit-answers]").click()
    assert "editing" in (card.get_attribute("class") or "")
    assert card.locator("textarea[name=description]").evaluate("el => el === document.activeElement")
    assert not card.locator(".understood-view").is_visible()
    page.screenshot(path=str(v.shot("assess-answers-editing").with_name(f"assess-answers-form-{viewport}.png")))
    if viewport == "phone":
        assert v.overflow_px() <= 1, f"edit form scrolls sideways by {v.overflow_px()}px"

    # Esc cancels without saving.
    page.keyboard.press("Escape")
    assert card.locator(".understood-view").is_visible()

    card.locator("[data-edit-answers]").click()
    data = card.locator("textarea[name=data_use]")
    assert data.input_value() and card.locator(".read-as").is_visible()
    data.scroll_into_view_if_needed()
    page.screenshot(path=str(v.shot("assess-answers-data").with_name(f"assess-answers-data-{viewport}.png")))
    note = " It also sends PHI to a third-party AI service."
    data.fill(data.input_value().replace(note, "") if note.strip() in data.input_value() else data.input_value() + note)
    with page.expect_response("**/assess/answers"):
        card.locator("button[type=submit]", has_text="Save answers").click()
    page.wait_for_selector("[data-understood] .saved-note")
    card = page.locator("[data-understood]")
    assert card.locator("dd.changed").count() == 1 and card.locator(".saved-note").is_visible()
    assert page.locator(".quick-replies").count() == 0  # nothing is asked again
    page.screenshot(path=str(v.shot("assess-answers-saved").with_name(f"assess-answers-saved-{viewport}.png")))
    assert page.url.endswith("/assess") and not dialogs
    assert not v.console_errors and not v.bad_responses
