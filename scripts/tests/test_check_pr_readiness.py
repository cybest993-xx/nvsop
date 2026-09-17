from __future__ import annotations

import unittest

from scripts.check_pr_readiness import evaluate


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
