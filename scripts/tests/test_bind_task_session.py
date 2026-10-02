from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "bind_task_session.py"
BINDING = Path(".nvsop") / "session-binding.json"


class BindTaskSessionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix="bind-session-")
        self.root = Path(self.temp_dir.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.env = os.environ.copy()
        self.env.update(
            {
                "GIT_CONFIG_GLOBAL": str(self.root / "global"),
                "GIT_CONFIG_NOSYSTEM": "1",
                "HOME": str(self.root),
            }
        )
        self.git("init", "--quiet", "-b", "main")
        self.git("config", "user.name", "Bind session test")
        self.git("config", "user.email", "bind-session@example.invalid")
        (self.repo / ".gitignore").write_text(".nvsop/\n", encoding="utf-8")
        (self.repo / "base.txt").write_text("base\n", encoding="utf-8")
        self.git("add", ".gitignore", "base.txt")
        self.git("commit", "--quiet", "-m", "base")
        self.branch = "agent/a/demo"
        self.worktree = self.root / "task"
        self.git("worktree", "add", "--quiet", "-b", self.branch, str(self.worktree))

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def command(self, *arguments: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *arguments],
            cwd=cwd or self.repo,
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
        )

    def git(self, *arguments: str, cwd: Path | None = None) -> str:
        result = self.command(*arguments, cwd=cwd)
        if result.returncode != 0:
            self.fail(result.stderr or result.stdout)
        return result.stdout.strip()

    def run_bind(self, session: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--session", session],
            cwd=cwd or self.worktree,
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
        )

    def binding_path(self, worktree: Path | None = None) -> Path:
        return (worktree or self.worktree) / BINDING

    def read_binding(self, worktree: Path | None = None) -> dict[str, object]:
        return json.loads(self.binding_path(worktree).read_text(encoding="utf-8"))

    def test_creates_exact_schema_with_restricted_mode(self) -> None:
        result = self.run_bind("session-a")

        self.assertEqual(0, result.returncode, result.stderr)
        binding = self.read_binding()
        self.assertEqual({"version", "session_id", "branch", "worktree"}, set(binding))
        self.assertEqual(1, binding["version"])
        self.assertEqual("session-a", binding["session_id"])
        self.assertEqual(self.branch, binding["branch"])
        self.assertEqual(os.path.realpath(self.worktree), binding["worktree"])
        mode = stat.S_IMODE(self.binding_path().stat().st_mode)
        self.assertEqual(0o600, mode)

    def test_same_session_is_idempotent(self) -> None:
        first = self.run_bind("session-a")
        content = self.binding_path().read_bytes()
        second = self.run_bind("session-a")

        self.assertEqual(0, first.returncode, first.stderr)
        self.assertEqual(0, second.returncode, second.stderr)
        self.assertEqual(content, self.binding_path().read_bytes())

    def test_other_session_is_refused(self) -> None:
        self.assertEqual(0, self.run_bind("session-a").returncode)

        result = self.run_bind("session-b")

        self.assertNotEqual(0, result.returncode)
        self.assertEqual("session-a", self.read_binding()["session_id"])

    def test_concurrent_first_binds_do_not_overwrite(self) -> None:
        sessions = ("session-a", "session-b")
        processes = [
            subprocess.Popen(
                [sys.executable, str(SCRIPT), "--session", session],
                cwd=self.worktree,
                env=self.env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for session in sessions
        ]
        for process in processes:
            process.communicate()
        codes = sorted(process.returncode for process in processes)

        self.assertEqual([0, 1], codes)
        self.assertIn(self.read_binding()["session_id"], sessions)

    def test_malformed_binding_is_rejected(self) -> None:
        directory = self.binding_path().parent
        directory.mkdir(parents=True)
        self.binding_path().write_text("{not json", encoding="utf-8")

        result = self.run_bind("session-a")

        self.assertNotEqual(0, result.returncode)

    def test_negative_schema_is_rejected(self) -> None:
        valid = {
            "version": 1,
            "session_id": "session-a",
            "branch": self.branch,
            "worktree": os.path.realpath(self.worktree),
        }
        cases = {
            "float-version": {**valid, "version": 1.0},
            "extra-key": {**valid, "extra": True},
            "missing-key": {key: value for key, value in valid.items() if key != "session_id"},
            "non-string-session": {**valid, "session_id": 7},
        }
        for name, payload in cases.items():
            with self.subTest(name=name):
                directory = self.binding_path().parent
                directory.mkdir(parents=True, exist_ok=True)
                self.binding_path().write_text(json.dumps(payload), encoding="utf-8")
                result = self.run_bind("session-a")
                self.assertNotEqual(0, result.returncode)

    def test_symlink_binding_is_rejected(self) -> None:
        directory = self.binding_path().parent
        directory.mkdir(parents=True)
        self.binding_path().symlink_to(self.repo / "base.txt")

        result = self.run_bind("session-a")

        self.assertNotEqual(0, result.returncode)

    def test_non_regular_binding_is_rejected(self) -> None:
        for name in ("directory", "fifo"):
            with self.subTest(name=name):
                directory = self.binding_path().parent
                if directory.exists():
                    shutil.rmtree(directory)
                directory.mkdir(parents=True)
                if name == "directory":
                    self.binding_path().mkdir()
                else:
                    os.mkfifo(self.binding_path())

                result = self.run_bind("session-a")

                self.assertNotEqual(0, result.returncode)
                shutil.rmtree(directory)

    def test_non_task_branch_is_rejected(self) -> None:
        other = self.root / "other"
        self.git("worktree", "add", "--quiet", "-b", "feature/x", str(other))

        result = self.run_bind("session-a", cwd=other)

        self.assertNotEqual(0, result.returncode)

    def test_detached_head_is_rejected(self) -> None:
        self.git("checkout", "--quiet", "--detach", cwd=self.worktree)

        result = self.run_bind("session-a")

        self.assertNotEqual(0, result.returncode)

    def test_untracked_binding_path_is_rejected(self) -> None:
        (self.worktree / ".gitignore").write_text("# no local state rule\n", encoding="utf-8")
        self.git("add", ".gitignore", cwd=self.worktree)
        self.git("commit", "--quiet", "-m", "drop ignore", cwd=self.worktree)

        result = self.run_bind("session-a")

        self.assertNotEqual(0, result.returncode)


if __name__ == "__main__":
    unittest.main()
