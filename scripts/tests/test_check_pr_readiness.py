from __future__ import annotations

import subprocess
import unittest
from unittest.mock import Mock, patch

from scripts.check_pr_readiness import (
    architecture_review_evidence,
    body_field,
    evaluate,
    landing_gate_enforcement,
    pr_files,
    protection_state,
    requires_architecture_review,
    requires_dispatch_impact,
    requires_harness_review,
)


class PrReadinessTest(unittest.TestCase):
    def test_machine_ready_still_requires_manual_independent_review_confirmation(self) -> None:
        result = evaluate(
            {
                "state": "OPEN",
                "baseRefName": "main",
                "headRefName": "agent/test/task",
                "headRefOid": "abc",
                "reviewDecision": "APPROVED",
                "statusCheckRollup": [
                    {"name": "CI required", "conclusion": "SUCCESS"},
                    {"name": "Landing gate", "conclusion": "SUCCESS"},
                ],
            },
            "protected",
            "agent/test/task",
            "abc",
            landing_enforcement="required",
        )
        self.assertTrue(result.ready)
        self.assertIn("independent_review=manual-confirmation-required", result.lines)
        self.assertIn("landing_gate=success", result.lines)
        self.assertIn("landing_gate_enforcement=required", result.lines)
        self.assertIn("automated_readiness=ready", result.lines)
        self.assertIn("merge_guard=server-protected", result.lines)

    def test_unsupported_branch_protection_is_not_ready(self) -> None:
        # 不可读/不支持的 protection 无法证明服务器强制双上下文，绝不能作为落地路径就绪。
        result = evaluate(
            {
                "state": "OPEN",
                "baseRefName": "main",
                "headRefName": "agent/test/task",
                "headRefOid": "abc",
                "statusCheckRollup": [
                    {"name": "CI required", "conclusion": "SUCCESS"},
                    {"name": "Landing gate", "conclusion": "SUCCESS"},
                ],
            },
            "unsupported",
            "agent/test/task",
            "abc",
            landing_enforcement="required",
        )
        self.assertFalse(result.ready)
        self.assertIn("branch_protection=unsupported", result.lines)
        self.assertIn("merge_guard=unverified", result.lines)
        self.assertIn("automated_readiness=blocked", result.lines)

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

    @patch("scripts.check_pr_readiness.run")
    def test_protection_state_accepts_ruleset_with_strict_ci_required(self, run_mock: Mock) -> None:
        run_mock.side_effect = [
            subprocess.CompletedProcess(
                args=[], returncode=1, stdout="", stderr="gh: Branch not protected (HTTP 404)"
            ),
            subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=(
                    '[{"type":"pull_request"},'
                    '{"type":"required_status_checks","parameters":{'
                    '"strict_required_status_checks_policy":true,'
                    '"required_status_checks":[{"context":"Another check"}]}},'
                    '{"type":"required_status_checks","parameters":{'
                    '"strict_required_status_checks_policy":true,'
                    '"required_status_checks":[{"context":"CI required"}]}}]'
                ),
                stderr="",
            ),
        ]

        self.assertEqual(protection_state("owner/repo", "main"), "protected")
        self.assertEqual(
            run_mock.call_args_list[1].args,
            ("gh", "api", "repos/owner/repo/rules/branches/main"),
        )

    @patch("scripts.check_pr_readiness.run")
    def test_ruleset_without_strict_ci_required_is_not_reported_as_protected(
        self, run_mock: Mock
    ) -> None:
        run_mock.side_effect = [
            subprocess.CompletedProcess(
                args=[], returncode=1, stdout="", stderr="gh: Branch not protected (HTTP 404)"
            ),
            subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=(
                    '[{"type":"pull_request"},'
                    '{"type":"required_status_checks","parameters":{'
                    '"strict_required_status_checks_policy":false,'
                    '"required_status_checks":[{"context":"CI required"}]}}]'
                ),
                stderr="",
            ),
        ]

        self.assertEqual(protection_state("owner/repo", "main"), "absent")

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
        blockers = next(line for line in result.lines if line.startswith("blockers="))
        self.assertIn("local_candidate=different-branch", blockers)
        self.assertIn("automated_readiness=blocked", result.lines)

    def test_authority_change_requires_dispatch_impact_evidence(self) -> None:
        pr = {
            "state": "OPEN",
            "baseRefName": "main",
            "headRefName": "agent/test/task",
            "headRefOid": "abc",
            "statusCheckRollup": [
                {"name": "CI required", "conclusion": "SUCCESS"},
                {"name": "Landing gate", "conclusion": "SUCCESS"},
            ],
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
            landing_enforcement="required",
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
            landing_enforcement="required",
        )
        self.assertTrue(reviewed.ready)
        self.assertIn("dispatch_impact_evidence=present", reviewed.lines)

    def test_synthetic_public_seam_change_requires_architecture_review_evidence(self) -> None:
        pr = {
            "state": "OPEN",
            "baseRefName": "main",
            "headRefName": "agent/test/task",
            "headRefOid": "abc",
            "statusCheckRollup": [
                {"name": "CI required", "conclusion": "SUCCESS"},
                {"name": "Landing gate", "conclusion": "SUCCESS"},
            ],
            "body": ("Architecture review: `not-required`\nArchitecture authority checked: `N/A`"),
        }
        changed = ["apps/control-api/src/factory_sop/synthetic_owner/api.py"]
        missing = evaluate(
            pr, "protected", "agent/test/task", "abc", changed, landing_enforcement="required"
        )
        self.assertFalse(missing.ready)
        self.assertIn("architecture_review=required", missing.lines)
        self.assertIn("architecture_review_evidence=missing", missing.lines)

        pr["body"] = (
            "Architecture review: `reviewed`\n"
            "Architecture authority checked: `docs/engineering/architecture.md`"
        )
        reviewed = evaluate(
            pr, "protected", "agent/test/task", "abc", changed, landing_enforcement="required"
        )
        self.assertTrue(reviewed.ready)
        self.assertIn("architecture_review_evidence=present", reviewed.lines)

    def test_edge_state_owner_change_requires_architecture_review(self) -> None:
        self.assertTrue(
            requires_architecture_review(
                ["apps/edge-runtime/src/edge_runtime/local_state/synthetic_store.py"]
            )
        )

    def test_composition_role_changes_require_architecture_review(self) -> None:
        for path in (
            "apps/control-api/src/factory_sop/overview.py",
            "apps/control-api/src/factory_sop/configuration/composition.py",
            "apps/control-api/src/factory_sop/synthetic_aggregate.py",
            "apps/control-api/src/factory_sop/synthetic/composition.py",
            "apps/control-api/src/factory_sop/synthetic/adapters/composition.py",
        ):
            with self.subTest(path=path):
                self.assertTrue(requires_architecture_review([path]))

    def test_body_field_reads_complete_inline_value_without_next_line(self) -> None:
        body = (
            "Architecture review: `reviewed`\n"
            "Architecture authority checked: `path1`, `path2`\n"
            "Next field: should-not-be-consumed"
        )
        self.assertEqual(body_field(body, "Architecture authority checked"), "`path1`, `path2`")
        self.assertTrue(architecture_review_evidence(body))
        self.assertEqual(
            body_field("Architecture review:\nNext field: reviewed", "Architecture review"), ""
        )

    @patch("scripts.check_pr_readiness.run")
    def test_renamed_authority_keeps_previous_path_for_dispatch_impact(
        self, run_mock: Mock
    ) -> None:
        run_mock.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=(
                '[[{"filename":"docs/engineering/architecture-renamed.md",'
                '"previous_filename":"docs/engineering/architecture.md",'
                '"status":"renamed"}]]'
            ),
            stderr="",
        )
        files = pr_files("owner/repo", "123")
        self.assertIsNotNone(files)
        assert files is not None
        self.assertIn("docs/engineering/architecture.md", files)
        self.assertTrue(requires_dispatch_impact(files))

    def test_harness_review_matches_real_control_entries_not_ordinary_code(self) -> None:
        sensitive = (
            "Makefile",
            "AGENTS.md",
            "CLAUDE.md",
            ".gitignore",
            ".secrets.baseline",
            ".python-version",
            ".nvmrc",
            "pyproject.toml",
            "apps/edge-runtime/pyproject.toml",
            "packages/contracts/pyproject.toml",
            "package.json",
            "apps/control-web/package.json",
            "uv.lock",
            "pnpm-lock.yaml",
            "pnpm-workspace.yaml",
            "packages/contracts/breaking-changes.json",
            "apps/control-web/.prettierrc.json",
            "scripts/check_repo_policy.py",
            ".github/workflows/blocking-ci.yml",
            "docs/engineering/workflow.md",
            "apps/control-web/eslint.config.ts",
            "apps/control-web/tsconfig.app.json",
            "apps/control-web/vite.config.ts",
            "apps/control-web/playwright.config.ts",
            "apps/control-web/openapi-ts.config.ts",
            "tests/system/conftest.py",
            "apps/control-api/tests/integration/conftest.py",
        )
        ordinary = (
            "apps/control-api/src/factory_sop/overview.py",
            "apps/control-api/tests/unit/test_overview.py",
            "apps/control-web/src/modules/devices/CameraMediaPanel.vue",
            "apps/control-web/tests/integration/devices.spec.ts",
            "docs/design/mechanisms/control-plane.md",
            "README.md",
        )
        for path in sensitive:
            with self.subTest(path=path):
                self.assertTrue(requires_harness_review([path]))
        for path in ordinary:
            with self.subTest(path=path):
                self.assertFalse(requires_harness_review([path]))

    def test_harness_review_output_requires_manual_confirmation_for_control_paths(self) -> None:
        pr = {
            "state": "OPEN",
            "baseRefName": "main",
            "headRefName": "agent/test/task",
            "headRefOid": "abc",
            "statusCheckRollup": [{"name": "CI required", "conclusion": "SUCCESS"}],
        }
        required = evaluate(
            pr, "protected", "agent/test/task", "abc", ["scripts/check_repo_policy.py"]
        )
        self.assertIn("harness_review=required", required.lines)
        self.assertIn("harness_review_confirmation=manual-required", required.lines)

        not_required = evaluate(
            pr,
            "protected",
            "agent/test/task",
            "abc",
            ["apps/control-api/src/factory_sop/overview.py"],
        )
        self.assertIn("harness_review=not-required", not_required.lines)
        self.assertIn("harness_review_confirmation=not-required", not_required.lines)

    def test_body_self_report_does_not_cancel_harness_review(self) -> None:
        # 预检不读正文: 正文自报不能把门禁改动降级为免审查, 也不阻塞机器状态。
        result = evaluate(
            {
                "state": "OPEN",
                "baseRefName": "main",
                "headRefName": "agent/test/task",
                "headRefOid": "abc",
                "statusCheckRollup": [
                    {"name": "CI required", "conclusion": "SUCCESS"},
                    {"name": "Landing gate", "conclusion": "SUCCESS"},
                ],
                "body": "Harness review: reviewed\nHarness authority checked: reviewed",
            },
            "protected",
            "agent/test/task",
            "abc",
            ["Makefile"],
            landing_enforcement="required",
        )
        self.assertTrue(result.ready)
        self.assertIn("harness_review=required", result.lines)
        self.assertIn("harness_review_confirmation=manual-required", result.lines)
        self.assertNotIn("harness_review=not-required", result.lines)
        self.assertIn("automated_readiness=ready", result.lines)

    @patch("scripts.check_pr_readiness.run")
    def test_renamed_control_file_keeps_previous_path_for_harness_review(
        self, run_mock: Mock
    ) -> None:
        run_mock.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=(
                '[[{"filename":"scripts/renamed.py",'
                '"previous_filename":"scripts/check_repo_policy.py",'
                '"status":"renamed"}]]'
            ),
            stderr="",
        )
        files = pr_files("owner/repo", "123")
        self.assertIsNotNone(files)
        assert files is not None
        self.assertIn("scripts/check_repo_policy.py", files)
        self.assertTrue(requires_harness_review(files))

    @patch("scripts.check_pr_readiness.run")
    def test_malformed_file_enumeration_is_unknown_not_a_short_list(self, run_mock: Mock) -> None:
        for payload in (
            '[[{"filename":"a.py"}, "not-a-dict"]]',
            '[[{"previous_filename":"old.py"}]]',
            '[[{"filename":"new.py","status":"renamed"}]]',
            '[[{"filename":""}]]',
        ):
            with self.subTest(payload=payload):
                run_mock.return_value = subprocess.CompletedProcess(
                    args=[], returncode=0, stdout=payload, stderr=""
                )
                self.assertIsNone(pr_files("owner/repo", "123"))

    def test_unknown_file_enumeration_reports_unknown_harness_review(self) -> None:
        result = evaluate(
            {
                "state": "OPEN",
                "baseRefName": "main",
                "headRefName": "agent/test/task",
                "headRefOid": "abc",
                "statusCheckRollup": [{"name": "CI required", "conclusion": "SUCCESS"}],
            },
            "protected",
            "agent/test/task",
            "abc",
            None,
        )
        self.assertIn("harness_review=unknown", result.lines)
        self.assertIn("harness_review_confirmation=unknown", result.lines)

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

    def test_unsupported_protection_without_observable_landing_gate_is_not_ready(self) -> None:
        result = evaluate(
            {
                "state": "OPEN",
                "baseRefName": "main",
                "headRefName": "agent/test/task",
                "headRefOid": "abc",
                "statusCheckRollup": [
                    {"name": "CI required", "conclusion": "SUCCESS"},
                    {"name": "Landing gate", "conclusion": "SUCCESS"},
                ],
            },
            "unsupported",
            "agent/test/task",
            "abc",
        )
        self.assertFalse(result.ready)
        self.assertIn("landing_gate_enforcement=unknown", result.lines)
        self.assertIn("automated_readiness=blocked", result.lines)

    def test_missing_exact_head_landing_gate_blocks_readiness(self) -> None:
        result = evaluate(
            {
                "state": "OPEN",
                "baseRefName": "main",
                "headRefName": "agent/test/task",
                "headRefOid": "abc",
                "statusCheckRollup": [{"name": "CI required", "conclusion": "SUCCESS"}],
            },
            "protected",
            "agent/test/task",
            "abc",
            landing_enforcement="required",
        )
        self.assertFalse(result.ready)
        self.assertIn("landing_gate=missing", result.lines)
        self.assertIn("automated_readiness=blocked", result.lines)

    def test_missing_server_landing_gate_enforcement_blocks_readiness(self) -> None:
        result = evaluate(
            {
                "state": "OPEN",
                "baseRefName": "main",
                "headRefName": "agent/test/task",
                "headRefOid": "abc",
                "statusCheckRollup": [
                    {"name": "CI required", "conclusion": "SUCCESS"},
                    {"name": "Landing gate", "conclusion": "SUCCESS"},
                ],
            },
            "protected",
            "agent/test/task",
            "abc",
            landing_enforcement="missing",
        )
        self.assertFalse(result.ready)
        self.assertIn("landing_gate_enforcement=missing", result.lines)

    @patch("scripts.check_pr_readiness.run")
    def test_landing_gate_enforcement_reads_strict_required_ruleset(self, run_mock: Mock) -> None:
        run_mock.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=(
                '[{"type":"required_status_checks","parameters":{'
                '"strict_required_status_checks_policy":true,'
                '"required_status_checks":[{"context":"CI required"},'
                '{"context":"Landing gate"}]}}]'
            ),
            stderr="",
        )
        self.assertEqual(landing_gate_enforcement("owner/repo", "main"), "required")
        self.assertEqual(
            run_mock.call_args.args, ("gh", "api", "repos/owner/repo/rules/branches/main")
        )

    @patch("scripts.check_pr_readiness.run")
    def test_landing_gate_enforcement_requires_both_strict_contexts(self, run_mock: Mock) -> None:
        # 经典 API 的宽松可见性不能单独证明 CI 强制；缺任一上下文或非 strict 都不算已强制。
        cases = {
            "ci-only": (
                '[{"type":"required_status_checks","parameters":{'
                '"strict_required_status_checks_policy":true,'
                '"required_status_checks":[{"context":"CI required"}]}}]'
            ),
            "gate-only": (
                '[{"type":"required_status_checks","parameters":{'
                '"strict_required_status_checks_policy":true,'
                '"required_status_checks":[{"context":"Landing gate"}]}}]'
            ),
            "not-strict": (
                '[{"type":"required_status_checks","parameters":{'
                '"strict_required_status_checks_policy":false,'
                '"required_status_checks":[{"context":"CI required"},'
                '{"context":"Landing gate"}]}}]'
            ),
        }
        for label, payload in cases.items():
            with self.subTest(case=label):
                run_mock.return_value = subprocess.CompletedProcess(
                    args=[], returncode=0, stdout=payload, stderr=""
                )
                self.assertEqual(landing_gate_enforcement("owner/repo", "main"), "missing")

    @patch("scripts.check_pr_readiness.run")
    def test_landing_gate_enforcement_unions_strict_contexts_across_rules(
        self, run_mock: Mock
    ) -> None:
        run_mock.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=(
                '[{"type":"required_status_checks","parameters":{'
                '"strict_required_status_checks_policy":true,'
                '"required_status_checks":[{"context":"CI required"}]}},'
                '{"type":"required_status_checks","parameters":{'
                '"strict_required_status_checks_policy":true,'
                '"required_status_checks":[{"context":"Landing gate"}]}}]'
            ),
            stderr="",
        )
        self.assertEqual(landing_gate_enforcement("owner/repo", "main"), "required")

    @patch("scripts.check_pr_readiness.run")
    def test_landing_gate_enforcement_fails_closed_on_unreadable_ruleset(
        self, run_mock: Mock
    ) -> None:
        run_mock.return_value = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="gh: error (HTTP 500)"
        )
        self.assertEqual(landing_gate_enforcement("owner/repo", "main"), "unknown")


if __name__ == "__main__":
    unittest.main()
