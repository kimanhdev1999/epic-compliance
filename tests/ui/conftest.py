"""UI harness: a throwaway server in sample-data mode, driven by a real browser.

The server runs from an empty temp dir with a minimal environment, so it never
reads the project's .env, the real data/runs.db, Epic or the Claude API.
Screenshots of every page land in UI_SHOTS_DIR (default build/ui-shots/).

    pip install -e '.[dev,ui]' && python -m playwright install chromium
    pytest -m ui
"""
from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest

sync_api = pytest.importorskip("playwright.sync_api", reason="pip install -e '.[ui]'")

ROOT = Path(__file__).resolve().parents[2]
SHOTS = Path(os.environ.get("UI_SHOTS_DIR", ROOT / "build" / "ui-shots"))

VIEWPORTS = {
    "desktop": {"width": 1440, "height": 900},
    "phone": {"width": 390, "height": 844},
}

# Described like a real first-time user would; the chat has to get from this to a plan.
APP_DESCRIPTION = (
    "A standalone clinician web app for Advocate nurses. They sign in, look up a "
    "patient, read their medications and allergies, and save wound photos to the chart."
)


def pytest_collection_modifyitems(items):
    for item in items:
        if "tests/ui/" in str(item.fspath).replace(os.sep, "/"):
            item.add_marker(pytest.mark.ui)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def server(tmp_path_factory) -> str:
    """The app after one sample-data run (see `sample_run`)."""
    yield from _serve(tmp_path_factory.mktemp("ui-server"))


@pytest.fixture(scope="session")
def empty_server(tmp_path_factory) -> str:
    """A fresh install: nothing described, nothing run."""
    yield from _serve(tmp_path_factory.mktemp("ui-empty"))


def _serve(work: Path):
    """Start the web app on a free port against a DB in `work`; yield its base URL."""
    port = _free_port()
    # Only what the interpreter needs: nothing from the project's .env leaks in.
    env = {k: os.environ[k] for k in ("PATH", "HOME", "LANG", "TMPDIR") if k in os.environ}
    env.update(
        PYTHONPATH=str(ROOT),
        EPIC_COMPLIANCE_DB=str(work / "runs.db"),
        RUN_MODE="mock",
        ANTHROPIC_API_KEY="",
    )
    log = open(work / "server.log", "w")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "epic_compliance.api:app", "--port", str(port)],
        cwd=work, env=env, stdout=log, stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 30
    while True:
        try:
            if httpx.get(f"{base}/healthz", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            pass
        if proc.poll() is not None or time.monotonic() > deadline:
            proc.kill()
            pytest.fail(f"UI server didn't start; see {work / 'server.log'}")
        time.sleep(0.2)
    yield base
    proc.terminate()
    proc.wait(timeout=10)
    log.close()


@pytest.fixture(scope="session")
def browser():
    with sync_api.sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


@dataclass
class Visit:
    """A page plus everything that went wrong while it loaded."""

    page: "sync_api.Page"
    viewport: str
    console_errors: list[str] = field(default_factory=list)
    bad_responses: list[str] = field(default_factory=list)

    def text(self) -> str:
        return self.page.inner_text("body")

    def shot(self, name: str) -> Path:
        SHOTS.mkdir(parents=True, exist_ok=True)
        path = SHOTS / f"{name}-{self.viewport}.png"
        self.page.screenshot(path=str(path), full_page=True)
        return path

    def overflow_px(self) -> int:
        """How far the page scrolls sideways (0 = fits the viewport)."""
        return self.page.evaluate(
            "document.documentElement.scrollWidth - document.documentElement.clientWidth"
        )


def _open(browser, base: str, viewport: str) -> Visit:
    ctx = browser.new_context(viewport=VIEWPORTS[viewport], base_url=base)
    page = ctx.new_page()
    v = Visit(page, viewport)
    page.on("console", lambda m: m.type == "error" and v.console_errors.append(m.text))
    page.on("pageerror", lambda e: v.console_errors.append(str(e)))
    page.on("response", lambda r: r.status >= 400 and v.bad_responses.append(f"{r.status} {r.url}"))
    return v


@pytest.fixture
def visit(browser):
    """visit(base_url, viewport) -> Visit; contexts close after the test."""
    opened: list[Visit] = []

    def make(base: str, viewport: str = "desktop") -> Visit:
        opened.append(_open(browser, base, viewport))
        return opened[-1]

    yield make
    for v in opened:
        v.page.context.close()


def describe_app(v: Visit, description: str = APP_DESCRIPTION, max_turns: int = 12) -> None:
    """Chat until the tool offers a test plan, taking the first quick reply
    (or "Not sure") for each follow-up, the way a hurried user would."""
    page = v.page
    page.goto("/assess")
    if page.locator("form[action='/assess/new'] button").count():
        page.once("dialog", lambda d: d.accept())
        page.click("form[action='/assess/new'] button")
    page.fill("#chat-form textarea[name=message]", description)
    page.click("#chat-form button[type=submit]")
    for _ in range(max_turns):
        page.wait_for_load_state("networkidle")
        if page.locator("text=Generate test plan").count():
            return
        reply = page.locator(".quick-replies button").first
        if reply.count():
            reply.click()
        else:
            page.fill("#chat-form textarea[name=message]", "Not sure")
            page.click("#chat-form button[type=submit]")
    pytest.fail("chat never offered a test plan")


def run_sample(v: Visit) -> str:
    """From the plan page, run the plan on sample data; return the run id.
    Needs no setup: the plan page offers it next to the Epic run."""
    page = v.page
    page.click("text=Generate test plan")
    page.wait_for_url("**/assess/plan")
    page.click("#run-sample")
    page.wait_for_url(re.compile(r"/runs/[^/]+$"), timeout=60_000)
    return page.url.rsplit("/", 1)[1]


@pytest.fixture(scope="session")
def sample_run(browser, server) -> str:
    """One finished sample-data run, shared by every test that needs results."""
    v = _open(browser, server, "desktop")
    describe_app(v)
    run_id = run_sample(v)
    v.page.context.close()
    return run_id
