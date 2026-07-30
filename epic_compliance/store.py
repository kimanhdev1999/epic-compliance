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

# Where the DB lives. Override with EPIC_COMPLIANCE_DB for tests / custom paths.
DEFAULT_DB_PATH = Path(__file__).parent.parent / "data" / "runs.db"

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
                   pass_count, fail_count, needs_human_count
            FROM runs ORDER BY created_at DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


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
    return {r["key"]: r["value"] for r in rows}
