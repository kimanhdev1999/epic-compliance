# Epic Compliance Tool — Agent Instructions

## Project Goal
Build an automated Epic FHIR compliance verification tool: ONC (g)(10) conformance + LLM-assisted manual checks, Epic-only MVP. See `plan/epic-compliance-build-plan.md` for the full 8-week build plan (1–2 hrs/day).

## Key Directories
- `plan/` — build plan (source of truth for what to do next)
- `notes/` — running notes per day and per topic (always check before starting a day's work)
- `notes/docker-setup.md` — Docker context for the ONC test kit (read this before any Docker work)

## Related Repos (do not edit without being told)
- **ONC (g)(10) Test Kit:** `/Users/belleasia/BelleGitRepos/HealthcareApp/onc-certification-g10-test-kit`
  - Running at `http://localhost:8080`
  - Start: `cd` into that dir, run `./run.sh` or `podman compose up -d`
  - Stop: `podman compose down`
  - Uses **Podman** (not Docker) — use `podman compose` for all container commands

## Build Plan Tracking
Before starting any day's work:
1. Read `plan/epic-compliance-build-plan.md` to find the next unchecked day
2. Read any relevant notes in `notes/` for context
3. After completing a day, mark it `[x]` in the plan and create `notes/day{N}-results.md`

## Tech Stack Decisions (update as made)
- Backend language: Python 3.11+ / FastAPI (decided Day 8 — see notes/decisions.md ADR-001)
- Rule catalog format: JSON files in rules/ (ADR-002)
- LLM evaluator: Claude API, model claude-sonnet-4-6 (ADR-004)
- FHIR/SMART HTTP: httpx
- Config: pydantic-settings (env/.env)
- CLI output: rich
- Tests: pytest

## Agent Behaviors
- Always check `notes/docker-setup.md` before running Docker commands
- When running test suites, save raw output to `notes/day{N}-results.md`
- When making architectural decisions, note the reasoning in `notes/decisions.md`
- Never commit credentials or `.env` files
- Prefer `docker compose` (v2 CLI) over `docker-compose` (v1)

## MCP Servers Available
See `.claude/settings.json` for configured MCP servers.
- **filesystem** — read/write to this project and the ONC test kit directory
