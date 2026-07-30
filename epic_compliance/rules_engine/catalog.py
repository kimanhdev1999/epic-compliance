"""Rule catalog loader — reads all rules/*.json files."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel


RULES_DIR = Path(__file__).parent.parent.parent / "rules"


class Rule(BaseModel):
    id: str
    category: str
    source: str
    severity: str
    evidence_needed: str
    check_type: str  # "automated" | "llm"
    description: str
    remediation_hint: str = ""


def load_rules() -> list[Rule]:
    rules: list[Rule] = []
    for f in sorted(RULES_DIR.glob("*.json")):
        data = json.loads(f.read_text())
        for item in data:
            rules.append(Rule(**item))
    return rules
