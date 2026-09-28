"""Filesystem roots that differ between a dev checkout and a frozen macOS app.

A PyInstaller-frozen app has ``sys.frozen`` set and its bundled files live
under ``sys._MEIPASS`` (onefile) or next to the executable (onedir) — not at
the project root three parents above this file. It's also read-only, so
writable state (the run-history DB) has to live outside the bundle.
"""
from __future__ import annotations

import sys
from pathlib import Path


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def resource_root() -> Path:
    """Root for bundled read-only resources (rules/, templates/, static/)."""
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).parent.parent


def user_data_dir() -> Path:
    """Root for writable state (the run-history SQLite DB)."""
    if is_frozen():
        base = Path.home() / "Library" / "Application Support" / "Epic Compliance"
    else:
        base = Path(__file__).parent.parent / "data"
    base.mkdir(parents=True, exist_ok=True)
    return base
