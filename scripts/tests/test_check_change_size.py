from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "check_change_size.py"


class ChangeSizeReportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.git("init", "--quiet", "--initial-branch=main")
        self.git("config", "user.name", "Repository policy test")
        self.git("config", "user.email", "policy-test@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        self.git("config", "core.hooksPath", str(self.root / "no-hooks"))
        self.git("commit", "--quiet", "--allow-empty", "-m", "base")
        self.base = self.git("rev-parse", "HEAD").strip()

    def git(self, *args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=self.root, check=True, capture_output=True, text=True
        ).stdout

    def report(self, *refs: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), *refs],
            cwd=self.root,
            capture_output=True,
            text=True,
            check=False,
        )

    def write(self, name: str, content: str) -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return path

    def test_reports_a_large_committed_change_without_blocking_delivery(self) -> None:
        source = self.root / "apps/control-api/src/factory_sop/auth/sessions.py"
        source.parent.mkdir(parents=True)
        source.write_text("session = None\n" * 1_000)
        self.git("add", ".")
        self.git("commit", "--quiet", "-m", "one cohesive change")
        source.write_text("uncommitted = True\n")

        result = self.report(self.base, "HEAD")

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("Change size report (advisory)", result.stdout)
        self.assertIn("Implementation: +1000 -0 (1000 changed lines)", result.stdout)
        self.assertIn("auth/sessions.py: 1000 physical lines", result.stdout)
        self.assertIn("cohesion", result.stdout)

    def test_local_report_includes_the_whole_task_from_its_merge_base(self) -> None:
        source = self.write("apps/control-api/src/factory_sop/auth/source.py", "one\ntwo\nthree\n")
        self.git("add", ".")
        self.git("commit", "--quiet", "-m", "shared base")
        self.git("branch", "feature")
        self.write("apps/control-api/src/factory_sop/device/unrelated.py", "other = 1\n" * 900)
        self.git("add", ".")
        self.git("commit", "--quiet", "-m", "main advances independently")
        advanced_main = self.git("rev-parse", "HEAD").strip()
        self.git("switch", "--quiet", "feature")
        self.write("apps/control-api/src/factory_sop/auth/committed.py", "committed = 1\n")
        self.git("add", ".")
        self.git("commit", "--quiet", "-m", "first task change")
        source.write_text("one\nfour\n")
        self.git("add", ".")
        source.write_text("one\nfour\nfive\n")
        self.write("apps/control-web/src/session/new.ts", "first\nlast")

        result = self.report(advanced_main)

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("Implementation: +5 -2 (7 changed lines)", result.stdout)
        self.assertIn("staged, unstaged and untracked", result.stdout)
        self.assertNotIn("device", result.stdout)

        committed = self.report(advanced_main, "HEAD")
        self.assertEqual(0, committed.returncode, committed.stderr)
        self.assertIn("Implementation: +1 -0 (1 changed lines)", committed.stdout)

    def test_separates_review_evidence_from_implementation_and_keeps_adapter_ownership(
        self,
    ) -> None:
        files = {
            "apps/control-api/src/factory_sop/auth/sessions.py": 500,
            "apps/control-api/src/factory_sop/auth/adapters/routes.py": 12,
            "apps/edge-runtime/src/edge_runtime/judgment/core.py": 500,
            "apps/control-web/src/modules/devices/Devices.vue": 800,
            "apps/control-web/src/modules/devices/Devices.spec.ts": 9,
            "scripts/tests/test_example.py": 7,
            "apps/control-web/src/api/generated/sdk.gen.ts": 1_000,
            "packages/contracts/openapi.json": 1,
            "apps/control-api/migrations/versions/0001_auth.py": 12,
            "vendor/sop-monitoring-blueprints/source.py": 2,
            "docs/example.md": 23,
            "uv.lock": 1,
        }
        for name, lines in files.items():
            self.write(name, "fixture\n" * lines)
        self.write("docs/image.bin", "binary\0fixture")

        result = self.report(self.base)

        self.assertEqual(0, result.returncode, result.stderr)
        for summary in (
            "Implementation: +1812 -0 (1812 changed lines)",
            "factory_sop/auth: +512 -0",
            "Tests: +16 -0",
            "Generated: +1001 -0",
            "Migrations: +12 -0",
            "Vendor: +2 -0",
            "Other: +24 -0",
            "Binary files: 1 (no line count)",
            "Judgment: 500 changed lines",
            "auth/sessions.py: 500 physical lines",
            "Devices.vue: 800 physical lines",
        ):
            self.assertIn(summary, result.stdout)

    def test_counts_deletions_and_follows_a_rename_without_charging_for_moved_lines(self) -> None:
        self.git("config", "diff.renames", "false")
        old = self.write("apps/control-api/src/factory_sop/auth/old name.py", "# reason\n\n" * 250)
        removed = self.write("apps/control-api/src/factory_sop/auth/removed.py", "old = 1\n" * 10)
        self.git("add", ".")
        self.git("commit", "--quiet", "-m", "existing files")
        base = self.git("rev-parse", "HEAD").strip()
        new = old.with_name("新\tname.py")
        self.git("mv", str(old), str(new))
        removed.unlink()

        result = self.report(base)

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("Implementation: +0 -10 (10 changed lines)", result.stdout)
        self.assertIn("新\tname.py: 500 physical lines", result.stdout)
        self.assertNotIn("removed.py: 10 physical lines", result.stdout)

    def test_invalid_revisions_and_usage_fail_instead_of_producing_a_success_report(self) -> None:
        for refs, status in (
            ((), 2),
            (("missing-base", "HEAD"), 1),
            ((self.base, "missing-head"), 1),
        ):
            with self.subTest(refs=refs):
                result = self.report(*refs)
                self.assertEqual(status, result.returncode)
                self.assertTrue(result.stderr)
                self.assertNotIn("Change size report (advisory)", result.stdout)


if __name__ == "__main__":
    unittest.main()
