"""FastAPI web application — server-rendered UI (HTMX) + JSON API.

Pages
  GET  /                      dashboard: run form + recent runs
  POST /runs                  trigger a pipeline run (HTMX → result partial)
  GET  /runs                  run history
  GET  /runs/{id}             saved report detail
  DELETE /runs/{id}           delete a run (HTMX → refreshed table)
  GET  /runs/{id}/export      download standalone HTML report
  GET  /runs/{id}/print       printable report (browser → PDF)
  GET  /config                config defaults form
  POST /config                save config defaults

JSON API (back-compat + additions)
  GET  /healthz               health check
  POST /api/run               run pipeline, return summary JSON
  GET  /api/report            last report
  GET  /api/runs              list runs
  GET  /api/runs/{id}         single run report JSON
"""
from __future__ import annotations

import secrets
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import pipeline, store
from .config import OVERRIDABLE_FIELDS, config_with_overrides, get_config
from .models import Report
from .pipeline import run_full_pipeline
from .smart.discovery import fetch_smart_configuration
from .smart.pkce import build_authorization_url, exchange_code_for_token, generate_pkce
from .viewmodel import build_report_view

BASE_DIR = Path(__file__).parent

app = FastAPI(title="Epic Compliance Tool", version="0.2.0")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

_last_report: Report | None = None

# Pending live launches, keyed by the random `state` value, holding the PKCE
# verifier and endpoint info needed to complete the exchange at /callback.
# In-memory / single-tenant — matches config.py's single-tenant scope; not a
# session store for multiple concurrent users.
_pending_launches: dict[str, dict[str, Any]] = {}

#: Scopes requested for a live write-back launch: read scopes so the pipeline
#: can still fetch US Core resources, plus the write scopes WRITE-004 checks.
LAUNCH_SCOPES = [
    "openid",
    "fhirUser",
    "launch/patient",
    "offline_access",
    "patient/Patient.read",
    "patient/Observation.read",
    "patient/Condition.read",
    "patient/DiagnosticReport.write",
    "patient/Observation.write",
    "patient/Media.write",
]


def render(request: Request, name: str, **ctx: Any) -> HTMLResponse:
    """Render a template using the current Starlette signature (request first)."""
    return templates.TemplateResponse(request, name, {"request": request, **ctx})


@app.on_event("startup")
def _startup() -> None:
    store.init_db()


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _execute_run(overrides: dict[str, str]) -> tuple[Report, dict[str, Any]]:
    """Run the pipeline with per-run overrides, persist it, return (report, cfg)."""
    global _last_report
    config = config_with_overrides(overrides)
    report = run_full_pipeline(config)
    snapshot = config.model_dump()
    store.save_run(report, snapshot)
    # Remember non-secret choices to prefill the next form.
    store.save_config_defaults(snapshot)
    _last_report = report
    return report, snapshot


def _load_run_or_404(run_id: str) -> dict[str, Any]:
    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"No run {run_id}")
    return run


# --------------------------------------------------------------------------- #
# Pages
# --------------------------------------------------------------------------- #
@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request) -> Any:
    defaults = store.get_config_defaults()
    if not defaults:
        # Seed from environment config so the form isn't blank on first run.
        cfg = get_config().model_dump()
        defaults = {k: cfg.get(k, "") for k in OVERRIDABLE_FIELDS}
        if cfg.get("anthropic_api_key"):
            defaults["anthropic_api_key"] = "***set***"
    return render(request, "dashboard.html", defaults=defaults, runs=store.list_runs(limit=8))


@app.post("/runs", response_class=HTMLResponse)
def create_run(
    request: Request,
    run_mode: str = Form("mock"),
    fhir_base_url: str = Form(""),
    oauth_client_id: str = Form(""),
    anthropic_api_key: str = Form(""),
) -> Any:
    overrides = {
        "run_mode": run_mode,
        "fhir_base_url": fhir_base_url,
        "oauth_client_id": oauth_client_id,
        "anthropic_api_key": anthropic_api_key,
    }
    report, _ = _execute_run(overrides)
    view = build_report_view(report)
    return render(request, "_run_result.html", view=view)


@app.get("/runs", response_class=HTMLResponse)
def history(request: Request) -> Any:
    return render(request, "history.html", runs=store.list_runs())


@app.get("/runs/{run_id}", response_class=HTMLResponse)
def report_detail(request: Request, run_id: str) -> Any:
    run = _load_run_or_404(run_id)
    view = build_report_view(run["report"])
    return render(request, "report.html", view=view, run=run)


@app.delete("/runs/{run_id}", response_class=HTMLResponse)
def delete_run(request: Request, run_id: str) -> Any:
    store.delete_run(run_id)
    return render(request, "_history_table.html", runs=store.list_runs())


@app.get("/runs/{run_id}/export", response_class=HTMLResponse)
def export_run(request: Request, run_id: str) -> Any:
    run = _load_run_or_404(run_id)
    view = build_report_view(run["report"])
    html = templates.get_template("export.html").render(
        request=request, view=view, run=run
    )
    filename = f"compliance-report-{run_id[:8]}.html"
    return Response(
        content=html,
        media_type="text/html",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/runs/{run_id}/print", response_class=HTMLResponse)
def print_run(request: Request, run_id: str) -> Any:
    run = _load_run_or_404(run_id)
    view = build_report_view(run["report"])
    return render(request, "export.html", view=view, run=run)


@app.get("/config", response_class=HTMLResponse)
def config_page(request: Request, saved: bool = False) -> Any:
    cfg = get_config().model_dump()
    defaults = store.get_config_defaults()
    cfg.update({k: v for k, v in defaults.items() if v})
    if get_config().anthropic_api_key:
        cfg["anthropic_api_key"] = "***set***"
    return render(request, "config.html", cfg=cfg, saved=saved)


@app.post("/config")
def save_config(
    run_mode: str = Form("mock"),
    fhir_base_url: str = Form(""),
    smart_launch_url: str = Form(""),
    smart_token_url: str = Form(""),
    oauth_client_id: str = Form(""),
    oauth_redirect_uri: str = Form(""),
    onc_test_kit_url: str = Form(""),
) -> RedirectResponse:
    store.save_config_defaults(
        {
            "run_mode": run_mode,
            "fhir_base_url": fhir_base_url,
            "smart_launch_url": smart_launch_url,
            "smart_token_url": smart_token_url,
            "oauth_client_id": oauth_client_id,
            "oauth_redirect_uri": oauth_redirect_uri,
            "onc_test_kit_url": onc_test_kit_url,
        }
    )
    return RedirectResponse(url="/config?saved=true", status_code=303)


# --------------------------------------------------------------------------- #
# Live SMART launch — /launch starts it, /callback completes it.
#
# This app is a "direction B" write-back client: it never hosts a FHIR server
# or authorization server of its own. These two routes are this web app's half
# of a normal SMART launch — the same role any app plays when Epic redirects
# a user back to it.
# --------------------------------------------------------------------------- #
@app.get("/launch")
def start_launch(request: Request) -> RedirectResponse:
    """Begin a live SMART launch: discover Epic's endpoints, then redirect the
    browser to Epic's authorization endpoint. Completes at GET /callback."""
    config = get_config()
    smart_config = fetch_smart_configuration(config.fhir_base_url, mock=False)
    if not smart_config.authorization_endpoint:
        raise HTTPException(
            status_code=502,
            detail="Could not discover authorization_endpoint from .well-known/smart-configuration.",
        )

    pkce = generate_pkce()
    state = secrets.token_urlsafe(24)
    _pending_launches[state] = {
        "pkce": pkce,
        "redirect_uri": config.oauth_redirect_uri,
        "client_id": config.oauth_client_id,
        "client_secret": config.oauth_client_secret,
        "token_endpoint": smart_config.token_endpoint,
    }

    url = build_authorization_url(
        authorization_endpoint=smart_config.authorization_endpoint,
        client_id=config.oauth_client_id,
        redirect_uri=config.oauth_redirect_uri,
        scopes=LAUNCH_SCOPES,
        pkce=pkce,
        state=state,
        aud=config.fhir_base_url,
    )
    return RedirectResponse(url=url, status_code=302)


@app.get("/callback", response_class=HTMLResponse)
def smart_callback(
    request: Request,
    code: str = "",
    state: str = "",
    error: str = "",
    error_description: str = "",
) -> Any:
    """Complete the authorization-code + PKCE exchange after Epic redirects here.

    On success, populates pipeline's in-process live-token holder so the next
    /api/run or "Run compliance check" (with run_mode=live) uses a real token
    instead of marking token- and write-back-dependent rules needs_human.
    """
    if error:
        return render(
            request, "_callback_result.html", ok=False,
            message=f"Epic returned an error: {error} — {error_description}",
        )

    # Looking the state up by exact dict key already proves the round trip —
    # an attacker without the state value this app generated cannot land here.
    pending = _pending_launches.pop(state, None)
    if pending is None:
        return render(
            request, "_callback_result.html", ok=False,
            message="Unknown or expired launch state. Start a new launch at /launch.",
        )
    if not code:
        return render(
            request, "_callback_result.html", ok=False,
            message="Epic's redirect had no authorization code.",
        )

    try:
        token = exchange_code_for_token(
            token_endpoint=pending["token_endpoint"],
            code=code,
            client_id=pending["client_id"],
            redirect_uri=pending["redirect_uri"],
            code_verifier=pending["pkce"].code_verifier,
            client_secret=pending["client_secret"],
        )
    except Exception as exc:  # token exchange failure is a launch failure, not a 500
        return render(
            request, "_callback_result.html", ok=False,
            message=f"Token exchange failed: {exc}",
        )

    # A normal launch only proves the state round-trip. AUTH-007..010's other
    # negative-path probes (wrong verifier, replayed code, cross-patient read,
    # id_token signature) are deliberate misuse this endpoint never attempts on
    # purpose — those stay needs_human here and are proven by
    # tests/test_smart_launch_e2e.py (pytest -m launch) instead.
    pipeline.set_live_token(token, auth_probe={"state_validated": True})

    return render(
        request, "_callback_result.html", ok=True,
        message=f"Live launch complete for patient {token.patient!r}. "
                "Go to the dashboard and run a live compliance check.",
    )


# --------------------------------------------------------------------------- #
# JSON API
# --------------------------------------------------------------------------- #
@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok", "version": "0.2.0"}


@app.post("/api/run")
def api_run() -> dict[str, Any]:
    report, _ = _execute_run({})
    summary = report.summary()
    return {
        "run_id": report.run_id,
        "summary": summary["counts"],
        "message": "Pipeline complete. GET /api/runs/{run_id} for full findings.",
    }


@app.get("/api/report")
def api_report() -> dict[str, Any]:
    if _last_report is None:
        raise HTTPException(status_code=404, detail="No report yet. POST /api/run first.")
    return {
        "run_id": _last_report.run_id,
        "findings": [f.model_dump() for f in _last_report.findings],
        "summary": _last_report.summary(),
    }


@app.get("/api/runs")
def api_runs() -> list[dict[str, Any]]:
    return store.list_runs()


@app.get("/api/runs/{run_id}")
def api_run_detail(run_id: str) -> JSONResponse:
    run = _load_run_or_404(run_id)
    report: Report = run["report"]
    return JSONResponse(
        {
            "run_id": report.run_id,
            "created_at": run["created_at"],
            "config": run["config"],
            "findings": [f.model_dump() for f in report.findings],
            "summary": report.summary(),
        }
    )
