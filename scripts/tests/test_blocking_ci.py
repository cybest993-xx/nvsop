from __future__ import annotations

import os
import re
import subprocess
import tempfile
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
        for docs_only, force_all, succeeds in (
            ("true", "false", True),
            ("false", "true", True),
            ("false", "false", True),
            ("true", "true", False),
            ("", "", False),
            ("unknown", "false", False),
        ):
            with self.subTest(scope=(docs_only, force_all)):
                result = self.run_step(
                    "scope",
                    "Validate scope outputs",
                    {"DOCS_ONLY": docs_only, "FORCE_ALL": force_all},
                )
                self.assertEqual(succeeds, result.returncode == 0, result.stderr)

    def test_suite_filters_honor_shared_scope_and_propagate_git_errors(self) -> None:
        steps = {
            "integration-gate": "Decide whether the suite has anything to check",
            "browser-gate": "Decide whether the browser suite has anything to check",
        }
        for job, step in steps.items():
            for docs_only, force_all, base, head, expected in (
                ("true", "false", "unused-base", "unused-head", "run=false\n"),
                ("false", "true", "HEAD", "HEAD", "run=true\n"),
                ("false", "false", "HEAD", "HEAD", "run=false\n"),
                ("false", "false", "HEAD", "missing-head", ""),
            ):
                with (
                    self.subTest(job=job, scope=(docs_only, force_all), head=head),
                    tempfile.TemporaryDirectory() as temporary,
                ):
                    output = Path(temporary) / "output"
                    output.touch()
                    result = self.run_step(
                        job,
                        step,
                        {
                            "DOCS_ONLY": docs_only,
                            "FORCE_ALL": force_all,
                            "BASE_SHA": base,
                            "HEAD_SHA": head,
                            "GITHUB_OUTPUT": str(output),
                            "RUNNER_TEMP": temporary,
                        },
                    )
                    self.assertEqual(expected, output.read_text())
                    if head == "missing-head":
                        self.assertNotEqual(0, result.returncode)
                    else:
                        self.assertEqual(0, result.returncode, result.stderr)


if __name__ == "__main__":
    unittest.main()
