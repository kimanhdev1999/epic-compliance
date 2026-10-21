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

## Progress Tracking
`docs/progress.md` is the running record of what's done, what's open and what's next.
**Read it before starting any task, and update it in the same PR as your change**: move what
you closed to *Done* with the PR number, add what you found to *Open*, and bump *Last updated*.
CI enforces it: the `progress` job fails a PR that changes code without changing the file, and
`tests/test_progress.py` keeps *Open* in step with the UI harness's known-issue markers.

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
- **UI/UX work follows `docs/ux-redesign/requirements.md`** (the owner's standing UX requirements,
  kept with the design canvas). Above all, the UI, the exported report and the canvas never mention
  Claude, Anthropic or a model name: use "Answer review" and "Basis". No sample data in the UI.
- Prefer `docker compose` (v2 CLI) over `docker-compose` (v1)
- **One git worktree per session.** Several Claude sessions often run in this folder at once.
  Before changing any file, call EnterWorktree (or start with `claude --worktree`). To continue an
  existing branch, run `git worktree add .claude/worktrees/<name> <branch>` and enter that path. Never
  switch branches, `git stash`, or `git add -A` in the shared checkout; keep it on `main`.

## MCP Servers Available
See `.claude/settings.json` for configured MCP servers.
- **filesystem** — read/write to this project and the ONC test kit directory
