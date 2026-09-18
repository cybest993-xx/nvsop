from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "retire_task.py"


class RetireTaskTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix="retire-task-")
        self.root = Path(self.temp_dir.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.remote = self.root / "origin.git"
        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir()
        self.gh_calls = self.root / "gh-calls"
        self.env = os.environ.copy()
        self.env.update(
            {
                "GIT_CONFIG_GLOBAL": str(self.root / "global"),
                "GIT_CONFIG_NOSYSTEM": "1",
                "HOME": str(self.root),
                "PATH": f"{self.bin_dir}{os.pathsep}{self.env['PATH']}",
            }
        )
        self._install_fake_gh()
        self.git("init", "--quiet", "-b", "main")
        self.git("config", "user.name", "Retire task test")
        self.git("config", "user.email", "retire-task@example.invalid")
        (self.repo / "base.txt").write_text("base\n", encoding="utf-8")
        self.git("add", "base.txt")
        self.git("commit", "--quiet", "-m", "base")
        self.base = self.git("rev-parse", "HEAD")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _install_fake_gh(self) -> None:
        fake_gh = self.bin_dir / "gh"
        fake_gh.write_text(
            "#!/bin/sh\n"
            "count=0\n"
            'if test -f "$GH_CALLS"; then count=$(cat "$GH_CALLS"); fi\n'
            "printf '%s\\n' $((count + 1)) > \"$GH_CALLS\"\n"
            "printf '%s\\n' \"$GH_PAYLOAD\"\n",
            encoding="utf-8",
        )
        fake_gh.chmod(0o755)

    def command(
        self,
        *arguments: str,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *arguments],
            cwd=cwd or self.repo,
            env=env or self.env,
            capture_output=True,
            text=True,
            check=False,
        )

    def git(self, *arguments: str, cwd: Path | None = None) -> str:
        result = self.command(*arguments, cwd=cwd)
        if result.returncode != 0:
            self.fail(result.stderr or result.stdout)
        return result.stdout.strip()

    def make_merged_task(self, branch: str = "agent/a/demo") -> tuple[str, str, Path]:
        task = self.root / "task"
        self.git("worktree", "add", "--quiet", "-b", branch, str(task))
        (task / "task.txt").write_text("task\n", encoding="utf-8")
        self.command("add", "task.txt", cwd=task)
        result = self.command("commit", "--quiet", "-m", "task", cwd=task)
        if result.returncode != 0:
            self.fail(result.stderr or result.stdout)
        candidate = self.git("rev-parse", f"refs/heads/{branch}")
        self.git("merge", "--quiet", "--squash", branch)
        self.git("commit", "--quiet", "-m", "squashed")
        merge_commit = self.git("rev-parse", "HEAD")
        self.git("init", "--quiet", "--bare", str(self.remote), cwd=self.root)
        self.git("remote", "add", "origin", str(self.remote))
        self.git("push", "--quiet", "origin", "main")
        return candidate, merge_commit, task

    def run_cli(
        self,
        branch: str,
        candidate: str,
        merge_commit: str,
        *,
        head_oid: str | None = None,
        cwd: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        payload = {
            "state": "MERGED",
            "headRefName": branch,
            "headRefOid": head_oid or candidate,
            "baseRefName": "main",
            "mergeCommit": {"oid": merge_commit},
        }
        env = self.env.copy()
        env.update(
            {
                "GH_CALLS": str(self.gh_calls),
                "GH_PAYLOAD": json.dumps(payload),
            }
        )
        return subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--pr",
                "311",
                "--branch",
                branch,
                "--candidate",
                candidate,
            ],
            cwd=cwd or self.repo,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def assert_branch_exists(self, branch: str) -> None:
        self.assertEqual(
            0,
            self.command("show-ref", "--verify", "--quiet", f"refs/heads/{branch}").returncode,
        )

    def assert_branch_absent(self, branch: str) -> None:
        self.assertNotEqual(
            0,
            self.command("show-ref", "--verify", "--quiet", f"refs/heads/{branch}").returncode,
        )

    def assert_gh_called_once(self) -> None:
        self.assertEqual("1\n", self.gh_calls.read_text(encoding="utf-8"))

    def test_clean_single_worktree_retires_only_the_requested_task(self) -> None:
        branch = "agent/a/demo"
        candidate, merge_commit, task = self.make_merged_task(branch)
        unrelated = "agent/a/unrelated"
        self.git("branch", unrelated, self.base)

        result = self.run_cli(branch, candidate, merge_commit)

        self.assertEqual(0, result.returncode, result.stderr)
        self.assert_branch_absent(branch)
        self.assertFalse(task.exists())
        self.assert_branch_exists(unrelated)
        self.assert_gh_called_once()

    def test_clean_task_without_worktree_is_retired(self) -> None:
        branch = "agent/a/demo"
        candidate, merge_commit, task = self.make_merged_task(branch)
        self.git("worktree", "remove", "--", str(task))

        result = self.run_cli(branch, candidate, merge_commit)

        self.assertEqual(0, result.returncode, result.stderr)
        self.assert_branch_absent(branch)
        self.assertFalse(task.exists())
        self.assert_gh_called_once()

    def test_reviewed_head_mismatch_preserves_branch_and_worktree(self) -> None:
        branch = "agent/a/demo"
        candidate, merge_commit, task = self.make_merged_task(branch)

        result = self.run_cli(branch, candidate, merge_commit, head_oid=self.base)

        self.assertNotEqual(0, result.returncode)
        self.assert_branch_exists(branch)
        self.assertTrue(task.exists())
        self.assert_gh_called_once()

    def test_multiple_worktrees_preserve_every_checkout(self) -> None:
        branch = "agent/a/demo"
        candidate, merge_commit, task = self.make_merged_task(branch)
        second = self.root / "second"
        self.git("worktree", "add", "--quiet", "--force", str(second), branch)

        result = self.run_cli(branch, candidate, merge_commit)

        self.assertNotEqual(0, result.returncode)
        self.assert_branch_exists(branch)
        self.assertTrue(task.exists())
        self.assertTrue(second.exists())
        self.assertFalse(self.gh_calls.exists())

    def test_dirty_worktree_is_preserved(self) -> None:
        branch = "agent/a/demo"
        candidate, merge_commit, task = self.make_merged_task(branch)
        (task / "untracked.txt").write_text("keep\n", encoding="utf-8")

        result = self.run_cli(branch, candidate, merge_commit)

        self.assertNotEqual(0, result.returncode)
        self.assert_branch_exists(branch)
        self.assertTrue((task / "untracked.txt").exists())
        self.assertFalse(self.gh_calls.exists())

    def test_ignored_worktree_is_preserved(self) -> None:
        branch = "agent/a/demo"
        candidate, merge_commit, task = self.make_merged_task(branch)
        (self.repo / ".git" / "info" / "exclude").write_text("ignored.txt\n", encoding="utf-8")
        (task / "ignored.txt").write_text("keep\n", encoding="utf-8")

        result = self.run_cli(branch, candidate, merge_commit)

        self.assertNotEqual(0, result.returncode)
        self.assert_branch_exists(branch)
        self.assertTrue((task / "ignored.txt").exists())
        self.assertFalse(self.gh_calls.exists())

    def test_symbolic_branch_ref_is_preserved(self) -> None:
        source = "agent/a/source"
        branch = "agent/a/symbolic"
        candidate, merge_commit, task = self.make_merged_task(source)
        self.git("worktree", "remove", "--", str(task))
        self.git("symbolic-ref", f"refs/heads/{branch}", f"refs/heads/{source}")

        result = self.run_cli(branch, candidate, merge_commit)

        self.assertNotEqual(0, result.returncode)
        self.assertEqual(f"refs/heads/{source}", self.git("symbolic-ref", f"refs/heads/{branch}"))
        self.assertFalse(self.gh_calls.exists())

    def test_out_of_scope_branch_is_rejected(self) -> None:
        result = self.run_cli("main", self.base, self.base)

        self.assertNotEqual(0, result.returncode)
        self.assertEqual(self.base, self.git("rev-parse", "refs/heads/main"))
        self.assertFalse(self.gh_calls.exists())

    def test_current_worktree_is_preserved(self) -> None:
        branch = "agent/a/demo"
        candidate, merge_commit, task = self.make_merged_task(branch)

        result = self.run_cli(branch, candidate, merge_commit, cwd=task)

        self.assertNotEqual(0, result.returncode)
        self.assert_branch_exists(branch)
        self.assertTrue(task.exists())
        self.assertFalse(self.gh_calls.exists())

    def test_primary_worktree_is_preserved(self) -> None:
        branch = "agent/a/demo"
        candidate, merge_commit, task = self.make_merged_task(branch)
        self.git("worktree", "remove", "--", str(task))
        self.git("switch", "--quiet", branch)
        other = self.root / "other"
        self.git("worktree", "add", "--quiet", "--detach", str(other), branch)

        result = self.run_cli(branch, candidate, merge_commit, cwd=other)

        self.assertNotEqual(0, result.returncode)
        self.assert_branch_exists(branch)
        self.assertFalse(self.gh_calls.exists())

    def test_same_named_tag_survives_branch_retirement(self) -> None:
        branch = "agent/a/demo"
        candidate, merge_commit, _task = self.make_merged_task(branch)
        self.git("tag", branch, self.base)

        result = self.run_cli(branch, candidate, merge_commit)

        self.assertEqual(0, result.returncode, result.stderr)
        self.assert_branch_absent(branch)
        self.assertEqual(self.base, self.git("rev-parse", f"refs/tags/{branch}"))

    def test_literal_config_removes_only_exact_branch_section(self) -> None:
        branch = "agent/a/foo+bar"
        candidate, merge_commit, _task = self.make_merged_task(branch)
        self.git("config", "--local", f"branch.{branch}.rebase", "true")
        self.git("config", "--local", f"branch.{branch}.extra.rebase", "false")

        result = self.run_cli(branch, candidate, merge_commit)

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(
            1,
            self.command("config", "--local", "--get", f"branch.{branch}.rebase").returncode,
        )
        self.assertEqual(
            "false\n",
            self.command("config", "--local", "--get", f"branch.{branch}.extra.rebase").stdout,
        )

    def test_global_only_config_is_not_removed(self) -> None:
        branch = "agent/a/demo"
        candidate, merge_commit, _task = self.make_merged_task(branch)
        self.git("config", "--global", f"branch.{branch}.rebase", "true")

        result = self.run_cli(branch, candidate, merge_commit)

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(
            1,
            self.command("config", "--local", "--get", f"branch.{branch}.rebase").returncode,
        )
        self.assertEqual(
            "true\n",
            self.command("config", "--global", "--get", f"branch.{branch}.rebase").stdout,
        )

    def test_target_worktree_git_read_failure_preserves_branch(self) -> None:
        branch = "agent/a/demo"
        candidate, merge_commit, task = self.make_merged_task(branch)
        (task / ".git").rename(task / ".git.moved")

        result = self.run_cli(branch, candidate, merge_commit)

        self.assertNotEqual(0, result.returncode)
        self.assert_branch_exists(branch)
        self.assertTrue(task.exists())
        self.assertFalse(self.gh_calls.exists())

    def test_repository_config_read_failure_preserves_branch(self) -> None:
        branch = "agent/a/demo"
        candidate, merge_commit, task = self.make_merged_task(branch)
        config = self.repo / ".git" / "config"
        original_config = config.read_bytes()
        config.write_bytes(b"[invalid\n")
        try:
            result = self.run_cli(branch, candidate, merge_commit)
        finally:
            config.write_bytes(original_config)

        self.assertNotEqual(0, result.returncode)
        self.assert_branch_exists(branch)
        self.assertTrue(task.exists())
        self.assertFalse(self.gh_calls.exists())


if __name__ == "__main__":
    unittest.main()
