"""Shared pytest fixtures — notably the interactive SMART launch driver."""
from __future__ import annotations

import threading
import urllib.parse
import webbrowser

import httpx
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from epic_compliance.smart.pkce import (
    PKCEChallenge,
    TokenResponse,
    build_authorization_url,
    exchange_code_for_token,
)


def pytest_addoption(parser):
    parser.addoption(
        "--launch-interactive",
        action="store_true",
        default=False,
        help="Allow tests marked `launch` to open a browser and wait for a human login.",
    )


def pytest_collection_modifyitems(config, items):
    """`launch` tests need a human. Skip unless explicitly opted in."""
    if config.getoption("--launch-interactive"):
        return
    skip = pytest.mark.skip(reason="needs a human at a browser; pass --launch-interactive")
    for item in items:
        if "launch" in item.keywords:
            item.add_marker(skip)


class _CallbackHandler(BaseHTTPRequestHandler):
    """Captures ?code=&state= from Epic's redirect."""

    def do_GET(self):  # noqa: N802
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(self.path).query))
        self.server.captured = q  # type: ignore[attr-defined]
        ok = "code" in q
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        msg = "Launch captured — back to the terminal." if ok else f"No code. Epic said: {q}"
        self.wfile.write(f"<h2>{msg}</h2>".encode())

    def log_message(self, *args):  # silence per-request logging
        pass


#: Epic's `error=N` codes on the authorize redirect, mapped to what to go fix.
#: Epic does not publish these; meanings inferred from observed behaviour (Day 13).
_EPIC_AUTHORIZE_ERRORS = {
    "4": (
        "Epic rejected the authorization request outright ('Invalid OAuth 2.0 request').\n"
        "   Observed for every scope set, including a bare `openid`, and for any\n"
        "   redirect_uri — so this is the app registration, not this request.\n"
        "   Check at https://fhir.epic.com:\n"
        "     - app audience is 'Patients' (standalone launch needs a patient-facing app)\n"
        "     - the R4 APIs you request are individually enabled on the app\n"
        "     - the app is marked ready for the sandbox / non-prod environment"
    ),
}


def _preflight_authorize(url: str) -> str | None:
    """Ask Epic whether it will honour this authorize request, before a human waits.

    Epic answers an invalid request with a 302 to a Hyperspace error page carrying
    `error=N` instead of rendering a login form. Detecting that here turns a silent
    180s timeout into an immediate, actionable failure. Returns a message, or None
    if the request looks acceptable.
    """
    try:
        resp = httpx.get(url, follow_redirects=True, timeout=15)
    except httpx.HTTPError as exc:  # network trouble is not a conformance verdict
        return f"could not reach the authorization endpoint: {exc}"

    # Form 1: a redirect chain carrying ?error=N to a Hyperspace error page.
    for hop in [*resp.history, resp]:
        location = hop.headers.get("location", "")
        err = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(location).query)).get("error")
        if err:
            detail = _EPIC_AUTHORIZE_ERRORS.get(
                err, f"Epic returned error={err} on the authorize redirect."
            )
            return f"Epic refused the launch before login (error={err}).\n   {detail}"

    # Form 2: HTTP 200 rendering an "OAuth2 Error" page rather than a login form.
    # Epic serves this for an unrecognised client_id — a made-up UUID is answered
    # identically, which is how you tell it apart from a misconfigured app.
    if "<title>OAuth2 Error</title>" in resp.text:
        return (
            "Epic served its generic 'OAuth2 Error' page instead of a login form.\n"
            "   A made-up client_id gets this same response, so Epic does not\n"
            "   recognise this one. Either the app has not propagated to the sandbox\n"
            "   yet (newly-registered apps are not live immediately — wait and retry),\n"
            "   or the client_id is wrong. Confirm you copied the NON-PRODUCTION\n"
            "   client ID from https://fhir.epic.com."
        )

    return None


@dataclass
class LaunchDriver:
    """Drives one interactive SMART launch: browser -> redirect -> token."""

    client_id: str = ""
    redirect_uri: str = ""
    token_endpoint: str = ""
    timeout_s: int = 180
    returned_state: str = ""
    _captured: dict = field(default_factory=dict)

    def authorize(
        self,
        *,
        authorization_endpoint: str,
        client_id: str,
        redirect_uri: str,
        scopes: list[str],
        pkce: PKCEChallenge,
        state: str,
        aud: str,
    ) -> str:
        self.client_id = client_id
        self.redirect_uri = redirect_uri

        url = build_authorization_url(
            authorization_endpoint=authorization_endpoint,
            client_id=client_id,
            redirect_uri=redirect_uri,
            scopes=scopes,
            pkce=pkce,
            state=state,
        )
        # `aud` is mandatory for Epic but build_authorization_url() does not yet
        # emit it (Day 13 fix). Append here so the interactive flow works now;
        # remove this once pkce.py sets aud itself.
        if "aud=" not in url:
            url += "&" + urllib.parse.urlencode({"aud": aud})

        # Fail fast: no point opening a browser and waiting out the timeout if Epic
        # has already refused the request.
        refusal = _preflight_authorize(url)
        if refusal:
            pytest.fail(refusal)

        parsed = urllib.parse.urlparse(redirect_uri)
        server = HTTPServer((parsed.hostname or "localhost", parsed.port or 80), _CallbackHandler)
        server.captured = {}  # type: ignore[attr-defined]
        server.timeout = self.timeout_s

        t = threading.Thread(target=server.handle_request, daemon=True)
        t.start()

        print(f"\n── Opening browser for SMART launch.\n   If it does not open:\n\n{url}\n")
        webbrowser.open(url)
        t.join(self.timeout_s)
        server.server_close()

        captured = getattr(server, "captured", {})
        if not captured:
            pytest.fail(f"no redirect received within {self.timeout_s}s")
        if "code" not in captured:
            pytest.fail(f"authorize failed: {captured}")

        self._captured = captured
        self.returned_state = captured.get("state", "")
        return captured["code"]

    def exchange(self, *, code: str, pkce: PKCEChallenge) -> TokenResponse:
        return exchange_code_for_token(
            token_endpoint=self.token_endpoint,
            code=code,
            client_id=self.client_id,
            redirect_uri=self.redirect_uri,
            code_verifier=pkce.code_verifier,
        )


@pytest.fixture
def launch_driver(live_smart_config) -> LaunchDriver:
    return LaunchDriver(token_endpoint=live_smart_config.token_endpoint)
