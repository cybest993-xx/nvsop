from __future__ import annotations

import subprocess
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from scripts.check_issue_readiness import REQUIRED_SECTIONS, architecture_freshness, evaluate

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
## Architecture impact
none
## Architecture authority
N/A
## Architecture validation baseline
N/A
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

    def test_map_is_valid_but_never_dispatchable(self) -> None:
        result = evaluate(
            {
                "state": "OPEN",
                "body": """## Delivery map
- #292
## Acceptance criteria
- [ ] Children close independently.
## Dependencies and blockers
None
## Evidence plan
Aggregate child evidence.
## Out of scope
No implementation here.
## Map readiness
Children own execution.
""",
                "labels": [{"name": "wayfinder:map"}],
                "assignees": [],
            },
            [],
        )
        self.assertTrue(result.ready)
        self.assertIn("map_structure=valid", result.lines)
        self.assertIn("dispatchable=no", result.lines)
        self.assertIn("automated_readiness=map-valid", result.lines)

    def test_changed_architecture_authority_revokes_ready_state(self) -> None:
        architecture_body = READY_BODY.replace(
            "## Architecture impact\nnone\n## Architecture authority\nN/A\n"
            "## Architecture validation baseline\nN/A\n",
            "## Architecture impact\narchitecture-sensitive\n"
            "## Architecture authority\n- `docs/engineering/architecture.md`\n"
            "## Architecture validation baseline\n"
            "main@0123456789abcdef0123456789abcdef01234567\n",
        )
        issue = {
            "state": "OPEN",
            "body": architecture_body,
            "labels": [{"name": "wayfinder:task"}, {"name": "ready-for-agent"}],
            "assignees": [],
        }
        self.assertTrue(evaluate(issue, [], "unchanged").ready)
        changed = evaluate(issue, [], "changed")
        self.assertFalse(changed.ready)
        self.assertIn("existing_seam_revalidation=required", changed.lines)
        self.assertIn("blockers=manual-revalidation-required", changed.lines)

    @patch("scripts.check_issue_readiness.run")
    def test_nonexistent_authority_path_is_not_reported_unchanged(self, run_mock: Mock) -> None:
        run_mock.side_effect = [
            subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
            subprocess.CompletedProcess(args=[], returncode=0, stdout="current-sha\n", stderr=""),
            subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
            subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr=""),
            subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr=""),
        ]
        result = architecture_freshness(
            {
                "body": (
                    "## Architecture impact\narchitecture-sensitive\n"
                    "## Architecture authority\n- `docs/engineering/does-not-exist.md`\n"
                    "## Architecture validation baseline\n"
                    "main@0123456789abcdef0123456789abcdef01234567\n"
                )
            }
        )
        self.assertEqual(result, "invalid-authority-paths")

    @patch("scripts.check_issue_readiness.run")
    def test_deleted_authority_path_stays_a_changed_authority(self, run_mock: Mock) -> None:
        run_mock.side_effect = [
            subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
            subprocess.CompletedProcess(args=[], returncode=0, stdout="current-sha\n", stderr=""),
            subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
            subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
            subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr=""),
            subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr=""),
        ]
        result = architecture_freshness(
            {
                "body": (
                    "## Architecture impact\narchitecture-sensitive\n"
                    "## Architecture authority\n- `docs/engineering/architecture.md`\n"
                    "## Architecture validation baseline\n"
                    "main@0123456789abcdef0123456789abcdef01234567\n"
                )
            }
        )
        self.assertEqual(result, "changed")

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
