---
name: verify-epic-compliance
description: Verify the Epic Compliance Tool actually works end-to-end — runs the harness that drives the CLI pipeline (mock mode), the pytest suite, and the FastAPI server (/healthz, /api/run, /api/report), and optionally probes the ONC (g)(10) test kit. Use when the user asks to verify, smoke-test, sanity-check, or confirm the tool runs after a change, before a commit, or when standing it up fresh.
---

# Verify the Epic Compliance Tool

The verification harness lives at `scripts/verify_tool.sh`. It is the source of
truth for "does the tool actually work" — do not hand-run ad-hoc commands when
this skill applies; run the harness so results are consistent and complete.

## What it checks
1. **Environment** — `.venv` is present and `epic_compliance` is importable (editable install).
2. **Unit tests** — `pytest tests/` passes.
3. **CLI pipeline** — `python -m epic_compliance run` (mock mode) produces a report with a pass/fail/needs_human breakdown. Exit code 1 here is EXPECTED (it means there are `fail` findings), not a harness failure.
4. **FastAPI server** — boots uvicorn on a spare port, asserts `/healthz` is ok, `POST /api/run` returns a run_id + summary, and `GET /api/report` returns findings. The server is torn down automatically.
5. **HTML UI pages** (step 4b) — drives the HTMX form (`POST /runs`), then asserts every page renders: `/`, `/runs`, `/config`, `/runs/{id}`, `/export`, `/print`, and the static assets. Also checks the export's `Content-Disposition: attachment`, that `POST /config` redirects, that an unknown run 404s (not 500s), and that the server log holds **zero tracebacks**.
6. **ONC test kit** (opt-in) — with `--onc`, probes `ONC_TEST_KIT_URL` (default `http://localhost:8080`).

> A green JSON API does **not** imply a working UI. The pages render through Jinja +
> Starlette `TemplateResponse`, which no API route touches — `/config` and
> `/runs/{id}/print` both returned 500 while every API check stayed green. That is
> why step 4b asserts status code *and* a minimum body size *and* an empty traceback
> count: a page can return 200 and still be blank.

## How to run

```bash
./scripts/verify_tool.sh            # full default suite (no external services needed)
./scripts/verify_tool.sh --quick    # CLI mock run only (fastest; skips pytest + server)
./scripts/verify_tool.sh --onc      # also verify the ONC (g)(10) test kit is reachable
```

Prereq (first time only): `python3 -m venv .venv && source .venv/bin/activate && pip install -e '.[dev]'`

## Interpreting the result
- Exit `0` + `ALL CHECKS PASSED` → the tool is healthy, report it plainly.
- Exit non-zero → **read the failing step's output**, which the harness prints inline (and logs to `/tmp/verify_*.log`). Fix the root cause, then re-run. Do not paper over a real failure by editing the harness.
- The mock pipeline's `fail=1 / needs_human=3` is normal tool *output*, not a harness failure — those are compliance findings the tool is designed to surface.

## After a code change
Run the full suite (not `--quick`) before telling the user a change works. If you
touched the FastAPI routes, the rules engine, or the pipeline, the server + CLI
checks are what actually exercise that code path.

## Extending the harness
When new surfaces are added (e.g. HTML export, live OAuth flow, HL7 validator
wiring), add a corresponding check to `scripts/verify_tool.sh` so this skill keeps
covering the whole tool. Keep each check to: run the thing → assert observable
output → `ok`/`bad`.
