"""FastAPI web application — server-rendered UI (HTMX) + JSON API.

Pages (workspace shell, ADR-012)
  GET  /                      Home: getting-started checklist, framework readiness, needs attention
  GET  /frameworks            readiness per framework (ONC (g)(10), SMART, US Core, Epic, HIPAA, safety)
  GET  /frameworks/{key}      one framework and its tests
  GET  /tests                 every test with its status; filter by status / framework / category
  GET  /tests/{rule_id}       test detail (HTMX → side drawer, or full page)
  GET  /integrations          Epic sandbox, Claude, FHIR validator, ONC test kit — what each unlocks
  POST /assess/rerun          re-run the current app's plan, back to where you were
  GET  /assess                step 1 of an assessment: describe your app in a chat
  POST /assess/chat           one chat turn (HTMX → chat partial)
  POST /assess/edit           replace an earlier answer and continue from there
  POST /assess/new            start a new conversation (old ones stay saved)
  POST /assess/resume         back to the chat from the plan, conversation intact
  POST /assess/plan           step 2: the test plan generated from those answers
  POST /assess/run            step 3: run the plan, redirect to its report
  POST /runs                  ad-hoc run with raw config (HTMX → result partial)
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
from .assessment import BAA_LABELS, PLATFORM_LABELS, RUN_FIELDS, USERS_LABELS, Intake, build_plan
from .intake_chat import ChatState, next_turn, rewind, summary
from .config import OVERRIDABLE_FIELDS, config_with_overrides, get_config
from .models import Report
from .fhir.writeback import CLINICIAN_SCOPES
from .pipeline import run_full_pipeline
from .smart.discovery import fetch_smart_configuration
from .smart.pkce import build_authorization_url, exchange_code_for_token, generate_pkce
from .rules_engine.catalog import load_rules
from .viewmodel import build_report_view
from . import workspace as ws

BASE_DIR = Path(__file__).parent

app = FastAPI(title="Epic Compliance Tool", version="0.2.0")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def _asset(path: str) -> str:
    """Static URL with the file's mtime, so browsers fetch CSS/JS again after a change."""
    try:
        return f"/static/{path}?v={int((BASE_DIR / 'static' / path).stat().st_mtime)}"
    except OSError:
        return f"/static/{path}"


templates.env.globals["asset"] = _asset

_last_report: Report | None = None

# Pending live launches, keyed by the random `state` value, holding the PKCE
# verifier and endpoint info needed to complete the exchange at /callback.
# In-memory / single-tenant — matches config.py's single-tenant scope; not a
# session store for multiple concurrent users.
_pending_launches: dict[str, dict[str, Any]] = {}

#: Scopes requested for a live write-back launch: read scopes so the pipeline
#: can still fetch US Core resources, plus the write scopes WRITE-004 checks.
#: LAUNCH_PROFILE=clinician_standalone requests CLINICIAN_SCOPES instead.
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
    return templates.TemplateResponse(request, name, {"request": request, "nav": _nav(), **ctx})


def _current_intake() -> dict[str, Any]:
    """The app's answers: from the last run, else from the saved conversation."""
    last = store.get_last_intake()
    if last:
        return last
    chat = _saved_chat()
    return chat.intake.model_dump() if len(chat.messages) > 1 else {}


def _nav() -> dict[str, Any]:
    """Sidebar context on every page: which app this workspace is, and its open counts."""
    intake = _current_intake()
    latest = store.list_runs(limit=1)
    return {
        "app_name": intake.get("app_name") or "Your app",
        "health_system": intake.get("health_system", ""),
        "failing": latest[0]["fail_count"] if latest else 0,
        "review": latest[0]["needs_human_count"] if latest else 0,
    }


@app.on_event("startup")
def _startup() -> None:
    store.init_db()


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _execute_run(
    overrides: dict[str, str],
    rule_ids: set[str] | None = None,
    assessment: dict[str, Any] | None = None,
) -> tuple[Report, dict[str, Any]]:
    """Run the pipeline with per-run overrides, persist it, return (report, cfg).

    ``rule_ids``/``assessment`` come from an assessment plan: the checks to run,
    and the intake + scope stored with the run so its report can show them.
    """
    global _last_report
    config = config_with_overrides(overrides)
    report = run_full_pipeline(config, rule_ids=rule_ids)
    snapshot = config.model_dump()
    if assessment is not None:
        snapshot["assessment"] = assessment
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
def _intake_labels() -> dict[str, Any]:
    return {"users": USERS_LABELS, "platform": PLATFORM_LABELS, "baa": BAA_LABELS}


def _render_chat(request: Request, state: ChatState, partial: bool = False) -> HTMLResponse:
    ctx = {"state": state, "summary": summary(state.intake) if state.ready else []}
    if partial:
        return render(request, "_chat.html", **ctx)
    return render(request, "chat.html", active="assess", **ctx)


def _integrations() -> dict[str, dict[str, Any]]:
    """Each external service: status, and which tests it turns into real results."""
    cfg = get_config()
    defaults = store.get_config_defaults()
    client_id = defaults.get("oauth_client_id") or cfg.oauth_client_id
    rules = load_rules()
    if pipeline.get_live_token() is not None:
        epic = "connected"
    elif client_id and client_id != "mock-client-id":
        epic = "configured"
    else:
        epic = "not_connected"
    validator = "configured" if cfg.validator_mode != "off" and (
        cfg.validator_jar_path or cfg.validator_mode == "service") else "not_connected"
    return {
        "epic": {
            "name": "Epic sandbox", "vendor": "fhir.epic.com", "status": epic,
            "what": "Real SMART sign-in and real FHIR calls with your non-production client ID, "
                    "instead of sample responses.",
            "detail": f"Client ID {client_id}" if epic != "not_connected" else "No client ID yet",
            "unlocks": [r.id for r in rules if r.subject in ("app_registration", "tool_launch", "tool_sample")],
            "actions": [("Sign in with SMART", "/launch"), ("Edit connection", "/config")],
        },
        "claude": {
            "name": "Claude", "vendor": "Anthropic API", "status": "connected" if cfg.anthropic_api_key else "not_connected",
            "what": "Judges your security answers (TLS, audit logging, BAAs) against HIPAA and Epic's review "
                    "criteria, and runs the describe-your-app chat.",
            "detail": "ANTHROPIC_API_KEY is set" if cfg.anthropic_api_key else "Set ANTHROPIC_API_KEY in .env",
            "unlocks": [r.id for r in rules if r.check_type == "llm"],
            "actions": [],
        },
        "validator": {
            "name": "HL7 FHIR validator", "vendor": "validator_cli.jar or validator service", "status": validator,
            "what": "Validates the resources your app writes against US Core before Epic sees them.",
            "detail": cfg.validator_jar_path or (cfg.validator_service_url if validator != "not_connected"
                                                 else "Set VALIDATOR_JAR_PATH or VALIDATOR_MODE=service"),
            "unlocks": [r.id for r in rules if r.subject == "tool_sample" and r.category == "write_back"],
            "actions": [],
        },
        "onc": {
            "name": "ONC (g)(10) test kit", "vendor": "Inferno", "status": "configured" if cfg.onc_test_kit_url else "not_connected",
            "what": "The official certification test kit, for the full (g)(10) suite beyond these checks.",
            "detail": cfg.onc_test_kit_url or "Set ONC_TEST_KIT_URL",
            "unlocks": [],
            "actions": [("Open test kit", cfg.onc_test_kit_url)] if cfg.onc_test_kit_url else [],
        },
    }


def _workspace() -> dict[str, Any]:
    return ws.build_workspace(
        store.latest_run(), intake=_current_intake(),
        integrations=_integrations(), history=store.list_runs(limit=30),
    )


@app.get("/", response_class=HTMLResponse)
def home(request: Request) -> Any:
    """Home: what to do next, readiness per framework, and the tests needing attention."""
    return render(request, "home.html", active="home", w=_workspace(), integrations=_integrations())


@app.get("/frameworks", response_class=HTMLResponse)
def frameworks(request: Request) -> Any:
    return render(request, "frameworks.html", active="frameworks", w=_workspace())


@app.get("/frameworks/{key}", response_class=HTMLResponse)
def framework_detail(request: Request, key: str) -> Any:
    w = _workspace()
    fw = next((f for f in w["frameworks"] if f["key"] == key), None)
    if fw is None:
        raise HTTPException(status_code=404, detail=f"No framework {key}")
    return render(request, "framework.html", active="frameworks", w=w, fw=fw)


@app.get("/tests", response_class=HTMLResponse)
def tests_page(request: Request, status: str = "", framework: str = "", category: str = "", q: str = "") -> Any:
    w = _workspace()
    categories = sorted({(t["category"], t["category_label"]) for t in w["tests"]}, key=lambda c: c[1])
    return render(
        request, "tests.html", active="tests", w=w, frameworks=ws.FRAMEWORKS, categories=categories,
        status_labels=ws.STATUS_LABELS, filters={"status": status, "framework": framework, "category": category, "q": q},
    )


@app.get("/tests/{rule_id}", response_class=HTMLResponse)
def test_detail(request: Request, rule_id: str) -> Any:
    w = _workspace()
    test = next((t for t in w["tests"] if t["id"] == rule_id), None)
    if test is None:
        raise HTTPException(status_code=404, detail=f"No test {rule_id}")
    ctx = {"t": test, "w": w, "fw_names": {f.key: f.name for f in ws.FRAMEWORKS},
           "history": ws.history_of_test(store.recent_runs(limit=8), rule_id)}
    if request.headers.get("HX-Request"):
        return render(request, "_test_drawer.html", **ctx)
    return render(request, "test.html", active="tests", **ctx)


@app.get("/integrations", response_class=HTMLResponse)
def integrations_page(request: Request) -> Any:
    return render(request, "integrations.html", active="integrations", integrations=_integrations())


def _saved_chat() -> ChatState:
    raw = store.latest_chat_state()
    return ChatState.load(raw) if raw else ChatState.start()


def _save_chat(state: ChatState) -> ChatState:
    store.save_chat_state(state.model_dump_json())
    return state


def _adopt_chat(raw: str) -> ChatState:
    """Save a conversation a page sent back if it isn't the saved one yet
    (e.g. a tab opened before chats were saved)."""
    state = ChatState.load(raw)
    if len(state.messages) > 1 and state != _saved_chat():
        _save_chat(state)
    return state


@app.get("/assess", response_class=HTMLResponse)
def assess_start(request: Request) -> Any:
    """Step 1: describe your app in your own words; the tool asks follow-ups.

    Opens the saved conversation (ADR-013), so you can pick up where you left
    off or correct an answer when the app changes.
    """
    return _render_chat(request, _saved_chat())


@app.post("/assess/rerun")
def assess_rerun(next: str = Form("/")) -> RedirectResponse:
    """Re-run the current app's plan (last intake) and return to the page."""
    last = store.get_last_intake()
    if not last:
        return RedirectResponse(url="/assess", status_code=303)
    plan = build_plan(Intake.model_validate(last))
    _execute_run(plan.config_overrides(), plan.rule_ids, plan.to_dict())
    safe_next = next if next.startswith("/") and not next.startswith("//") else "/"
    return RedirectResponse(url=safe_next, status_code=303)


@app.post("/assess/chat", response_class=HTMLResponse)
def assess_chat(request: Request, message: str = Form(""), state: str = Form("")) -> Any:
    """One chat turn. The state round-trips through the page and is saved locally."""
    old = ChatState.load(state)
    new_state = next_turn(old, message, api_key=get_config().anthropic_api_key)
    if new_state != old:
        _save_chat(new_state)
    return _render_chat(request, new_state, partial=True)


@app.post("/assess/edit", response_class=HTMLResponse)
def assess_edit(request: Request, index: int = Form(...), message: str = Form("")) -> Any:
    """Replace an earlier answer: rewind to just before it and answer again."""
    current = _saved_chat()
    snapshots = [ChatState.load(raw) for raw in store.chat_snapshots()]
    base = rewind(current, index, snapshots)
    if base is None or not message.strip():
        return _render_chat(request, current, partial=True)
    return _render_chat(request, _save_chat(
        next_turn(base, message, api_key=get_config().anthropic_api_key)), partial=True)


@app.post("/assess/new")
def assess_new() -> RedirectResponse:
    """Start a new conversation. Earlier ones stay in the local database."""
    _save_chat(ChatState.start())
    return RedirectResponse(url="/assess", status_code=303)


@app.post("/assess/resume", response_class=HTMLResponse)
def assess_resume(request: Request, state: str = Form("")) -> Any:
    if not state:
        return _render_chat(request, _saved_chat())
    return _render_chat(request, _adopt_chat(state))


@app.post("/assess/plan", response_class=HTMLResponse)
async def assess_plan(request: Request) -> Any:
    """Step 2: show the checks this app needs, and why, before anything runs.

    The chat supplies what the app is; where to run it is picked on this page,
    prefilled from the last assessment.
    """
    form = dict(await request.form())
    if form.get("state"):
        _adopt_chat(str(form["state"]))
    intake = Intake.from_form(form)
    last = store.get_last_intake()
    env = {k: last[k] for k in RUN_FIELDS if k in last and k not in form}
    intake = intake.model_copy(update=env)
    if not intake.fhir_base_url:
        intake.fhir_base_url = get_config().fhir_base_url
    plan = build_plan(intake)
    return render(
        request, "plan.html", active="assess",
        intake=intake, plan=plan, labels=_intake_labels(), chat_state=form.get("state", ""),
        run_fields=RUN_FIELDS, live_token=pipeline.get_live_token() is not None,
        epic_client_id=bool(get_config().oauth_client_id),
    )


@app.post("/assess/run")
async def assess_run(request: Request) -> RedirectResponse:
    """Step 3: run exactly the planned checks, then show the report."""
    intake = Intake.from_form(dict(await request.form()))
    store.save_last_intake(intake.model_dump())
    plan = build_plan(intake)
    report, _ = _execute_run(plan.config_overrides(), plan.rule_ids, plan.to_dict())
    return RedirectResponse(url=f"/runs/{report.run_id}", status_code=303)


@app.post("/runs", response_class=HTMLResponse)
def create_run(
    request: Request,
    run_mode: str = Form("mock"),
    fhir_base_url: str = Form(""),
    oauth_client_id: str = Form(""),
    anthropic_api_key: str = Form(""),
    launch_profile: str = Form(""),
    test_patient_query: str = Form(""),
) -> Any:
    overrides = {
        "run_mode": run_mode,
        "fhir_base_url": fhir_base_url,
        "oauth_client_id": oauth_client_id,
        "anthropic_api_key": anthropic_api_key,
        "launch_profile": launch_profile,
        "test_patient_query": test_patient_query,
    }
    report, _ = _execute_run(overrides)
    view = build_report_view(report)
    return render(request, "_run_result.html", view=view)


@app.get("/runs", response_class=HTMLResponse)
def history(request: Request) -> Any:
    return render(request, "history.html", active="history", runs=store.list_runs())


@app.get("/runs/{run_id}", response_class=HTMLResponse)
def report_detail(request: Request, run_id: str) -> Any:
    run = _load_run_or_404(run_id)
    view = build_report_view(run["report"])
    return render(
        request, "report.html", active="history", view=view, run=run,
        assessment=run["config"].get("assessment"), labels=_intake_labels(),
    )


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
    cfg.pop("anthropic_api_key", None)  # never sent to the page
    return render(request, "config.html", active="config", cfg=cfg, saved=saved)


@app.post("/config")
def save_config(
    fhir_base_url: str = Form(""),
    smart_launch_url: str = Form(""),
    smart_token_url: str = Form(""),
    oauth_client_id: str = Form(""),
    oauth_redirect_uri: str = Form(""),
    onc_test_kit_url: str = Form(""),
) -> RedirectResponse:
    store.save_config_defaults(
        {
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
        scopes=CLINICIAN_SCOPES if config.launch_profile == "clinician_standalone" else LAUNCH_SCOPES,
        pkce=pkce,
        state=state,
        aud=config.fhir_base_url,
    )
    return RedirectResponse(url=url, status_code=302)


@app.get("/launch/status")
def launch_status() -> dict[str, bool]:
    """Whether a SMART sign-in has completed; the plan page polls this."""
    return {"signed_in": pipeline.get_live_token() is not None}


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
        message=(
            # A clinician standalone login has no patient until one is searched for.
            f"Live launch complete for patient {token.patient!r}. " if token.patient
            else f"Live launch complete, signed in as {token.fhir_user or 'unknown user'!r}. "
        ) + "Go back to your test plan and run the assessment.",
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
