"""docs/progress.md stays in step with the code.

Every session reads it before starting and updates it in the same PR (CLAUDE.md).
These checks make that hold: every open UI-harness issue is listed under *Open*,
every marker listed there still exists (so a fix that removes its marker must also
move the line to *Done*), *Done* rows cite a PR, and *Last updated* is a real date.
CI also fails a PR that changes the app without touching the file (ci.yml).
"""
from __future__ import annotations

import ast
import re
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROGRESS = ROOT / "docs" / "progress.md"
UI_TESTS = ROOT / "tests" / "ui"


def _section(title: str) -> str:
    text = PROGRESS.read_text()
    m = re.search(rf"^## {re.escape(title)}\n(.*?)(?=^## |\Z)", text, re.S | re.M)
    assert m, f"docs/progress.md has no '## {title}' section"
    return m.group(1)


def _table_rows(section: str) -> list[list[str]]:
    rows = [[c.strip() for c in line.strip().strip("|").split("|")]
            for line in section.splitlines() if line.lstrip().startswith("|")]
    return [r for r in rows[2:]]  # skip the header and its |---| line


def _known_markers() -> set[str]:
    """The UI harness's open issues: tests marked known(...), and KNOWN entries."""
    found: set[str] = set()
    for path in sorted(UI_TESTS.glob("test_*.py")):
        source = path.read_text()
        for node in ast.parse(source).body:
            if isinstance(node, ast.FunctionDef):
                for dec in node.decorator_list:
                    seg = ast.get_source_segment(source, dec) or ""
                    if seg.startswith("known(") or "marks=known(" in seg:
                        found.add(node.name)
            elif isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "KNOWN" for t in node.targets):
                for key in ast.literal_eval(node.value):
                    found.add(f"KNOWN{key!r}")
    return found


def _listed_markers() -> set[str]:
    """Markers named in the Open table's last column, normalised like _known_markers."""
    listed: set[str] = set()
    for row in _table_rows(_section("Open")):
        for code in re.findall(r"`([^`]+)`", row[-1]):
            if code.startswith("KNOWN["):
                key = ast.literal_eval(code[len("KNOWN["):-1])
                listed.add(f"KNOWN{key!r}")
            elif code.startswith("test_"):
                listed.add(code.split("[")[0])
    return listed


def test_every_open_ui_issue_is_listed_in_progress():
    missing = _known_markers() - _listed_markers()
    assert not missing, (
        f"UI-harness issues not in docs/progress.md's Open table: {sorted(missing)}. "
        "Add a row naming each marker.")


def test_every_marker_listed_as_open_still_exists():
    stale = _listed_markers() - _known_markers()
    assert not stale, (
        f"docs/progress.md lists these as open, but their harness marker is gone: {sorted(stale)}. "
        "If the issue is fixed, move its row to Done with the PR number.")


def test_done_rows_cite_a_pr():
    rows = _table_rows(_section("Done"))
    assert rows, "docs/progress.md's Done table is empty"
    uncited = [r[0] for r in rows if not re.search(r"#\d+", r[-1])]
    assert not uncited, f"Done rows without a PR number (e.g. '#36'): {uncited}"


def test_last_updated_is_a_real_date():
    m = re.search(r"\*\*Last updated:\*\* (\d{4}-\d{2}-\d{2})", PROGRESS.read_text())
    assert m, "docs/progress.md needs a '**Last updated:** YYYY-MM-DD (…)' line"
    assert date.fromisoformat(m.group(1)) <= date.today()
