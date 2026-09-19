"""用真实 Git 暂存区验证提交检查，不改调用者的索引。"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HOOK = ROOT / "scripts/githooks/pre-commit"


class PreCommitTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.git("init", "--quiet", "--initial-branch=agent/test/index")
        self.git("config", "user.name", "Index test")
        self.git("config", "user.email", "index@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        self.git("config", "core.hooksPath", str(self.root / "no-hooks"))
        binary = self.root / ".nvsop/venv/bin/ruff"
        binary.parent.mkdir(parents=True)
        binary.symlink_to(ROOT / ".nvsop/venv/bin/ruff")
        (self.root / ".gitignore").write_text(".nvsop/\n")
        self.git("add", ".gitignore")
        self.git("commit", "--quiet", "-m", "fixture")

    def git(self, *args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=self.root, check=True, capture_output=True, text=True
        ).stdout

    def hook(self) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment.pop("NVSOP_VENDOR_PATCH", None)
        return subprocess.run(
            [sys.executable, str(HOOK)],
            cwd=self.root,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_partial_staging_checks_index_bytes_and_preserves_worktree(self) -> None:
        source = self.root / "sample.py"
        for staged, unstaged, code in (("x=1\n", "x = 1\n", 1), ("x = 1\n", "x=1\n", 0)):
            with self.subTest(staged=staged):
                source.write_text(staged)
                self.git("add", "sample.py")
                source.write_text(unstaged)
                result = self.hook()
                self.assertEqual(code, result.returncode, result.stderr)
                self.assertEqual(staged, self.git("show", ":sample.py"))
                self.assertEqual(unstaged, source.read_text())

    def test_vendor_deletion_cannot_escape_the_patch_boundary(self) -> None:
        source = self.root / "vendor/upstream.py"
        source.parent.mkdir()
        source.write_text("x = 1\n")
        self.git("add", "vendor/upstream.py")
        self.git("commit", "--quiet", "-m", "fixture vendor")
        self.git("rm", "vendor/upstream.py")
        result = self.hook()
        self.assertEqual(1, result.returncode, result.stderr)
        self.assertIn("vendor", result.stderr)

    def test_vendor_rename_outside_vendor_cannot_escape_the_patch_boundary(self) -> None:
        source = self.root / "vendor/upstream.py"
        source.parent.mkdir()
        source.write_text("x = 1\n")
        self.git("add", "vendor/upstream.py")
        self.git("commit", "--quiet", "-m", "fixture vendor")
        (self.root / "apps").mkdir()
        self.git("mv", "vendor/upstream.py", "apps/upstream.py")
        result = self.hook()
        self.assertEqual(1, result.returncode, result.stderr)
        self.assertIn("vendor/upstream.py", result.stderr)

    def test_missing_formatter_reports_the_unperformed_check(self) -> None:
        (self.root / ".nvsop/venv/bin/ruff").unlink()
        (self.root / "sample.py").write_text("x = 1\n")
        self.git("add", "sample.py")
        result = self.hook()
        self.assertIn("make sync", result.stderr)
        self.assertEqual(1, result.returncode)
