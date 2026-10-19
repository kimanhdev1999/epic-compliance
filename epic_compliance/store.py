"""SQLite-backed persistence for run history and saved config defaults.

Stores each completed pipeline run (full report JSON + the config snapshot it
ran against + a timestamp) so the web app can show history and let users
re-open / compare / export past reports. Also keeps the last-used config so the
run form can prefill sensible defaults instead of starting blank every time.

Uses the stdlib ``sqlite3`` module only — no new dependency.
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import Report
from .paths import user_data_dir

# Where the DB lives. Override with EPIC_COMPLIANCE_DB for tests / custom paths.
DEFAULT_DB_PATH = user_data_dir() / "runs.db"

# Config keys we allow to be snapshotted / saved as defaults. Secrets are stored
# but never rendered back to the browser (the web layer redacts them).
CONFIG_KEYS = (
    "fhir_base_url",
    "smart_launch_url",
    "smart_token_url",
    "oauth_client_id",
    "oauth_redirect_uri",
    "run_mode",
    "onc_test_kit_url",
    "anthropic_api_key",
    "launch_profile",
    "test_patient_query",
)


def _db_path() -> Path:
    env = os.environ.get("EPIC_COMPLIANCE_DB")
    return Path(env) if env else DEFAULT_DB_PATH


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Create tables if they don't exist. Safe to call repeatedly."""
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS runs (
                run_id      TEXT PRIMARY KEY,
                created_at  TEXT NOT NULL,
                run_mode    TEXT NOT NULL,
                fhir_base_url TEXT NOT NULL,
                pass_count  INTEGER NOT NULL,
                fail_count  INTEGER NOT NULL,
                needs_human_count INTEGER NOT NULL,
                report_json TEXT NOT NULL,
                config_json TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS evidence (
                id          TEXT PRIMARY KEY,
                rule_id     TEXT NOT NULL,
                created_at  TEXT NOT NULL,
                kind        TEXT NOT NULL,
                text        TEXT NOT NULL DEFAULT '',
                filename    TEXT NOT NULL DEFAULT '',
                media_type  TEXT NOT NULL DEFAULT '',
                size        INTEGER NOT NULL DEFAULT 0,
                sha256      TEXT NOT NULL DEFAULT '',
                stored_name TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_turns (
                seq        INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                state_json TEXT NOT NULL
            )
            """
        )
        conn.commit()


def _redact_config(config: dict[str, Any]) -> dict[str, Any]:
    """Replace secret values with a flag indicating presence, for storage."""
    safe = dict(config)
    if safe.get("anthropic_api_key"):
        safe["anthropic_api_key"] = "***set***"
    if safe.get("oauth_client_secret"):
        safe["oauth_client_secret"] = "***set***"
    return safe


def save_run(report: Report, config_snapshot: dict[str, Any]) -> str:
    """Persist a completed run. Returns the run_id. Secrets are redacted."""
    init_db()
    counts = report.summary()["counts"]
    created_at = datetime.now(timezone.utc).isoformat()
    safe_config = _redact_config(config_snapshot)
    with _connect() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO runs
                (run_id, created_at, run_mode, fhir_base_url,
                 pass_count, fail_count, needs_human_count, report_json, config_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                report.run_id,
                created_at,
                config_snapshot.get("run_mode", "mock"),
                config_snapshot.get("fhir_base_url", ""),
                counts["pass"],
                counts["fail"],
                counts["needs_human"],
                report.model_dump_json(),
                json.dumps(safe_config),
            ),
        )
        conn.commit()
    return report.run_id


def list_runs(limit: int = 100) -> list[dict[str, Any]]:
    """Return run summaries, newest first (no full report payload)."""
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT run_id, created_at, run_mode, fhir_base_url,
                   pass_count, fail_count, needs_human_count, config_json
            FROM runs ORDER BY created_at DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
    out = []
    for r in rows:
        data = dict(r)
        intake = (json.loads(data.pop("config_json")).get("assessment") or {}).get("intake") or {}
        data["app_name"] = intake.get("app_name", "")
        data["health_system"] = intake.get("health_system", "")
        out.append(data)
    return out


def get_run(run_id: str) -> dict[str, Any] | None:
    """Return a single run with its parsed Report and config, or None."""
    init_db()
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
    if row is None:
        return None
    data = dict(row)
    data["report"] = Report.model_validate_json(data.pop("report_json"))
    data["config"] = json.loads(data.pop("config_json"))
    return data


def recent_runs(limit: int = 10) -> list[dict[str, Any]]:
    """The newest ``limit`` runs with their full Report and config, newest first."""
    return [run for r in list_runs(limit=limit) if (run := get_run(r["run_id"])) is not None]


def latest_run() -> dict[str, Any] | None:
    """The newest run with its Report and config — the workspace's current state."""
    runs = recent_runs(limit=1)
    return runs[0] if runs else None


def delete_run(run_id: str) -> bool:
    """Delete a run. Returns True if a row was removed."""
    init_db()
    with _connect() as conn:
        cur = conn.execute("DELETE FROM runs WHERE run_id = ?", (run_id,))
        conn.commit()
        return cur.rowcount > 0


def save_config_defaults(config: dict[str, Any]) -> None:
    """Persist last-used config so the run form can prefill it. Redacts secrets."""
    init_db()
    safe = _redact_config(config)
    with _connect() as conn:
        for key in CONFIG_KEYS:
            if key in safe:
                conn.execute(
                    "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                    (key, str(safe[key])),
                )
        conn.commit()


def get_config_defaults() -> dict[str, str]:
    """Return saved config defaults for prefilling the form (secrets redacted)."""
    init_db()
    with _connect() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    return {r["key"]: r["value"] for r in rows if r["key"] != "last_intake"}


def save_last_intake(intake: dict[str, Any]) -> None:
    """Remember the last assessment answers so the intake form can prefill."""
    init_db()
    with _connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            ("last_intake", json.dumps(intake)),
        )
        conn.commit()


def get_last_intake() -> dict[str, Any]:
    init_db()
    with _connect() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = 'last_intake'").fetchone()
    return json.loads(row["value"]) if row else {}


# --------------------------------------------------------------------------- #
# Describe-your-app conversation (ADR-013)
# --------------------------------------------------------------------------- #
# Every turn appends a snapshot of the whole ChatState, so the conversation
# survives restarts and an earlier message can be edited by going back to the
# snapshot taken just before it. Rows are never updated; the newest is current.
def save_chat_state(state_json: str) -> None:
    init_db()
    with _connect() as conn:
        conn.execute(
            "INSERT INTO chat_turns (created_at, state_json) VALUES (?, ?)",
            (datetime.now(timezone.utc).isoformat(), state_json),
        )
        conn.commit()


def latest_chat_state() -> str | None:
    init_db()
    with _connect() as conn:
        row = conn.execute("SELECT state_json FROM chat_turns ORDER BY seq DESC LIMIT 1").fetchone()
    return row["state_json"] if row else None


def chat_snapshots() -> list[str]:
    """Every saved snapshot, newest first."""
    init_db()
    with _connect() as conn:
        rows = conn.execute("SELECT state_json FROM chat_turns ORDER BY seq DESC").fetchall()
    return [r["state_json"] for r in rows]


# --------------------------------------------------------------------------- #
# Evidence attached to tests (ADR-015)
# --------------------------------------------------------------------------- #
# One row per statement, file or link. File bytes live in evidence_dir();
# this table holds what the UI, the report and Claude need to know about them.
EVIDENCE_FIELDS = ("id", "rule_id", "created_at", "kind", "text", "filename",
                   "media_type", "size", "sha256", "stored_name")


def evidence_dir() -> Path:
    path = _db_path().parent / "evidence"
    path.mkdir(parents=True, exist_ok=True)
    return path


def add_evidence(item: dict[str, Any]) -> None:
    init_db()
    row = {k: item.get(k, "" if k != "size" else 0) for k in EVIDENCE_FIELDS}
    row["created_at"] = row["created_at"] or datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        conn.execute(
            f"INSERT INTO evidence ({', '.join(EVIDENCE_FIELDS)}) "
            f"VALUES ({', '.join('?' for _ in EVIDENCE_FIELDS)})",
            tuple(row[k] for k in EVIDENCE_FIELDS),
        )
        conn.commit()


def list_evidence(rule_id: str | None = None) -> list[dict[str, Any]]:
    """Evidence for one test (or all), oldest first."""
    init_db()
    with _connect() as conn:
        if rule_id is None:
            rows = conn.execute("SELECT * FROM evidence ORDER BY created_at").fetchall()
        else:
            rows = conn.execute("SELECT * FROM evidence WHERE rule_id = ? ORDER BY created_at",
                                (rule_id,)).fetchall()
    return [dict(r) for r in rows]


def get_evidence(evidence_id: str) -> dict[str, Any] | None:
    init_db()
    with _connect() as conn:
        row = conn.execute("SELECT * FROM evidence WHERE id = ?", (evidence_id,)).fetchone()
    return dict(row) if row else None


def delete_evidence(evidence_id: str) -> dict[str, Any] | None:
    """Remove one item (and its file). Returns what was removed."""
    item = get_evidence(evidence_id)
    if item is None:
        return None
    with _connect() as conn:
        conn.execute("DELETE FROM evidence WHERE id = ?", (evidence_id,))
        conn.commit()
    if item["stored_name"]:
        (evidence_dir() / item["stored_name"]).unlink(missing_ok=True)
    return item
