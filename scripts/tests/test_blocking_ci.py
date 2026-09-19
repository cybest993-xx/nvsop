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
        for docs_only, force_all, integration, browser, media, succeeds in (
            ("true", "false", "false", "false", "false", True),
            ("false", "true", "true", "true", "true", True),
            ("false", "false", "false", "false", "false", True),
            ("false", "false", "true", "false", "false", True),
            ("false", "false", "false", "true", "false", True),
            ("false", "false", "true", "true", "false", True),
            ("false", "false", "true", "true", "true", True),
            ("true", "true", "true", "true", "true", False),
            ("true", "false", "true", "false", "false", False),
            ("false", "false", "false", "true", "true", False),
            ("", "", "", "", "", False),
            ("unknown", "false", "false", "false", "false", False),
        ):
            with self.subTest(scope=(docs_only, force_all, integration, browser, media)):
                result = self.run_step(
                    "scope",
                    "Validate scope outputs",
                    {
                        "DOCS_ONLY": docs_only,
                        "FORCE_ALL": force_all,
                        "INTEGRATION": integration,
                        "BROWSER": browser,
                        "MEDIA": media,
                    },
                )
                self.assertEqual(succeeds, result.returncode == 0, result.stderr)

    def test_expensive_jobs_consume_the_shared_scope_without_second_path_filter(self) -> None:
        workflow = WORKFLOW.read_text()
        self.assertIn("needs.scope.outputs.integration == 'true'", workflow)
        self.assertIn("needs.scope.outputs.browser == 'true'", workflow)
        self.assertIn("needs.scope.outputs.media == 'true'", workflow)
        self.assertIn("run: make media-system", workflow)
        self.assertIn("run: make web-e2e-whep", workflow)
        self.assertNotIn("git diff --name-only --no-renames -z", workflow)
        self.assertNotIn("steps.relevant.outputs.run", workflow)

    def test_browser_failure_upload_is_pinned_and_limited_to_playwright_output(self) -> None:
        workflow = WORKFLOW.read_text()
        self.assertIn(
            "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02 # v4.6.2",
            workflow,
        )
        self.assertIn("path: .nvsop/artifacts/web/test-results/", workflow)
        self.assertNotIn(".nvsop/dev-main", workflow)


if __name__ == "__main__":
    unittest.main()
