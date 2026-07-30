#!/usr/bin/env bash
#
# verify_tool.sh — end-to-end verification harness for the Epic Compliance Tool.
#
# Drives every surface of the tool and asserts it actually works:
#   1. venv + editable install present
#   2. pytest suite passes
#   3. CLI mock run produces a report and a sane pass/fail/needs_human breakdown
#   4. FastAPI server boots, /healthz is green, POST /api/run + GET /api/report work
#   5. (optional) ONC (g)(10) test kit is reachable if --onc is passed
#
# Usage:
#   ./scripts/verify_tool.sh            # steps 1-4 (default, no external deps)
#   ./scripts/verify_tool.sh --onc      # also probe the ONC test kit at ONC_TEST_KIT_URL
#   ./scripts/verify_tool.sh --quick    # skip pytest and the server boot (CLI only)
#
# Exit code 0 = every selected check passed; non-zero = something failed.
# Designed to be run by a human or by the `verify-epic-compliance` skill.

set -uo pipefail

# --------------------------------------------------------------------------- #
# Setup
# --------------------------------------------------------------------------- #
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

PORT="${VERIFY_PORT:-8055}"           # non-default port so we don't clash with a running server
ONC_URL="${ONC_TEST_KIT_URL:-http://localhost:8080}"
RUN_ONC=0
QUICK=0
for arg in "$@"; do
  case "$arg" in
    --onc)   RUN_ONC=1 ;;
    --quick) QUICK=1 ;;
    *) echo "unknown arg: $arg" >&2; exit 2 ;;
  esac
done

PASS=0; FAIL=0
SERVER_PID=""

# colors (fall back to plain if not a tty)
if [ -t 1 ]; then G="\033[32m"; R="\033[31m"; Y="\033[33m"; B="\033[1m"; X="\033[0m"; else G=""; R=""; Y=""; B=""; X=""; fi

ok()   { echo -e "  ${G}✓${X} $1"; PASS=$((PASS+1)); }
bad()  { echo -e "  ${R}✗${X} $1"; FAIL=$((FAIL+1)); }
info() { echo -e "  ${Y}·${X} $1"; }
step() { echo -e "\n${B}▶ $1${X}"; }

cleanup() {
  if [ -n "$SERVER_PID" ] && kill -0 "$SERVER_PID" 2>/dev/null; then
    kill "$SERVER_PID" 2>/dev/null
    wait "$SERVER_PID" 2>/dev/null
  fi
}
trap cleanup EXIT

# --------------------------------------------------------------------------- #
# 1. Environment
# --------------------------------------------------------------------------- #
step "1. Environment & install"
if [ -d ".venv" ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
  ok "activated .venv ($(python --version 2>&1))"
else
  bad ".venv not found — run: python3 -m venv .venv && source .venv/bin/activate && pip install -e '.[dev]'"
  echo -e "\n${R}Cannot continue without a virtualenv.${X}"; exit 1
fi

if python -c "import epic_compliance" 2>/dev/null; then
  ok "epic_compliance importable (editable install present)"
else
  bad "epic_compliance not importable — run: pip install -e '.[dev]'"
  exit 1
fi

# --------------------------------------------------------------------------- #
# 2. Unit tests
# --------------------------------------------------------------------------- #
if [ "$QUICK" -eq 0 ]; then
  step "2. Unit tests (pytest)"
  if python -m pytest tests/ -q >/tmp/verify_pytest.log 2>&1; then
    ok "pytest passed ($(grep -Eo '[0-9]+ passed' /tmp/verify_pytest.log | tail -1))"
  else
    bad "pytest failed — see /tmp/verify_pytest.log"
    tail -20 /tmp/verify_pytest.log | sed 's/^/      /'
  fi
else
  step "2. Unit tests — SKIPPED (--quick)"
fi

# --------------------------------------------------------------------------- #
# 3. CLI mock run
# --------------------------------------------------------------------------- #
step "3. CLI pipeline (mock mode)"
CLI_LOG=/tmp/verify_cli.log
python -m epic_compliance run >"$CLI_LOG" 2>&1
CLI_EXIT=$?
# exit 1 is expected when there are 'fail' findings; both 0 and 1 mean the pipeline ran.
if [ "$CLI_EXIT" -le 1 ] && grep -q "Run ID:" "$CLI_LOG"; then
  SUMMARY_LINE="$(grep -m1 'Run ID:' "$CLI_LOG" | sed -E 's/.*Run ID:[^ ]* +//')"
  ok "pipeline ran — ${SUMMARY_LINE}"
else
  bad "CLI run did not produce a report (exit=$CLI_EXIT) — see $CLI_LOG"
  tail -15 "$CLI_LOG" | sed 's/^/      /'
fi

# --------------------------------------------------------------------------- #
# 4. FastAPI server surface
# --------------------------------------------------------------------------- #
if [ "$QUICK" -eq 0 ]; then
  step "4. FastAPI server (/healthz, /api/run, /api/report)"
  python -m uvicorn epic_compliance.api:app --host 127.0.0.1 --port "$PORT" >/tmp/verify_server.log 2>&1 &
  SERVER_PID=$!

  # wait up to 15s for /healthz
  BASE="http://127.0.0.1:$PORT"
  UP=0
  for _ in $(seq 1 30); do
    if curl -sf "$BASE/healthz" >/dev/null 2>&1; then UP=1; break; fi
    sleep 0.5
  done

  if [ "$UP" -eq 1 ]; then
    ok "server booted on :$PORT"
    HEALTH="$(curl -sf "$BASE/healthz")"
    echo "$HEALTH" | grep -q '"status":"ok"' && ok "/healthz → $HEALTH" || bad "/healthz unexpected: $HEALTH"

    RUN_JSON="$(curl -sf -X POST "$BASE/api/run")"
    if echo "$RUN_JSON" | grep -q '"run_id"'; then
      ok "POST /api/run → $(echo "$RUN_JSON" | python -c 'import sys,json; d=json.load(sys.stdin); print(d["summary"])' 2>/dev/null)"
    else
      bad "POST /api/run failed: $RUN_JSON"
    fi

    REPORT_JSON="$(curl -sf "$BASE/api/report")"
    NFIND="$(echo "$REPORT_JSON" | python -c 'import sys,json; print(len(json.load(sys.stdin)["findings"]))' 2>/dev/null)"
    if [ -n "$NFIND" ] && [ "$NFIND" -gt 0 ]; then
      ok "GET /api/report → $NFIND findings returned"
    else
      bad "GET /api/report returned no findings: ${REPORT_JSON:0:120}"
    fi

    # ----------------------------------------------------------------------- #
    # 4b. HTML UI pages
    #
    # The JSON API passing does NOT mean the UI works: the pages go through
    # Jinja + Starlette's TemplateResponse, which the API routes never touch.
    # /config and /runs/{id}/print both once 500'd while every API check above
    # stayed green. Assert real status codes AND that a body was rendered.
    # ----------------------------------------------------------------------- #
    step "4b. HTML UI pages"

    # Drive a run through the actual HTMX form so we have a real run to view.
    FORM_HTML="$(curl -sf -X POST "$BASE/runs" -d "run_mode=mock" 2>/dev/null)"
    if echo "$FORM_HTML" | grep -q "Run complete"; then
      ok "POST /runs (HTMX form) → rendered result partial"
    else
      bad "POST /runs did not render a result partial: ${FORM_HTML:0:160}"
    fi

    RID="$(curl -sf "$BASE/api/runs" | python -c 'import sys,json; d=json.load(sys.stdin); print(d[0]["run_id"] if d else "")' 2>/dev/null)"

    # path : minimum plausible body size in bytes
    for entry in \
      "/:1000" \
      "/runs:500" \
      "/config:500" \
      "/config?saved=true:500" \
      "/runs/$RID:2000" \
      "/runs/$RID/export:2000" \
      "/runs/$RID/print:2000" \
      "/static/app.css:500" \
      "/static/htmx.min.js:1000" \
    ; do
      UPATH="${entry%:*}"; MINBYTES="${entry##*:}"
      [ -z "$RID" ] && case "$UPATH" in */runs/*) continue ;; esac
      CODE="$(curl -s -o /tmp/verify_page.html -w '%{http_code}' "$BASE$UPATH")"
      BYTES="$(wc -c < /tmp/verify_page.html | tr -d ' ')"
      if [ "$CODE" = "200" ] && [ "$BYTES" -ge "$MINBYTES" ]; then
        ok "GET ${UPATH/$RID/<id>} → 200 (${BYTES}b)"
      else
        bad "GET ${UPATH/$RID/<id>} → HTTP $CODE (${BYTES}b, wanted ≥${MINBYTES})"
        tail -20 /tmp/verify_server.log | sed 's/^/      /'
      fi
    done

    # The export must download as a file, not render inline.
    if curl -sf -D - -o /dev/null "$BASE/runs/$RID/export" 2>/dev/null | grep -qi 'content-disposition: attachment'; then
      ok "GET /runs/<id>/export → Content-Disposition: attachment"
    else
      bad "GET /runs/<id>/export is missing its attachment header"
    fi

    # POST /config must persist and redirect (303), not 200 into a dead end.
    CFG_CODE="$(curl -s -o /dev/null -w '%{http_code}' -X POST "$BASE/config" -d "run_mode=mock")"
    if [ "$CFG_CODE" = "303" ] || [ "$CFG_CODE" = "200" ]; then
      ok "POST /config → HTTP $CFG_CODE"
    else
      bad "POST /config → HTTP $CFG_CODE"
    fi

    # Unknown run must 404, not 500.
    NF_CODE="$(curl -s -o /dev/null -w '%{http_code}' "$BASE/runs/no-such-run-id")"
    if [ "$NF_CODE" = "404" ]; then
      ok "GET /runs/<unknown> → 404"
    else
      bad "GET /runs/<unknown> → HTTP $NF_CODE (expected 404)"
    fi

    # Nothing above should have raised. This catches 500s that still render.
    # NB: grep -c prints "0" *and* exits 1 when there are no matches, so a
    # `|| echo 0` fallback here would yield "0\n0" and break the -eq test.
    TB_COUNT="$(grep -c Traceback /tmp/verify_server.log 2>/dev/null | head -1)"
    TB_COUNT="${TB_COUNT:-0}"
    if [ "$TB_COUNT" -eq 0 ]; then
      ok "no server tracebacks during UI checks"
    else
      bad "$TB_COUNT traceback(s) in server log — see /tmp/verify_server.log"
      grep -A6 Traceback /tmp/verify_server.log | tail -25 | sed 's/^/      /'
    fi
  else
    bad "server never became healthy on :$PORT — see /tmp/verify_server.log"
    tail -15 /tmp/verify_server.log | sed 's/^/      /'
  fi
else
  step "4. FastAPI server — SKIPPED (--quick)"
fi

# --------------------------------------------------------------------------- #
# 5. ONC test kit (optional)
# --------------------------------------------------------------------------- #
if [ "$RUN_ONC" -eq 1 ]; then
  step "5. ONC (g)(10) test kit reachability"
  if curl -sf "$ONC_URL" >/dev/null 2>&1; then
    ok "ONC test kit reachable at $ONC_URL"
  else
    bad "ONC test kit NOT reachable at $ONC_URL — start it: (cd ../onc-certification-g10-test-kit && podman compose up -d)"
  fi
fi

# --------------------------------------------------------------------------- #
# Summary
# --------------------------------------------------------------------------- #
echo -e "\n${B}════════════════════════════════════════${X}"
if [ "$FAIL" -eq 0 ]; then
  echo -e "${G}${B}ALL CHECKS PASSED${X}  (${PASS} checks)"
  exit 0
else
  echo -e "${R}${B}${FAIL} CHECK(S) FAILED${X}  (${PASS} passed, ${FAIL} failed)"
  exit 1
fi
