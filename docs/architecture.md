# Architecture

Short orientation for a developer touching this codebase for the first time.

## Shape of the system

One Python package, two entrypoints (CLI and FastAPI), one shared pipeline.
Everything is synchronous; there is no queue, no background worker, no ORM.

```
Entrypoints            Core pipeline                        Output
-----------            -------------                        ------
__main__.py  ─┐        collect_evidence()                   rich table (CLI)
              ├──▶  pipeline.run_full_pipeline(config)  ──▶  Report ──▶ viewmodel ──▶ Jinja/HTMX
api.py       ─┘        run_pipeline() (rules engine)             └──▶ store.save_run() → SQLite
```

## Modules

| Module | Responsibility |
|---|---|
| `config.py` | `AppConfig` (pydantic-settings) from env/`.env`; `config_with_overrides()` applies the safe per-run subset from the web form |
| `models.py` | The only shared domain types: `Finding` and `Report` (+ `Report.summary()`) |
| `pipeline.py` | Orchestrator: evidence collection, then the rules engine. The one place that knows the whole flow |
| `smart/discovery.py` | Fetch + parse `.well-known/smart-configuration` (or return the mock fixture) |
| `smart/pkce.py` | PKCE S256 generation, authorization-URL builder, token exchange, `MOCK_TOKEN_RESPONSE` |
| `fhir/client.py` | Fetch US Core resources with a bearer token; mock fixtures in mock mode |
| `rules_engine/catalog.py` | Load `rules/*.json` into `Rule` objects |
| `rules_engine/automated_checks.py` | `AUTOMATED_CHECKS`: `rule_id → fn(evidence, rule) -> Finding` |
| `rules_engine/llm_evaluator.py` | Claude call for `check_type: llm`; strict JSON verdict + citation |
| `rules_engine/engine.py` | Iterate rules, dispatch by `check_type`, aggregate into a `Report` |
| `store.py` | SQLite (stdlib `sqlite3`): run history, config defaults, secret redaction |
| `viewmodel.py` | `Report` → sorted, grouped, template-ready dict. Keeps logic out of Jinja |
| `api.py` | FastAPI routes: HTML pages (HTMX) + JSON API |
| `templates/`, `static/` | Jinja templates and CSS/HTMX asset |

## Request flow (web run)

```
POST /runs (HTMX form)
  └─ api._execute_run(overrides)
       ├─ config_with_overrides()            # env defaults + safe form overrides
       ├─ pipeline.run_full_pipeline(config)
       │    ├─ collect_evidence(config)      # ← all I/O happens here
       │    │    ├─ fetch_smart_configuration()   → smart_configuration
       │    │    ├─ MOCK_TOKEN_RESPONSE           → token_response (secrets redacted)
       │    │    └─ FhirClient.fetch_all_us_core() → fhir_resources
       │    └─ rules_engine.run_pipeline(evidence)
       │         ├─ load_rules()             # rules/*.json
       │         └─ per rule → AUTOMATED_CHECKS[id](...) or evaluate_with_llm(...)
       │              → Finding(verdict, evidence, citation, severity, category)
       ├─ store.save_run(report, config_snapshot)   # redacted snapshot
       └─ store.save_config_defaults(...)           # prefill next form
  └─ build_report_view(report) → render `_run_result.html` partial
```

The CLI path is the same minus persistence: `cmd_run()` → `run_full_pipeline()`
→ `Report.summary()` → rich tables → `sys.exit(1)` if any verdict is `fail`.

Read paths (`GET /runs`, `/runs/{id}`, `/runs/{id}/export|print`, `/api/*`) never
touch the pipeline — they read SQLite and go through `build_report_view`.

## Design rules worth knowing

1. **Evidence is a plain dict.** `collect_evidence()` returns
   `{smart_configuration, token_response, fhir_resources, run_mode}`. Checks
   only see that dict — they never do I/O. That's what makes rules trivially
   testable and mock mode possible.
2. **Mock mode is a first-class path**, threaded from `config.run_mode` into
   discovery, the FHIR client, and the LLM evaluator. Every network boundary has
   a fixture behind it, so `pytest` and a fresh clone both run green offline.
3. **A check never crashes a run.** `engine.run_pipeline` wraps each rule in
   try/except and converts failures — and unregistered rule ids — into a
   `needs_human` finding.
4. **Secrets never leave the process.** Tokens are `[REDACTED]` in evidence;
   `store._redact_config` scrubs the config snapshot before it's persisted or
   rendered.
5. **An LLM verdict without a citation is downgraded** to `needs_human` in
   `llm_evaluator`. Verdicts must quote real evidence text.

## Where to add things

| You want to… | Do this |
|---|---|
| Add a deterministic rule | Add the JSON entry in `rules/*.json` (`check_type: automated`) + a `_check_<id>` fn registered in `AUTOMATED_CHECKS` |
| Add a judgment rule | Add the JSON entry with `check_type: llm` — no code needed |
| Collect new evidence | Extend `collect_evidence()` and the dict it returns; rules read the new key |
| Wire the ONC test kit / HL7 validator | Those are seams in `automated_checks.py`; call out from a check fn using `config.onc_test_kit_url` |
| Complete live OAuth | Replace `MOCK_TOKEN_RESPONSE` in `pipeline.collect_evidence()` with a real authorization-code + PKCE flow using `smart/pkce.py` and a `/callback` route |
| Change report presentation | `viewmodel.build_report_view` first, templates second |

## Tests

`tests/test_rules_engine.py` (engine + checks against synthetic evidence) and
`tests/test_smart_launch_e2e.py`. Two pytest markers gate network work:
`live` (hits the Epic sandbox, no credentials) and `launch` (full interactive
SMART launch; needs `EPIC_SANDBOX_CLIENT_ID` + `--launch-interactive`).
