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

    def install_prettier(self, script: str) -> None:
        binary = self.root / "apps/control-web/node_modules/.bin/prettier"
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_text(script)
        binary.chmod(0o755)

    def stage_web(self, content: str) -> None:
        source = self.root / "apps/control-web/src/sample.ts"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(content)
        self.git("add", "apps/control-web/src/sample.ts")

    def stage_web_unstaged(self, content: str) -> None:
        (self.root / "apps/control-web/src/sample.ts").write_text(content)

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

    def test_staged_web_source_is_checked_with_the_worktree_prettier(self) -> None:
        self.install_prettier(
            "#!/bin/sh\n"
            "if grep -q 'const a=1' -; then\n"
            "  echo '(stdin)' >&2\n"
            "  exit 1\n"
            "fi\n"
            "exit 0\n"
        )
        self.stage_web("const a=1\n")
        rejected = self.hook()
        self.assertEqual(1, rejected.returncode, rejected.stderr)
        self.assertIn("apps/control-web/src/sample.ts", rejected.stderr)
        self.assertIn("control-web run format:write", rejected.stderr)

        self.stage_web("const a = 1\n")
        accepted = self.hook()
        self.assertEqual(0, accepted.returncode, accepted.stderr)

    def test_partially_staged_web_source_checks_index_bytes(self) -> None:
        self.install_prettier("#!/bin/sh\nif grep -q 'const a=1' -; then\n  exit 1\nfi\nexit 0\n")
        self.stage_web("const a=1\n")
        self.stage_web_unstaged("const a = 1\n")
        result = self.hook()
        self.assertEqual(1, result.returncode, result.stderr)
        self.assertIn("apps/control-web/src/sample.ts", result.stderr)

    def test_missing_web_dependencies_report_the_unperformed_check(self) -> None:
        self.stage_web("const a=1\n")
        result = self.hook()
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("make web-install", result.stderr)
        self.assertIn("not checked", result.stderr)

    def test_prettier_that_cannot_run_does_not_block_the_commit(self) -> None:
        self.install_prettier("#!/bin/sh\necho 'node: not found' >&2\nexit 127\n")
        self.stage_web("const a=1\n")
        result = self.hook()
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("not checked", result.stderr)
