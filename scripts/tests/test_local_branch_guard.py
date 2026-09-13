from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HOOKS = ROOT / "scripts" / "githooks"


class LocalBranchGuardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name) / "repo"
        self.root.mkdir()
        self.git("init", "--initial-branch=main")
        self.git("config", "user.email", "guard-test@example.invalid")
        self.git("config", "user.name", "Local branch guard test")
        (self.root / "README.md").write_text("base\n", encoding="utf-8")
        self.git("add", "README.md")
        self.git("commit", "--quiet", "-m", "base")
        self.initial = self.git("rev-parse", "HEAD")
        self.git("config", "core.hooksPath", str(HOOKS))
        self.git("update-ref", "refs/remotes/origin/main", self.initial)
        self.git("update-ref", "refs/remotes/origin/dev", self.initial)
        self.git("branch", "dev", self.initial)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def command(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *arguments], cwd=self.root, capture_output=True, text=True, check=False
        )

    def git(self, *arguments: str) -> str:
        result = self.command(*arguments)
        if result.returncode != 0:
            self.fail(result.stderr or result.stdout)
        return result.stdout.strip()

    def make_agent_commit(self) -> str:
        self.git("switch", "--quiet", "-c", "agent/a/first", "dev")
        (self.root / "agent.txt").write_text("agent\n", encoding="utf-8")
        self.git("add", "agent.txt")
        self.git("commit", "--quiet", "-m", "agent work")
        tip = self.git("rev-parse", "HEAD")
        self.git("switch", "--quiet", "dev")
        self.git("merge", "--quiet", "--no-ff", "--no-edit", "agent/a/first")
        return tip

    def test_main_only_promotes_the_current_dev_tip(self) -> None:
        self.make_agent_commit()
        self.git("switch", "--quiet", "main")
        fast_forward = self.command("merge", "--ff-only", "dev")
        self.assertEqual(0, fast_forward.returncode, fast_forward.stderr)
        self.assertNotEqual(self.initial, self.git("rev-parse", "main"))

    def test_remote_main_can_overwrite_local_main(self) -> None:
        self.make_agent_commit()
        self.git("switch", "--quiet", "main")
        self.git("merge", "--quiet", "--no-ff", "--no-edit", "dev")

        self.git("reset", "--quiet", "--hard", "origin/main")
        self.assertEqual(self.initial, self.git("rev-parse", "main"))

    def test_direct_main_updates_are_rejected_even_with_no_verify(self) -> None:
        self.make_agent_commit()
        self.git("switch", "--quiet", "main")
        (self.root / "direct.txt").write_text("blocked\n", encoding="utf-8")
        self.git("add", "direct.txt")
        commit = self.command("commit", "--no-verify", "-m", "direct main commit")
        self.assertNotEqual(0, commit.returncode)
        self.assertIn("local `main` may change only", commit.stderr)
        self.assertEqual(self.initial, self.git("rev-parse", "main"))

        reset = self.command("reset", "--hard", "agent/a/first")
        self.assertNotEqual(0, reset.returncode)
        self.assertEqual(self.initial, self.git("rev-parse", "main"))

    def test_main_and_dev_cannot_be_deleted_or_renamed(self) -> None:
        rename_main = self.command("branch", "-m", "main", "renamed")
        self.assertNotEqual(0, rename_main.returncode)
        self.assertIn("`main` is permanent", rename_main.stderr)
        self.assertEqual("main", self.git("branch", "--show-current"))

        self.git("switch", "--quiet", "dev")
        delete_main = self.command("branch", "-D", "main")
        self.assertNotEqual(0, delete_main.returncode)
        self.assertIn("`main` is permanent", delete_main.stderr)

        rename_dev = self.command("branch", "-m", "dev", "renamed-dev")
        self.assertNotEqual(0, rename_dev.returncode)
        self.assertIn("`dev` is the integration branch", rename_dev.stderr)

    def test_direct_dev_commit_is_rejected(self) -> None:
        self.git("switch", "--quiet", "dev")
        (self.root / "direct-dev.txt").write_text("blocked\n", encoding="utf-8")
        self.git("add", "direct-dev.txt")
        commit = self.command("commit", "--no-verify", "-m", "direct dev commit")
        self.assertNotEqual(0, commit.returncode)
        self.assertIn("local `dev` may change only", commit.stderr)

    def test_new_local_branches_use_agent_namespace(self) -> None:
        valid = self.command("branch", "agent/a/second", "main")
        self.assertEqual(0, valid.returncode, valid.stderr)

        invalid = self.command("branch", "feature/second", "main")
        self.assertNotEqual(0, invalid.returncode)
        self.assertIn("new local branch `feature/second` is not allowed", invalid.stderr)


if __name__ == "__main__":
    unittest.main()
