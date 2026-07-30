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

from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import store
from .config import OVERRIDABLE_FIELDS, config_with_overrides, get_config
from .models import Report
from .pipeline import run_full_pipeline
from .viewmodel import build_report_view

BASE_DIR = Path(__file__).parent

app = FastAPI(title="Epic Compliance Tool", version="0.2.0")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

_last_report: Report | None = None


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
