from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "cleanup_task.py"


class CleanupTaskTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix="cleanup-task-")
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
        self.git("config", "user.name", "Cleanup task test")
        self.git("config", "user.email", "cleanup-task@example.invalid")
        (self.repo / ".gitignore").write_text(".nvsop/\n", encoding="utf-8")
        (self.repo / "base.txt").write_text("base\n", encoding="utf-8")
        self.git("add", ".gitignore", "base.txt")
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

    def make_abandoned_task(self, branch: str = "agent/a/demo") -> tuple[str, str, Path]:
        task = self.root / "task"
        self.git("worktree", "add", "--quiet", "-b", branch, str(task))
        (task / "task.txt").write_text("task\n", encoding="utf-8")
        self.command("add", "task.txt", cwd=task)
        result = self.command("commit", "--quiet", "-m", "task", cwd=task)
        if result.returncode != 0:
            self.fail(result.stderr or result.stdout)
        candidate = self.git("rev-parse", f"refs/heads/{branch}")
        self.git("init", "--quiet", "--bare", str(self.remote), cwd=self.root)
        self.git("remote", "add", "origin", str(self.remote))
        self.git("push", "--quiet", "origin", "main")
        return candidate, self.base, task

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

    def run_abandon_cli(
        self,
        branch: str,
        candidate: str,
        replaced_by: str,
        *,
        pr_state: str | None = None,
        head_oid: str | None = None,
        merge_commit: str | None = None,
        cwd: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        env = self.env.copy()
        arguments = [
            sys.executable,
            str(SCRIPT),
            "--branch",
            branch,
            "--candidate",
            candidate,
            "--replaced-by",
            replaced_by,
        ]
        if pr_state is not None:
            payload = {
                "state": pr_state,
                "headRefName": branch,
                "headRefOid": head_oid or candidate,
                "baseRefName": "main",
                "mergeCommit": {"oid": merge_commit} if merge_commit else None,
            }
            env.update(
                {
                    "GH_CALLS": str(self.gh_calls),
                    "GH_PAYLOAD": json.dumps(payload),
                }
            )
            arguments[2:2] = ["--pr", "312"]
        return subprocess.run(
            arguments,
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

    def test_clean_single_worktree_removes_only_the_requested_task(self) -> None:
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

    def test_merged_cleanup_fast_forwards_primary_main(self) -> None:
        branch = "agent/a/demo"
        candidate, merge_commit, task = self.make_merged_task(branch)
        self.git("reset", "--quiet", "--hard", self.base)

        result = self.run_cli(branch, candidate, merge_commit)

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(merge_commit, self.git("rev-parse", "HEAD"))
        self.assert_branch_absent(branch)
        self.assertFalse(task.exists())

    def test_dirty_primary_main_preserves_merged_task(self) -> None:
        branch = "agent/a/demo"
        candidate, merge_commit, task = self.make_merged_task(branch)
        self.git("reset", "--quiet", "--hard", self.base)
        (self.repo / "local.txt").write_text("keep\n", encoding="utf-8")

        result = self.run_cli(branch, candidate, merge_commit)

        self.assertNotEqual(0, result.returncode)
        self.assertEqual(self.base, self.git("rev-parse", "HEAD"))
        self.assert_branch_exists(branch)
        self.assertTrue(task.exists())

    def test_clean_task_without_worktree_removes_branch(self) -> None:
        branch = "agent/a/demo"
        candidate, merge_commit, task = self.make_merged_task(branch)
        self.git("worktree", "remove", "--", str(task))

        result = self.run_cli(branch, candidate, merge_commit)

        self.assertEqual(0, result.returncode, result.stderr)
        self.assert_branch_absent(branch)
        self.assertFalse(task.exists())
        self.assert_gh_called_once()

    def test_abandon_without_pr_removes_exact_clean_task(self) -> None:
        branch = "agent/a/demo"
        candidate, replaced_by, task = self.make_abandoned_task(branch)
        self.write_binding(task, branch)

        result = self.run_abandon_cli(branch, candidate, replaced_by)

        self.assertEqual(0, result.returncode, result.stderr)
        self.assert_branch_absent(branch)
        self.assertFalse(task.exists())
        self.assertFalse(self.gh_calls.exists())

    def test_abandon_with_closed_pr_requires_exact_head(self) -> None:
        branch = "agent/a/demo"
        candidate, replaced_by, task = self.make_abandoned_task(branch)

        result = self.run_abandon_cli(branch, candidate, replaced_by, pr_state="CLOSED")

        self.assertEqual(0, result.returncode, result.stderr)
        self.assert_branch_absent(branch)
        self.assertFalse(task.exists())
        self.assert_gh_called_once()

    def test_abandon_rejects_open_pr(self) -> None:
        branch = "agent/a/demo"
        candidate, replaced_by, task = self.make_abandoned_task(branch)

        result = self.run_abandon_cli(branch, candidate, replaced_by, pr_state="OPEN")

        self.assertNotEqual(0, result.returncode)
        self.assert_branch_exists(branch)
        self.assertTrue(task.exists())
        self.assert_gh_called_once()

    def test_abandon_rejects_merged_pr(self) -> None:
        branch = "agent/a/demo"
        candidate, replaced_by, task = self.make_abandoned_task(branch)

        result = self.run_abandon_cli(
            branch,
            candidate,
            replaced_by,
            pr_state="MERGED",
            merge_commit=self.base,
        )

        self.assertNotEqual(0, result.returncode)
        self.assert_branch_exists(branch)
        self.assertTrue(task.exists())
        self.assert_gh_called_once()

    def test_abandon_rejects_closed_pr_head_mismatch(self) -> None:
        branch = "agent/a/demo"
        candidate, replaced_by, task = self.make_abandoned_task(branch)

        result = self.run_abandon_cli(
            branch, candidate, replaced_by, pr_state="CLOSED", head_oid=self.base
        )

        self.assertNotEqual(0, result.returncode)
        self.assert_branch_exists(branch)
        self.assertTrue(task.exists())
        self.assert_gh_called_once()

    def test_abandon_replacement_not_retained_by_main_preserves_task(self) -> None:
        branch = "agent/a/demo"
        candidate, _replaced_by, task = self.make_abandoned_task(branch)

        result = self.run_abandon_cli(branch, candidate, candidate)

        self.assertNotEqual(0, result.returncode)
        self.assert_branch_exists(branch)
        self.assertTrue(task.exists())
        self.assertFalse(self.gh_calls.exists())

    def test_abandon_dirty_worktree_is_preserved(self) -> None:
        branch = "agent/a/demo"
        candidate, replaced_by, task = self.make_abandoned_task(branch)
        (task / "untracked.txt").write_text("keep\n", encoding="utf-8")

        result = self.run_abandon_cli(branch, candidate, replaced_by)

        self.assertNotEqual(0, result.returncode)
        self.assert_branch_exists(branch)
        self.assertTrue((task / "untracked.txt").exists())
        self.assertFalse(self.gh_calls.exists())

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

    def test_same_named_tag_survives_branch_cleanup(self) -> None:
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

    def write_binding(
        self, target: Path, branch: str, *, session: str = "session-a", **overrides: object
    ) -> Path:
        directory = target / ".nvsop"
        directory.mkdir(parents=True, exist_ok=True)
        binding: dict[str, object] = {
            "version": 1,
            "session_id": session,
            "branch": branch,
            "worktree": os.path.realpath(target),
        }
        binding.update(overrides)
        path = directory / "session-binding.json"
        path.write_text(json.dumps(binding, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        return path

    def test_clean_accepts_validated_binding(self) -> None:
        branch = "agent/a/demo"
        candidate, merge_commit, task = self.make_merged_task(branch)
        self.write_binding(task, branch)

        result = self.run_cli(branch, candidate, merge_commit)

        self.assertEqual(0, result.returncode, result.stderr)
        self.assert_branch_absent(branch)
        self.assertFalse(task.exists())
        self.assert_gh_called_once()

    def test_malformed_binding_is_preserved(self) -> None:
        branch = "agent/a/demo"
        candidate, merge_commit, task = self.make_merged_task(branch)
        self.write_binding(task, branch)
        (task / ".nvsop" / "session-binding.json").write_text("{not json", encoding="utf-8")

        result = self.run_cli(branch, candidate, merge_commit)

        self.assertNotEqual(0, result.returncode)
        self.assert_branch_exists(branch)
        self.assertTrue(task.exists())
        self.assertFalse(self.gh_calls.exists())

    def test_mismatched_binding_branch_is_preserved(self) -> None:
        branch = "agent/a/demo"
        candidate, merge_commit, task = self.make_merged_task(branch)
        self.write_binding(task, "agent/a/other")

        result = self.run_cli(branch, candidate, merge_commit)

        self.assertNotEqual(0, result.returncode)
        self.assert_branch_exists(branch)
        self.assertTrue(task.exists())
        self.assertFalse(self.gh_calls.exists())

    def test_mismatched_binding_worktree_is_preserved(self) -> None:
        branch = "agent/a/demo"
        candidate, merge_commit, task = self.make_merged_task(branch)
        self.write_binding(task, branch, worktree=str(self.root / "elsewhere"))

        result = self.run_cli(branch, candidate, merge_commit)

        self.assertNotEqual(0, result.returncode)
        self.assert_branch_exists(branch)
        self.assertTrue(task.exists())
        self.assertFalse(self.gh_calls.exists())

    def test_binding_with_untracked_dirt_is_preserved(self) -> None:
        branch = "agent/a/demo"
        candidate, merge_commit, task = self.make_merged_task(branch)
        self.write_binding(task, branch)
        (task / "untracked.txt").write_text("keep\n", encoding="utf-8")

        result = self.run_cli(branch, candidate, merge_commit)

        self.assertNotEqual(0, result.returncode)
        self.assert_branch_exists(branch)
        self.assertTrue(task.exists())
        self.assertFalse(self.gh_calls.exists())

    def test_binding_with_other_ignored_state_is_preserved(self) -> None:
        branch = "agent/a/demo"
        candidate, merge_commit, task = self.make_merged_task(branch)
        self.write_binding(task, branch)
        (self.repo / ".git" / "info" / "exclude").write_text("other.txt\n", encoding="utf-8")
        (task / "other.txt").write_text("keep\n", encoding="utf-8")

        result = self.run_cli(branch, candidate, merge_commit)

        self.assertNotEqual(0, result.returncode)
        self.assert_branch_exists(branch)
        self.assertTrue(task.exists())
        self.assertFalse(self.gh_calls.exists())

    def test_empty_unknown_ignored_directory_is_preserved(self) -> None:
        branch = "agent/a/demo"
        candidate, merge_commit, task = self.make_merged_task(branch)
        self.write_binding(task, branch)
        (task / ".nvsop" / "unknown").mkdir()

        result = self.run_cli(branch, candidate, merge_commit)

        self.assertNotEqual(0, result.returncode)
        self.assert_branch_exists(branch)
        self.assertTrue(task.exists())
        self.assertFalse(self.gh_calls.exists())

    def test_empty_unknown_nvsop_root_is_preserved(self) -> None:
        branch = "agent/a/demo"
        candidate, merge_commit, task = self.make_merged_task(branch)
        (task / ".nvsop").mkdir()

        result = self.run_cli(branch, candidate, merge_commit)

        self.assertNotEqual(0, result.returncode)
        self.assert_branch_exists(branch)
        self.assertTrue(task.exists())
        self.assertFalse(self.gh_calls.exists())


if __name__ == "__main__":
    unittest.main()
