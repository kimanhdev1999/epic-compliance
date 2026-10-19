"""Evidence a developer attaches to a test (ADR-015).

A test's result says what the tool observed. Evidence is what the developer
brings to back up the things the tool can't observe (audit logging, TLS
settings, BAAs): a statement, a file (log sample, config, screenshot, policy
PDF) or a link. It is stored locally, Claude reviews it for the AI-reviewed
tests, and the report lists it next to the test it supports.

Files must not contain real patient data; the UI says so on upload.
"""
from __future__ import annotations

import base64
import hashlib
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from . import store

MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_TEXT_CHARS = 20_000       # per text file sent to Claude
MAX_NOTE_CHARS = 5_000
MAX_LLM_BYTES = 20 * 1024 * 1024  # images + PDFs sent to Claude for one test

TEXT_TYPES = {
    ".txt": "text/plain", ".log": "text/plain", ".md": "text/markdown", ".csv": "text/csv",
    ".json": "application/json", ".yaml": "text/yaml", ".yml": "text/yaml",
    ".xml": "application/xml", ".conf": "text/plain", ".ini": "text/plain", ".tf": "text/plain",
}
IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
               ".gif": "image/gif", ".webp": "image/webp"}
PDF_TYPES = {".pdf": "application/pdf"}
ALLOWED_TYPES = {**TEXT_TYPES, **IMAGE_TYPES, **PDF_TYPES}
ACCEPT = ",".join(sorted(ALLOWED_TYPES))


class EvidenceError(ValueError):
    """Shown to the user as-is."""


def _safe_name(filename: str) -> str:
    name = Path(filename or "file").name.strip() or "file"
    return "".join(c for c in name if c.isprintable() and c not in '<>:"/\\|?*')[:120] or "file"


def add_note(rule_id: str, text: str) -> None:
    text = text.strip()
    if not text:
        return
    if len(text) > MAX_NOTE_CHARS:
        raise EvidenceError(f"Keep a statement under {MAX_NOTE_CHARS:,} characters, or attach it as a file.")
    store.add_evidence({"id": uuid.uuid4().hex, "rule_id": rule_id, "kind": "note", "text": text})


def add_link(rule_id: str, url: str) -> None:
    url = url.strip()
    if not url:
        return
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise EvidenceError("Links must start with http:// or https://.")
    store.add_evidence({"id": uuid.uuid4().hex, "rule_id": rule_id, "kind": "link", "text": url})


def add_file(rule_id: str, filename: str, data: bytes) -> None:
    name = _safe_name(filename)
    ext = Path(name).suffix.lower()
    if ext not in ALLOWED_TYPES:
        raise EvidenceError(f"{name}: use a text, log, JSON, YAML, CSV, PDF or image file.")
    if not data:
        raise EvidenceError(f"{name} is empty.")
    if len(data) > MAX_FILE_BYTES:
        raise EvidenceError(f"{name} is over {MAX_FILE_BYTES // (1024 * 1024)} MB.")
    if ext in TEXT_TYPES:
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            raise EvidenceError(f"{name} isn't UTF-8 text.") from None
    item_id = uuid.uuid4().hex
    stored = f"{item_id}{ext}"
    (store.evidence_dir() / stored).write_bytes(data)
    store.add_evidence({
        "id": item_id, "rule_id": rule_id, "kind": "file", "filename": name,
        "media_type": ALLOWED_TYPES[ext], "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(), "stored_name": stored,
    })


def file_path(item: dict[str, Any]) -> Path | None:
    if item.get("kind") != "file" or not item.get("stored_name"):
        return None
    path = store.evidence_dir() / item["stored_name"]
    return path if path.is_file() else None


def by_rule(items: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        out.setdefault(item["rule_id"], []).append(item)
    return out


def snapshot(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """What a run records about the evidence it used: no file contents, but
    the hash, so the report can show which exact file was reviewed."""
    keep = ("id", "rule_id", "created_at", "kind", "text", "filename", "media_type", "size", "sha256")
    return [{k: item.get(k) for k in keep} for item in items]


def for_claude(items: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """(text summary, content blocks) for Claude: statements, links and text
    files inline; images and PDFs as image / document blocks."""
    lines: list[str] = []
    blocks: list[dict[str, Any]] = []
    budget = MAX_LLM_BYTES
    for n, item in enumerate(items, 1):
        if item["kind"] == "note":
            lines.append(f"[{n}] Statement: {item['text']}")
        elif item["kind"] == "link":
            lines.append(f"[{n}] Link (not opened by the reviewer): {item['text']}")
        else:
            path = file_path(item)
            if path is None:
                lines.append(f"[{n}] File {item['filename']}: missing from disk")
            elif item["media_type"] in TEXT_TYPES.values():
                text = path.read_text(encoding="utf-8", errors="replace")
                cut = " (truncated)" if len(text) > MAX_TEXT_CHARS else ""
                lines.append(f"[{n}] File {item['filename']}{cut}:\n{text[:MAX_TEXT_CHARS]}")
            elif item["size"] <= budget:
                budget -= item["size"]
                data = base64.standard_b64encode(path.read_bytes()).decode()
                kind = "document" if item["media_type"] == "application/pdf" else "image"
                lines.append(f"[{n}] File {item['filename']}: attached below as {kind} [{n}]")
                blocks.append({"type": "text", "text": f"Evidence [{n}]: {item['filename']}"})
                blocks.append({"type": kind, "source": {"type": "base64",
                                                        "media_type": item["media_type"], "data": data}})
            else:
                lines.append(f"[{n}] File {item['filename']}: too large to review together with the others")
    return "\n".join(lines), blocks
