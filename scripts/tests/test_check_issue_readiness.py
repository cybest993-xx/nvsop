from __future__ import annotations

import unittest
from pathlib import Path

from scripts.check_issue_readiness import REQUIRED_SECTIONS, evaluate

ROOT = Path(__file__).resolve().parents[2]

READY_BODY = """## Task type
implementation
## Priority
P1
## Plan ID
S999
## Parent map
#1
## Outcome
One observable result.
## Authority and current evidence
Roadmap plus current source.
## Existing seam and reuse
Existing public seam.
## Scope
Bounded writes.
## Acceptance criteria
- [ ] Observable behavior.
## Dependencies and blockers
None
## Evidence plan
Reuse the owning tests.
## Out of scope
Adjacent refactors.
## Dispatch readiness
- [x] Ready.
"""


class IssueReadinessTest(unittest.TestCase):
    def test_required_sections_stay_aligned_with_the_issue_form(self) -> None:
        template = (ROOT / ".github/ISSUE_TEMPLATE/task.yml").read_text(encoding="utf-8")
        for section in REQUIRED_SECTIONS:
            with self.subTest(section=section):
                self.assertIn(f"label: {section}", template)

    def test_ready_agent_issue_has_no_machine_blocker(self) -> None:
        result = evaluate(
            {
                "state": "OPEN",
                "body": READY_BODY,
                "labels": [{"name": "wayfinder:task"}, {"name": "ready-for-agent"}],
                "assignees": [],
            },
            [],
        )
        self.assertTrue(result.ready)
        self.assertIn("automated_readiness=ready", result.lines)
        self.assertIn("semantic_quality=manual-review-required", result.lines)

    def test_unknown_native_blockers_and_readiness_conflict_fail_closed(self) -> None:
        result = evaluate(
            {
                "state": "OPEN",
                "body": READY_BODY.replace("One observable result.", "_No response_"),
                "labels": [
                    {"name": "wayfinder:task"},
                    {"name": "ready-for-agent"},
                    {"name": "needs-info"},
                ],
                "assignees": [{"login": "someone"}],
            },
            None,
        )
        self.assertFalse(result.ready)
        blockers = next(line for line in result.lines if line.startswith("blockers="))
        self.assertIn("missing_sections=Outcome", blockers)
        self.assertIn("already_assigned", blockers)
        self.assertIn("native_blockers=unknown", blockers)
        self.assertIn("conflicting_readiness_labels", blockers)


if __name__ == "__main__":
    unittest.main()
