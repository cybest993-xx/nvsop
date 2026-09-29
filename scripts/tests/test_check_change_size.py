from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "check_change_size.py"
REPO_ROOT = Path(__file__).resolve().parents[2]
TRUSTED_MAKEFILE = REPO_ROOT / "Makefile"
TRUSTED_CHECKER = REPO_ROOT / "scripts" / "check_change_size.py"


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

    def check(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--check", *args],
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

    def test_check_passes_within_authorized_scope_and_budget(self) -> None:
        self.write("scripts/tool.py", "line\n" * 10)
        self.write("docs/engineering/coding.md", "text\n" * 5)

        result = self.check(
            "--allow",
            "scripts/",
            "--allow",
            "docs/engineering/coding.md",
            "--max-lines",
            "800",
            self.base,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("task_check=within-bounds", result.stdout)
        self.assertIn("not acceptance or approval", result.stdout)

    def test_check_counts_test_lines_and_does_not_reset_across_commits(self) -> None:
        test_file = self.write("scripts/tests/test_budget.py", "case\n" * 40)
        self.git("add", ".")
        self.git("commit", "--quiet", "-m", "first commit")
        test_file.write_text("case\n" * 80)
        self.git("add", ".")
        self.git("commit", "--quiet", "-m", "second commit")

        result = self.check("--allow", "scripts/", "--max-lines", "60", self.base)

        self.assertEqual(3, result.returncode, result.stdout + result.stderr)
        self.assertIn("task_check=pause", result.stdout)
        self.assertIn("budget exceeded: 80 > 60", result.stdout)

    def test_check_counts_deletions_toward_budget(self) -> None:
        self.write("scripts/legacy.py", "old\n" * 10)
        self.git("add", ".")
        self.git("commit", "--quiet", "-m", "legacy file")
        base = self.git("rev-parse", "HEAD").strip()
        (self.root / "scripts/legacy.py").unlink()

        result = self.check("--allow", "scripts/", "--max-lines", "5", base)

        self.assertEqual(3, result.returncode, result.stdout + result.stderr)
        self.assertIn("budget exceeded: 10 > 5", result.stdout)

    def test_check_keeps_the_deletion_when_a_staged_path_is_rebuilt_as_untracked(self) -> None:
        # 同一路径先暂存删除、再以未跟踪新文件重建时，两段变更都要计入预算。
        source = self.write("scripts/source.py", "old\n" * 10)
        self.git("add", ".")
        self.git("commit", "--quiet", "-m", "old source")
        base = self.git("rev-parse", "HEAD").strip()
        self.git("rm", "--cached", "--quiet", "scripts/source.py")
        source.write_text("brand-new-A\nbrand-new-B\n")

        result = self.check("--allow", "scripts/", "--max-lines", "2", base)

        self.assertEqual(3, result.returncode, result.stdout + result.stderr)
        self.assertIn("task_check=pause", result.stdout)
        self.assertIn("budget exceeded: 12 > 2", result.stdout)

    def test_check_excludes_generated_output_but_counts_its_source(self) -> None:
        self.write("apps/control-web/src/api/generated/sdk.gen.ts", "line\n" * 200)
        self.write("scripts/export.py", "line\n" * 3)

        result = self.check(
            "--allow",
            "apps/control-web/src/api/generated/",
            "--allow",
            "scripts/",
            "--max-lines",
            "10",
            self.base,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("Budget: 3 / 10", result.stdout)
        self.assertIn("Generated files excluded from budget: 1", result.stdout)

    def test_check_reports_binary_files_without_spending_line_budget(self) -> None:
        self.write("scripts/blob.bin", "binary\0data")

        result = self.check("--allow", "scripts/", "--max-lines", "1", self.base)

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("task_check=within-bounds", result.stdout)
        self.assertIn("Binary files (1)", result.stdout)

    def test_check_pauses_on_untracked_and_similar_prefix_paths(self) -> None:
        self.write("scripts/inside.py", "ok = 1\n")
        self.write("scripts-old/outside.py", "no = 1\n")

        result = self.check("--allow", "scripts/", "--max-lines", "800", self.base)

        self.assertEqual(3, result.returncode, result.stdout + result.stderr)
        self.assertIn("task_check=pause", result.stdout)
        self.assertIn("Out-of-scope paths (1): scripts-old/outside.py", result.stdout)

    def test_check_checks_both_rename_paths(self) -> None:
        old = self.write("apps/x/old.py", "content\n")
        self.git("add", ".")
        self.git("commit", "--quiet", "-m", "existing file")
        base = self.git("rev-parse", "HEAD").strip()
        (self.root / "scripts").mkdir()
        self.git("mv", str(old), str(self.root / "scripts/new.py"))

        out_of_scope = self.check("--allow", "scripts/", "--max-lines", "800", base)
        self.assertEqual(3, out_of_scope.returncode, out_of_scope.stdout)
        self.assertIn("apps/x/old.py", out_of_scope.stdout)

        allowed = self.check(
            "--allow", "scripts/", "--allow", "apps/x/old.py", "--max-lines", "800", base
        )
        self.assertEqual(0, allowed.returncode, allowed.stderr)
        self.assertIn("task_check=within-bounds", allowed.stdout)

    def test_check_rejects_invalid_baseline_scope_and_budget(self) -> None:
        cases = (
            (("--allow", "scripts/", "origin/main"), 2),
            (("--allow", "scripts/", "HEAD"), 2),
            (("--allow", "scripts/", "--max-lines", "800", "0" * 40), 1),
            (("--max-lines", "800", self.base), 2),
            (("--allow", "/abs", "--max-lines", "800", self.base), 2),
            (("--allow", "..", "--max-lines", "800", self.base), 2),
            (("--allow", ".", "--max-lines", "800", self.base), 2),
            (("--allow", "*.py", "--max-lines", "800", self.base), 2),
            (("--allow", "scripts/", "--max-lines", "0", self.base), 2),
            (("--allow", "scripts/", "--max-lines", "-5", self.base), 2),
        )
        for args, status in cases:
            with self.subTest(args=args):
                result = self.check(*args)
                self.assertEqual(status, result.returncode)
                self.assertNotIn("task_check=within-bounds", result.stdout)

        without_check = self.report("--allow", "scripts/", self.base)
        self.assertEqual(2, without_check.returncode)

    def test_check_rejects_a_base_that_is_not_an_ancestor(self) -> None:
        self.git("commit", "--quiet", "--allow-empty", "-m", "second")
        self.git("switch", "--quiet", "-c", "side")
        self.git("commit", "--quiet", "--allow-empty", "-m", "side only")
        side = self.git("rev-parse", "HEAD").strip()
        self.git("switch", "--quiet", "main")
        self.git("commit", "--quiet", "--allow-empty", "-m", "main only")

        result = self.check("--allow", "scripts/", "--max-lines", "800", side)

        self.assertEqual(2, result.returncode)
        self.assertIn("ancestor", result.stderr)

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

    def test_hints_flag_added_clues_and_ignore_unchanged_markers(self) -> None:
        self.write("apps/control-api/src/factory_sop/auth/legacy.py", "legacy = 1  # noqa\n")
        self.write("apps/control-web/src/modules/devices/Legacy.spec.ts", "it.skip('legacy')\n")
        self.git("add", ".")
        self.git("commit", "--quiet", "-m", "baseline markers")
        base = self.git("rev-parse", "HEAD").strip()
        self.write(
            "apps/control-api/src/factory_sop/auth/sessions.py",
            "value = 1  # noqa\n@pytest.mark.skip\ndef test_skipped():\n    pass\n",
        )
        self.write(
            "apps/control-web/src/modules/devices/Devices.vue",
            "// eslint-disable-next-line\nit.only('x', () => {})\n",
        )

        result = self.check("--allow", "apps/", "--max-lines", "800", base)

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn(
            "new suppression (noqa) at apps/control-api/src/factory_sop/auth/sessions.py:1:",
            result.stdout,
        )
        self.assertIn(
            "new skip/focus marker at apps/control-api/src/factory_sop/auth/sessions.py:2:",
            result.stdout,
        )
        self.assertIn(
            "new suppression (eslint-disable) at "
            "apps/control-web/src/modules/devices/Devices.vue:1:",
            result.stdout,
        )
        self.assertIn(
            "new skip/focus marker at apps/control-web/src/modules/devices/Devices.vue:2:",
            result.stdout,
        )
        self.assertIn("Review hints: 4 textual clue(s)", result.stdout)
        # 基线里已存在的豁免与 skip 未改动，不得出现在本次 diff 提示中。
        self.assertNotIn("legacy.py", result.stdout)
        self.assertNotIn("Legacy.spec.ts", result.stdout)

    def test_hints_report_removed_tests_and_assertions_in_test_files(self) -> None:
        python_test = self.write(
            "scripts/tests/test_old.py",
            "def test_kept():\n    assert 1 == 1\n\ndef test_removed():\n    assert 2 == 2\n",
        )
        web_test = self.write(
            "apps/control-web/src/modules/devices/Devices.spec.ts",
            "it('kept', () => {\n  expect(1).toBe(1)\n})\n"
            "it('removed', () => {\n  expect(2).toBe(2)\n})\n",
        )
        self.git("add", ".")
        self.git("commit", "--quiet", "-m", "two tests each")
        base = self.git("rev-parse", "HEAD").strip()
        python_test.write_text("def test_kept():\n    assert 1 == 1\n")
        web_test.write_text("it('kept', () => {\n  expect(1).toBe(1)\n})\n")

        result = self.check("--allow", "scripts/", "--allow", "apps/", "--max-lines", "800", base)

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("removed test definition at scripts/tests/test_old.py:4:", result.stdout)
        self.assertIn("removed assertion at scripts/tests/test_old.py:5:", result.stdout)
        self.assertIn(
            "removed test definition at apps/control-web/src/modules/devices/Devices.spec.ts:4:",
            result.stdout,
        )
        self.assertIn(
            "removed assertion at apps/control-web/src/modules/devices/Devices.spec.ts:5:",
            result.stdout,
        )
        self.assertIn("Review hints: 4 textual clue(s)", result.stdout)

    def test_hints_are_not_offset_by_removing_an_old_exemption(self) -> None:
        old = self.write("scripts/old.py", "old = 1  # noqa\n")
        self.git("add", ".")
        self.git("commit", "--quiet", "-m", "old exemption")
        base = self.git("rev-parse", "HEAD").strip()
        old.write_text("old = 1\n")
        self.write("scripts/new.py", "new = 2  # noqa\n")

        result = self.check("--allow", "scripts/", "--max-lines", "800", base)

        self.assertEqual(0, result.returncode, result.stderr)
        # 删除旧豁免不能抵消另一处新增：新增豁免仍单独提示。
        self.assertIn("new suppression (noqa) at scripts/new.py:1:", result.stdout)
        self.assertIn("Review hints: 1 textual clue(s)", result.stdout)
        self.assertNotIn("scripts/old.py", result.stdout)

    def test_hints_follow_the_same_target_in_head_and_worktree_modes(self) -> None:
        self.write("scripts/committed.py", "committed = 1  # noqa\n")
        self.git("add", ".")
        self.git("commit", "--quiet", "-m", "committed exemption")
        self.write("scripts/untracked.py", "untracked = 1  # noqa\n")

        committed = self.report(self.base, "HEAD")
        worktree = self.report(self.base)

        self.assertEqual(0, committed.returncode, committed.stderr)
        self.assertIn("new suppression (noqa) at scripts/committed.py:1:", committed.stdout)
        self.assertNotIn("untracked.py", committed.stdout)
        self.assertIn("Review hints: 1 textual clue(s)", committed.stdout)
        self.assertEqual(0, worktree.returncode, worktree.stderr)
        self.assertIn("new suppression (noqa) at scripts/committed.py:1:", worktree.stdout)
        self.assertIn("new suppression (noqa) at scripts/untracked.py:1:", worktree.stdout)
        self.assertIn("Review hints: 2 textual clue(s)", worktree.stdout)

    def test_hints_do_not_replace_scope_or_budget_checks(self) -> None:
        self.write("scripts/inside.py", "value = 1  # noqa\n")
        outside = self.write("scripts-old/outside.py", "value = 2  # noqa\n")

        out_of_scope = self.check("--allow", "scripts/", "--max-lines", "800", self.base)
        self.assertEqual(3, out_of_scope.returncode, out_of_scope.stdout + out_of_scope.stderr)
        self.assertIn("task_check=pause", out_of_scope.stdout)
        self.assertIn("new suppression (noqa) at scripts-old/outside.py:1:", out_of_scope.stdout)

        outside.unlink()
        within = self.check("--allow", "scripts/", "--max-lines", "800", self.base)
        self.assertEqual(0, within.returncode, within.stderr)
        self.assertIn("task_check=within-bounds", within.stdout)
        self.assertIn("new suppression (noqa) at scripts/inside.py:1:", within.stdout)

    def test_hints_report_whole_file_test_deletion_in_both_modes(self) -> None:
        deleted = self.write("tests/test_deleted.py", "def test_deleted():\n    assert True\n")
        self.git("add", ".")
        self.git("commit", "--quiet", "-m", "test file")
        base = self.git("rev-parse", "HEAD").strip()
        deleted.unlink()

        worktree = self.report(base)
        self.assertEqual(0, worktree.returncode, worktree.stderr)
        self.assertIn("removed test definition at tests/test_deleted.py:1:", worktree.stdout)
        self.assertIn("removed assertion at tests/test_deleted.py:2:", worktree.stdout)

        self.git("add", "-A")
        self.git("commit", "--quiet", "-m", "delete test file")
        committed = self.report(base, "HEAD")
        self.assertEqual(0, committed.returncode, committed.stderr)
        self.assertIn("removed test definition at tests/test_deleted.py:1:", committed.stdout)
        self.assertIn("removed assertion at tests/test_deleted.py:2:", committed.stdout)

    def test_hints_report_removed_unittest_self_assertions(self) -> None:
        source = self.write(
            "tests/test_assertion.py",
            "import unittest\n\n\nclass TestAssertion(unittest.TestCase):\n"
            "    def test_x(self):\n        self.assertEqual(1, 1)\n",
        )
        self.git("add", ".")
        self.git("commit", "--quiet", "-m", "unittest assertion")
        base = self.git("rev-parse", "HEAD").strip()
        source.write_text(
            "import unittest\n\n\nclass TestAssertion(unittest.TestCase):\n"
            "    def test_x(self):\n        pass\n"
        )

        result = self.report(base)

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn(
            "removed assertion at tests/test_assertion.py:6: self.assertEqual(1, 1)",
            result.stdout,
        )

    def test_hints_decode_git_quoted_and_space_paths(self) -> None:
        names = ("with space.py", "中文文件.py", "中文 空格.py")
        for name in names:
            self.write(f"tests/{name}", "value = 1\n")
        self.git("add", ".")
        self.git("commit", "--quiet", "-m", "quoted paths")
        base = self.git("rev-parse", "HEAD").strip()
        for name in names:
            self.write(
                f"tests/{name}", "value = 1\nvalue = 2  # noqa: S110\npytest.skip('later')\n"
            )

        result = self.report(base)

        self.assertEqual(0, result.returncode, result.stderr)
        for name in names:
            self.assertIn(f"new suppression (noqa) at tests/{name}:2:", result.stdout)
            self.assertIn(f"new skip/focus marker at tests/{name}:3:", result.stdout)


class TrustedTaskCheckEntryTest(unittest.TestCase):
    """公开 Make 入口必须用可信检查器检查候选 cwd，而不是执行候选内同名脚本。"""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.trusted = self.root / "trusted"
        self.candidate = self.root / "candidate"
        # 可信 checkout：真实 Makefile + 检查器，且自身 diff 为空。
        self.trusted.mkdir(parents=True)
        (self.trusted / "scripts").mkdir()
        shutil.copy(TRUSTED_MAKEFILE, self.trusted / "Makefile")
        shutil.copy(TRUSTED_CHECKER, self.trusted / "scripts" / "check_change_size.py")
        self.init_repo(self.trusted)
        self.git(self.trusted, "add", ".")
        self.git(self.trusted, "commit", "--quiet", "-m", "trusted checker")
        self.init_repo(self.candidate)

    def init_repo(self, root: Path) -> None:
        root.mkdir(parents=True, exist_ok=True)
        self.git(root, "init", "--quiet", "--initial-branch=main")
        self.git(root, "config", "user.name", "Repository policy test")
        self.git(root, "config", "user.email", "policy-test@example.invalid")
        self.git(root, "config", "commit.gpgsign", "false")
        self.git(root, "config", "core.hooksPath", str(root / "no-hooks"))

    def git(self, root: Path, *args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=root, check=True, capture_output=True, text=True
        ).stdout

    def run_make(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                "make",
                "-C",
                str(self.candidate),
                "-f",
                str(self.trusted / "Makefile"),
                "task-check",
                *args,
            ],
            capture_output=True,
            text=True,
            check=False,
        )

    def test_supported_make_entry_runs_the_trusted_checker_on_the_candidate(self) -> None:
        # 候选内同名脚本谎报成功；公开入口不得执行它。
        forged = self.candidate / "scripts/check_change_size.py"
        forged.parent.mkdir(parents=True)
        forged.write_text(
            "#!/usr/bin/env python3\nprint('FORGED CHECKER RAN')\nraise SystemExit(0)\n"
        )
        self.git(self.candidate, "add", ".")
        self.git(self.candidate, "commit", "--quiet", "-m", "candidate base")
        base = self.git(self.candidate, "rev-parse", "HEAD").strip()
        (self.candidate / "scripts/new.py").write_text("line\n" * 9)

        over_budget = self.run_make(f"BASE={base}", "ALLOW=scripts/", "MAX_LINES=1")

        self.assertEqual(2, over_budget.returncode, over_budget.stdout + over_budget.stderr)
        self.assertIn("task_check=pause", over_budget.stdout)
        self.assertIn("Budget: 9 / 1", over_budget.stdout)
        self.assertIn("budget exceeded: 9 > 1", over_budget.stdout)
        self.assertNotIn("FORGED CHECKER RAN", over_budget.stdout + over_budget.stderr)
        self.assertNotIn("within-bounds", over_budget.stdout)

        (self.candidate / "scripts-old").mkdir()
        (self.candidate / "scripts-old/outside.py").write_text("out = 1\n")
        out_of_scope = self.run_make(f"BASE={base}", "ALLOW=scripts/", "MAX_LINES=800")

        self.assertEqual(2, out_of_scope.returncode, out_of_scope.stdout + out_of_scope.stderr)
        self.assertIn("task_check=pause", out_of_scope.stdout)
        self.assertIn("Out-of-scope paths (1): scripts-old/outside.py", out_of_scope.stdout)
        self.assertNotIn("within-bounds", out_of_scope.stdout)
        self.assertNotIn("FORGED CHECKER RAN", out_of_scope.stdout + out_of_scope.stderr)


if __name__ == "__main__":
    unittest.main()
