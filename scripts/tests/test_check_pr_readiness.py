from __future__ import annotations

import subprocess
import unittest
from unittest.mock import Mock, patch

from scripts.check_pr_readiness import evaluate, protection_state


class PrReadinessTest(unittest.TestCase):
    def test_machine_ready_still_requires_manual_independent_review_confirmation(self) -> None:
        result = evaluate(
            {
                "state": "OPEN",
                "baseRefName": "main",
                "headRefName": "agent/test/task",
                "headRefOid": "abc",
                "reviewDecision": "APPROVED",
                "statusCheckRollup": [{"name": "CI required", "conclusion": "SUCCESS"}],
            },
            "protected",
            "agent/test/task",
            "abc",
        )
        self.assertTrue(result.ready)
        self.assertIn("independent_review=manual-confirmation-required", result.lines)
        self.assertIn("automated_readiness=ready", result.lines)
        self.assertIn("merge_guard=server-protected", result.lines)

    def test_plan_without_branch_protection_can_use_manual_ci_confirmation(self) -> None:
        result = evaluate(
            {
                "state": "OPEN",
                "baseRefName": "main",
                "headRefName": "agent/test/task",
                "headRefOid": "abc",
                "statusCheckRollup": [{"name": "CI required", "conclusion": "SUCCESS"}],
            },
            "unsupported",
            "agent/test/task",
            "abc",
        )
        self.assertTrue(result.ready)
        self.assertIn("branch_protection=unsupported", result.lines)
        self.assertIn("merge_guard=manual-ci-confirmation-required", result.lines)

    @patch("scripts.check_pr_readiness.run")
    def test_protection_state_only_classifies_the_plan_capability_error_as_unsupported(
        self, run_mock: Mock
    ) -> None:
        run_mock.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr=(
                "gh: Upgrade to GitHub Pro or make this repository public to enable this "
                "feature. (HTTP 403)"
            ),
        )
        self.assertEqual(protection_state("owner/repo", "main"), "unsupported")

        run_mock.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr=(
                "gh: Upgrade to GitHub Pro or make this repository public to enable this "
                "feature. (HTTP 500)"
            ),
        )
        self.assertEqual(protection_state("owner/repo", "main"), "unknown")

        run_mock.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr="gh: Resource not accessible by integration (HTTP 403)",
        )
        self.assertEqual(protection_state("owner/repo", "main"), "unknown")

    def test_different_local_branch_cannot_be_reported_as_automatically_ready(self) -> None:
        result = evaluate(
            {
                "state": "OPEN",
                "baseRefName": "main",
                "headRefName": "agent/test/task",
                "headRefOid": "abc",
                "statusCheckRollup": [{"name": "CI required", "conclusion": "SUCCESS"}],
            },
            "protected",
            "main",
            "def",
        )
        self.assertFalse(result.ready)
        self.assertIn("local_candidate=different-branch", result.lines)
        self.assertIn("blockers=local_candidate=different-branch", result.lines)

    def test_authority_change_requires_dispatch_impact_evidence(self) -> None:
        pr = {
            "state": "OPEN",
            "baseRefName": "main",
            "headRefName": "agent/test/task",
            "headRefOid": "abc",
            "statusCheckRollup": [{"name": "CI required", "conclusion": "SUCCESS"}],
            "body": (
                "Dispatch impact: `not-required`\nAffected open/ready Issues: `N/A`\nActions: `N/A`"
            ),
        }
        missing = evaluate(
            pr,
            "protected",
            "agent/test/task",
            "abc",
            ["docs/engineering/issues.md"],
        )
        self.assertFalse(missing.ready)
        self.assertIn("dispatch_impact_review=required", missing.lines)
        self.assertIn("dispatch_impact_evidence=missing", missing.lines)

        pr["body"] = (
            "Dispatch impact: `reviewed`\n"
            "Affected open/ready Issues: `none-found`\n"
            "Actions: `scan-recorded`"
        )
        reviewed = evaluate(
            pr,
            "protected",
            "agent/test/task",
            "abc",
            ["docs/engineering/issues.md"],
        )
        self.assertTrue(reviewed.ready)
        self.assertIn("dispatch_impact_evidence=present", reviewed.lines)

    def test_synthetic_public_seam_change_requires_architecture_review_evidence(self) -> None:
        pr = {
            "state": "OPEN",
            "baseRefName": "main",
            "headRefName": "agent/test/task",
            "headRefOid": "abc",
            "statusCheckRollup": [{"name": "CI required", "conclusion": "SUCCESS"}],
            "body": ("Architecture review: `not-required`\nArchitecture authority checked: `N/A`"),
        }
        changed = ["apps/control-api/src/factory_sop/synthetic_owner/api.py"]
        missing = evaluate(pr, "protected", "agent/test/task", "abc", changed)
        self.assertFalse(missing.ready)
        self.assertIn("architecture_review=required", missing.lines)
        self.assertIn("architecture_review_evidence=missing", missing.lines)

        pr["body"] = (
            "Architecture review: `reviewed`\n"
            "Architecture authority checked: `docs/engineering/architecture.md`"
        )
        reviewed = evaluate(pr, "protected", "agent/test/task", "abc", changed)
        self.assertTrue(reviewed.ready)
        self.assertIn("architecture_review_evidence=present", reviewed.lines)

    def test_missing_ci_or_unreadable_protection_never_claims_automated_readiness(self) -> None:
        result = evaluate(
            {
                "state": "OPEN",
                "baseRefName": "main",
                "headRefName": "agent/test/task",
                "headRefOid": "abc",
                "statusCheckRollup": [],
            },
            "unknown",
            "agent/test/task",
            "abc",
        )
        self.assertFalse(result.ready)
        self.assertIn("ci_required=missing", result.lines)
        self.assertIn("branch_protection=unknown", result.lines)
        self.assertIn("automated_readiness=blocked", result.lines)


if __name__ == "__main__":
    unittest.main()
