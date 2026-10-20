"""Rule catalog loader — reads all rules/*.json files."""
from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import BaseModel

from ..paths import resource_root

RULES_DIR = resource_root() / "rules"


class Rule(BaseModel):
    id: str
    category: str
    # What a pass actually proves — see SUBJECTS in viewmodel.py. A rule that
    # only exercises Epic must never read as evidence about the app itself.
    subject: str = ""
    source: str
    severity: str
    evidence_needed: str
    check_type: str  # "automated" | "llm"
    description: str
    remediation_hint: str = ""
    # Launch profiles the rule applies to (config.launch_profile); empty = all.
    # A rule that does not apply is not run, rather than reported needs_human.
    profiles: list[str] = []

    def applies_to(self, launch_profile: str) -> bool:
        return not self.profiles or launch_profile in self.profiles


def rules_fingerprint() -> str:
    """Short hash of the rule files, stored with each run so the UI can tell
    when results predate a rule change."""
    h = hashlib.sha256()
    for f in sorted(RULES_DIR.glob("*.json")) + sorted(RULES_DIR.glob("reviewer/*.json")):
        h.update(f.name.encode() + b"\0" + f.read_bytes())
    return h.hexdigest()[:16]


def load_rules() -> list[Rule]:
    """The automated tests, then the reviewer questions (ADR-019) as tests."""
    from ..reviewer_questions import load_library

    rules: list[Rule] = []
    for f in sorted(RULES_DIR.glob("*.json")):
        data = json.loads(f.read_text())
        for item in data:
            rules.append(Rule(**item))
    rules += [Rule(**q.as_rule()) for q in load_library()]
    return rules


def is_reviewer_question(rule: Rule) -> bool:
    return rule.category.startswith("rq_")
