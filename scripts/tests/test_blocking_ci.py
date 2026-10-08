from __future__ import annotations

import os
import re
import subprocess
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/blocking-ci.yml"


def workflow_script(job: str, step: str) -> str:
    job_body = WORKFLOW.read_text().split(f"\n  {job}:\n", 1)[1]
    job_body = re.split(r"\n  (?=\S)", job_body, maxsplit=1)[0]
    step_body = job_body.split(f"      - name: {step}\n", 1)[1]
    step_body = step_body.split("\n      - name:", 1)[0]
    return textwrap.dedent(step_body.split("        run: |\n", 1)[1])


class BlockingCiTest(unittest.TestCase):
    def run_step(
        self, job: str, step: str, environment: dict[str, str]
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                "bash",
                "--noprofile",
                "--norc",
                "-e",
                "-o",
                "pipefail",
                "-c",
                workflow_script(job, step),
            ],
            cwd=ROOT,
            env=os.environ | environment,
            check=False,
            capture_output=True,
            text=True,
        )

    def test_required_status_accepts_only_success_from_every_dependency(self) -> None:
        environment = dict.fromkeys(
            (
                "SCOPE_RESULT",
                "LOCKFILE_RESULT",
                "GATE_RESULT",
                "INTEGRATION_RESULT",
                "BROWSER_RESULT",
            ),
            "success",
        )
        result = self.run_step("required", "Require successful dependencies", environment)
        self.assertEqual(0, result.returncode, result.stderr)
        for dependency in environment:
            for status in ("failure", "cancelled", "skipped", ""):
                with self.subTest(dependency=dependency, status=status):
                    result = self.run_step(
                        "required",
                        "Require successful dependencies",
                        environment | {dependency: status},
                    )
                    self.assertNotEqual(0, result.returncode, result.stdout)

    def test_scope_outputs_must_be_complete_and_consistent(self) -> None:
        for docs_only, force_all, integration, browser, media, annotation, succeeds in (
            ("true", "false", "false", "false", "false", "false", True),
            ("false", "true", "true", "true", "true", "true", True),
            ("false", "false", "false", "false", "false", "false", True),
            ("false", "false", "true", "false", "false", "false", True),
            ("false", "false", "false", "true", "false", "false", True),
            ("false", "false", "true", "true", "false", "false", True),
            ("false", "false", "true", "true", "true", "false", True),
            ("false", "false", "true", "false", "false", "true", True),
            ("true", "true", "true", "true", "true", "true", False),
            ("true", "false", "true", "false", "false", "false", False),
            ("false", "false", "false", "true", "true", "false", False),
            ("true", "false", "false", "false", "false", "true", False),
            ("false", "true", "true", "true", "true", "false", False),
            ("false", "false", "false", "false", "false", "true", False),
            ("false", "false", "true", "false", "false", "", False),
            ("false", "false", "true", "false", "false", "unknown", False),
            ("", "", "", "", "", "", False),
            ("unknown", "false", "false", "false", "false", "false", False),
        ):
            with self.subTest(
                scope=(docs_only, force_all, integration, browser, media, annotation)
            ):
                result = self.run_step(
                    "scope",
                    "Validate scope outputs",
                    {
                        "DOCS_ONLY": docs_only,
                        "FORCE_ALL": force_all,
                        "INTEGRATION": integration,
                        "BROWSER": browser,
                        "MEDIA": media,
                        "ANNOTATION": annotation,
                    },
                )
                self.assertEqual(succeeds, result.returncode == 0, result.stderr)

    def test_expensive_jobs_consume_the_shared_scope_without_second_path_filter(self) -> None:
        workflow = WORKFLOW.read_text()
        self.assertIn("needs.scope.outputs.integration == 'true'", workflow)
        self.assertIn("needs.scope.outputs.browser == 'true'", workflow)
        self.assertIn("needs.scope.outputs.media == 'true'", workflow)
        self.assertIn("needs.scope.outputs.annotation == 'true'", workflow)
        self.assertIn("run: make annotation-image", workflow)
        self.assertIn("run: make media-system", workflow)
        self.assertIn("run: make web-e2e-whep", workflow)
        self.assertNotIn("git diff --name-only --no-renames -z", workflow)
        self.assertNotIn("steps.relevant.outputs.run", workflow)

    def test_integration_matrix_runs_both_suites_and_media_only_once(self) -> None:
        workflow = WORKFLOW.read_text()
        integration = workflow.split("\n  integration-gate:\n", 1)[1].split(
            "\n  browser-gate:\n", 1
        )[0]
        self.assertIn("suite: [center-integration, center-system]", integration)
        self.assertIn("fail-fast: false", integration)
        self.assertIn('run: make sync "$TEST_SUITE"', integration)
        self.assertIn("TEST_SUITE: ${{ matrix.suite }}", integration)
        self.assertIn(
            "if: needs.scope.outputs.media == 'true' && matrix.suite == 'center-system'",
            integration,
        )
        self.assertIn(
            "if: needs.scope.outputs.annotation == 'true' && matrix.suite == 'center-system'",
            integration,
        )
        self.assertIn(
            "needs: [scope, lockfile, merge-gate, integration-gate, browser-gate]", workflow
        )

    def test_browser_failure_upload_is_pinned_and_limited_to_playwright_output(self) -> None:
        workflow = WORKFLOW.read_text()
        self.assertIn(
            "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02 # v4.6.2",
            workflow,
        )
        self.assertIn("path: .nvsop/artifacts/web/test-results/", workflow)
        self.assertIn("include-hidden-files: true", workflow)
        self.assertNotIn(".nvsop/dev-main", workflow)

    def test_blocking_ci_is_reusable_only_and_checks_exact_controller_head(self) -> None:
        workflow = WORKFLOW.read_text()
        # queue-only：候选只由可信 landing-queue 以显式 head/base 调用。
        self.assertNotIn("\n  pull_request:\n", workflow)
        self.assertNotIn("\n  push:\n", workflow)
        self.assertIn("workflow_call:", workflow)
        self.assertEqual(2, workflow.count("required: true"))
        self.assertIn("head_sha:", workflow)
        self.assertIn("base_sha:", workflow)
        head_expression = "ref: ${{ inputs.head_sha }}"
        self.assertEqual(workflow.count(head_expression), workflow.count("actions/checkout@"))
        self.assertIn("BASE_SHA: ${{ inputs.base_sha }}", workflow)
        self.assertIn("HEAD_SHA: ${{ inputs.head_sha }}", workflow)
        self.assertNotIn("github.event", workflow)
        self.assertNotIn("github.sha", workflow)
        # 候选代码只读检出，绝不携带凭据或 App secret。
        self.assertEqual(
            workflow.count("persist-credentials: false"),
            workflow.count("actions/checkout@"),
        )
        self.assertNotIn("secrets.", workflow)
        self.assertNotIn("LANDING_APP_PRIVATE_KEY", workflow)

    def test_blocking_ci_concurrency_does_not_share_the_queue_mutex(self) -> None:
        workflow = WORKFLOW.read_text()
        concurrency = workflow.split("\nconcurrency:\n", 1)[1].split("\njobs:\n", 1)[0]
        # workflow_call 以候选头为组，绝不与 landing-queue 的全局互斥组同名而自锁。
        self.assertIn("group: blocking-ci-", concurrency)
        self.assertNotIn("group: landing-queue", concurrency)
        self.assertIn("inputs.head_sha", concurrency)
        self.assertIn("cancel-in-progress: false", concurrency)


if __name__ == "__main__":
    unittest.main()
