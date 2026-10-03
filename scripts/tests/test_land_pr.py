from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "land_pr.py"
HEAD = "a" * 40
BASE = "b" * 40
MERGE = "c" * 40


class LandPrTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix="land-pr-")
        self.root = Path(self.temp_dir.name)
        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir()
        self.calls_file = self.root / "gh-calls.jsonl"
        self.env = os.environ.copy()
        self.env.update(
            {
                "PATH": f"{self.bin_dir}{os.pathsep}{self.env['PATH']}",
                "GH_CALLS": str(self.calls_file),
                "GH_REPO_PAYLOAD": json.dumps({"nameWithOwner": "owner/repo"}),
                "GH_API_PAYLOAD": json.dumps({"message": "accepted"}),
                "GH_API_RETURN": "0",
            }
        )
        self._install_fake_gh()
        self._install_fake_git()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _install_fake_gh(self) -> None:
        fake_gh = self.bin_dir / "gh"
        fake_gh.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "args = sys.argv[1:]\n"
            "with open(os.environ['GH_CALLS'], 'a', encoding='utf-8') as stream:\n"
            "    stream.write(json.dumps(args) + '\\n')\n"
            "if args[:2] == ['pr', 'view']:\n"
            "    print(os.environ['GH_PR_PAYLOAD'])\n"
            "    raise SystemExit(0)\n"
            "if args[:2] == ['repo', 'view']:\n"
            "    print(os.environ['GH_REPO_PAYLOAD'])\n"
            "    raise SystemExit(0)\n"
            "if args[:1] == ['api']:\n"
            "    print(os.environ['GH_API_PAYLOAD'])\n"
            "    raise SystemExit(int(os.environ.get('GH_API_RETURN', '0')))\n"
            "print('unexpected gh call', file=sys.stderr)\n"
            "raise SystemExit(2)\n",
            encoding="utf-8",
        )
        fake_gh.chmod(0o755)

    def _install_fake_git(self) -> None:
        fake_git = self.bin_dir / "git"
        fake_git.write_text(
            "#!/usr/bin/env python3\n"
            "import os, sys\n"
            "args = sys.argv[1:]\n"
            "head = os.environ.get('GIT_LOCAL_HEAD', '')\n"
            "if args[:3] == ['rev-parse', '--verify', '--quiet']:\n"
            "    if not head: raise SystemExit(1)\n"
            "    print(head); raise SystemExit(0)\n"
            "if args == ['worktree', 'list', '--porcelain']:\n"
            "    path = os.environ.get('GIT_WORKTREE_PATH', '')\n"
            "    if path:\n"
            "        print(f'worktree {path}\\nHEAD {head}\\nbranch refs/heads/agent/test/task')\n"
            "    raise SystemExit(0)\n"
            "if len(args) >= 3 and args[0] == '-C' and args[2] == 'status':\n"
            "    if os.environ.get('GIT_WORKTREE_DIRTY') == '1': print(' M local.txt')\n"
            "    raise SystemExit(0)\n"
            "print('unexpected git call', file=sys.stderr); raise SystemExit(2)\n",
            encoding="utf-8",
        )
        fake_git.chmod(0o755)

    def pr_payload(
        self,
        *,
        state: str = "OPEN",
        draft: bool = False,
        head: str = HEAD,
        merge_state: str = "CLEAN",
        mergeable: str = "MERGEABLE",
        ci_status: str = "COMPLETED",
        ci_conclusion: str | None = "SUCCESS",
        review: str = "",
        merge_commit: str | None = None,
    ) -> dict[str, object]:
        check: dict[str, object] = {
            "name": "CI required",
            "status": ci_status,
        }
        if ci_conclusion is not None:
            check["conclusion"] = ci_conclusion
        return {
            "state": state,
            "isDraft": draft,
            "baseRefName": "main",
            "baseRefOid": BASE,
            "headRefName": "agent/test/task",
            "headRefOid": head,
            "mergeStateStatus": merge_state,
            "mergeable": mergeable,
            "statusCheckRollup": [check],
            "reviewDecision": review,
            "mergeCommit": {"oid": merge_commit} if merge_commit else None,
        }

    def run_cli(
        self,
        *arguments: str,
        payload: dict[str, object] | None = None,
        api_payload: dict[str, object] | None = None,
        cwd: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        env = self.env.copy()
        env["GH_PR_PAYLOAD"] = json.dumps(payload or self.pr_payload())
        if api_payload is not None:
            env["GH_API_PAYLOAD"] = json.dumps(api_payload)
        return subprocess.run(
            [sys.executable, str(SCRIPT), *arguments],
            cwd=cwd or self.root,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def calls(self) -> list[list[str]]:
        if not self.calls_file.exists():
            return []
        return [
            json.loads(line) for line in self.calls_file.read_text(encoding="utf-8").splitlines()
        ]

    def test_status_reports_refresh_for_behind_head(self) -> None:
        result = self.run_cli(
            "status",
            "--pr",
            "123",
            payload=self.pr_payload(merge_state="BEHIND"),
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("head_branch=agent/test/task", result.stdout)
        self.assertIn(f"head_sha={HEAD}", result.stdout)
        self.assertIn("ci_required=success", result.stdout)
        self.assertIn("next_action=refresh", result.stdout)
        self.assertEqual(1, len(self.calls()))

    def test_status_reports_merge_only_for_clean_green_head(self) -> None:
        result = self.run_cli("status", "--pr", "123")

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("merge_state=clean", result.stdout)
        self.assertIn("mergeable=mergeable", result.stdout)
        self.assertIn("next_action=merge", result.stdout)

    def test_status_reports_merge_for_blocked_mergeable_green_head(self) -> None:
        # 总体 merge_state=BLOCKED 但 mergeable 且 CI 绿、无拒绝：状态必须与 merge 一致。
        result = self.run_cli(
            "status",
            "--pr",
            "123",
            payload=self.pr_payload(merge_state="BLOCKED"),
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("merge_state=blocked", result.stdout)
        self.assertIn("mergeable=mergeable", result.stdout)
        self.assertIn("next_action=merge", result.stdout)

    def test_status_reports_cleanup_after_merge(self) -> None:
        result = self.run_cli(
            "status",
            "--pr",
            "123",
            payload=self.pr_payload(state="MERGED", merge_commit=MERGE),
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn(f"merge_commit={MERGE}", result.stdout)
        self.assertIn("next_action=cleanup", result.stdout)

    def test_status_reports_conflict_without_mutation(self) -> None:
        result = self.run_cli(
            "status",
            "--pr",
            "123",
            payload=self.pr_payload(merge_state="DIRTY", mergeable="CONFLICTING"),
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("next_action=resolve-conflict", result.stdout)
        self.assertEqual(1, len(self.calls()))

    def test_status_blocks_missing_or_unknown_required_ci(self) -> None:
        cases = (
            [],
            "malformed",
            [42],
            [{"name": "CI required", "status": "COMPLETED", "conclusion": "NEUTRAL"}],
        )
        for checks in cases:
            with self.subTest(checks=checks):
                payload = self.pr_payload()
                payload["statusCheckRollup"] = checks
                result = self.run_cli("status", "--pr", "123", payload=payload)

                self.assertEqual(0, result.returncode, result.stderr)
                self.assertIn("next_action=blocked-ci", result.stdout)

    def test_status_stops_when_local_branch_has_unpublished_head(self) -> None:
        self.env["GIT_LOCAL_HEAD"] = "d" * 40

        result = self.run_cli("status", "--pr", "123")

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("local_candidate=mismatch", result.stdout)
        self.assertIn("next_action=local-work", result.stdout)

    def test_merge_refuses_dirty_local_task_worktree(self) -> None:
        self.env.update(
            {
                "GIT_LOCAL_HEAD": HEAD,
                "GIT_WORKTREE_PATH": str(self.root / "task"),
                "GIT_WORKTREE_DIRTY": "1",
            }
        )

        result = self.run_cli("merge", "--pr", "123", "--expected-head", HEAD)

        self.assertNotEqual(0, result.returncode)
        self.assertIn("local PR candidate is dirty", result.stderr)
        self.assertEqual(1, len(self.calls()))

    def test_refresh_uses_expected_head_cas(self) -> None:
        result = self.run_cli(
            "refresh",
            "--pr",
            "123",
            "--expected-head",
            HEAD,
            payload=self.pr_payload(merge_state="BEHIND"),
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("action=refresh-requested", result.stdout)
        self.assertIn("evidence=stale", result.stdout)
        self.assertEqual(
            [
                "api",
                "--method",
                "PUT",
                "repos/owner/repo/pulls/123/update-branch",
                "-f",
                f"expected_head_sha={HEAD}",
            ],
            self.calls()[-1],
        )

    def test_refresh_rejects_changed_head_before_mutation(self) -> None:
        result = self.run_cli(
            "refresh",
            "--pr",
            "123",
            "--expected-head",
            "d" * 40,
            payload=self.pr_payload(merge_state="BEHIND"),
        )

        self.assertNotEqual(0, result.returncode)
        self.assertIn("pull-request head changed", result.stderr)
        self.assertEqual(1, len(self.calls()))

    def test_refresh_refuses_non_behind_pr(self) -> None:
        result = self.run_cli(
            "refresh",
            "--pr",
            "123",
            "--expected-head",
            HEAD,
        )

        self.assertNotEqual(0, result.returncode)
        self.assertIn("does not need a base refresh", result.stderr)
        self.assertEqual(1, len(self.calls()))

    def test_merge_uses_exact_head_and_squash(self) -> None:
        result = self.run_cli(
            "merge",
            "--pr",
            "123",
            "--expected-head",
            HEAD,
            api_payload={"merged": True, "sha": MERGE, "message": "merged"},
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("action=merged", result.stdout)
        self.assertIn(f"merge_commit={MERGE}", result.stdout)
        self.assertEqual(
            [
                "api",
                "--method",
                "PUT",
                "repos/owner/repo/pulls/123/merge",
                "-f",
                f"sha={HEAD}",
                "-f",
                "merge_method=squash",
            ],
            self.calls()[-1],
        )

    def test_merge_refuses_stale_ci_or_behind_head_before_mutation(self) -> None:
        cases = (
            (
                "ci-not-success",
                self.pr_payload(ci_status="IN_PROGRESS", ci_conclusion=None),
                "CI required is not successful",
            ),
            ("behind", self.pr_payload(merge_state="BEHIND"), "not cleanly mergeable"),
            ("unknown", self.pr_payload(merge_state="UNKNOWN"), "not cleanly mergeable"),
            (
                "unknown-unmergeable",
                self.pr_payload(merge_state="UNKNOWN", mergeable="UNKNOWN"),
                "not cleanly mergeable",
            ),
            (
                "dirty-conflicting",
                self.pr_payload(merge_state="DIRTY", mergeable="CONFLICTING"),
                "not cleanly mergeable",
            ),
            (
                "blocked-conflicting",
                self.pr_payload(merge_state="BLOCKED", mergeable="CONFLICTING"),
                "not cleanly mergeable",
            ),
        )
        for label, payload, expected_error in cases:
            with self.subTest(case=label):
                self.calls_file.unlink(missing_ok=True)
                result = self.run_cli(
                    "merge",
                    "--pr",
                    "123",
                    "--expected-head",
                    HEAD,
                    payload=payload,
                )
                self.assertNotEqual(0, result.returncode)
                self.assertIn(expected_error, result.stderr)
                self.assertEqual(1, len(self.calls()))

    def test_merge_refuses_changes_requested(self) -> None:
        result = self.run_cli(
            "merge",
            "--pr",
            "123",
            "--expected-head",
            HEAD,
            payload=self.pr_payload(review="CHANGES_REQUESTED"),
        )

        self.assertNotEqual(0, result.returncode)
        self.assertIn("changes requested", result.stderr)
        self.assertEqual(1, len(self.calls()))

    def test_merge_requires_positive_api_confirmation(self) -> None:
        result = self.run_cli(
            "merge",
            "--pr",
            "123",
            "--expected-head",
            HEAD,
            api_payload={"merged": False, "sha": MERGE, "message": "blocked"},
        )

        self.assertNotEqual(0, result.returncode)
        self.assertIn("GitHub did not merge the pull request: blocked", result.stderr)

    def test_merge_allows_blocked_mergeable_green_head_to_reach_server_cas(self) -> None:
        # 总体 merge_state=BLOCKED 但 mergeable 且 CI 绿、无拒绝：服务器 ruleset 是权威，
        # 必须到达精确 head 的 squash CAS，由服务器对执行 actor 判定，本地不得提前否决。
        result = self.run_cli(
            "merge",
            "--pr",
            "123",
            "--expected-head",
            HEAD,
            payload=self.pr_payload(merge_state="BLOCKED"),
            api_payload={"merged": True, "sha": MERGE, "message": "merged"},
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("action=merged", result.stdout)
        self.assertIn(f"merge_commit={MERGE}", result.stdout)
        self.assertEqual(
            [
                "api",
                "--method",
                "PUT",
                "repos/owner/repo/pulls/123/merge",
                "-f",
                f"sha={HEAD}",
                "-f",
                "merge_method=squash",
            ],
            self.calls()[-1],
        )
        self.assertEqual(1, len(self.merge_writes()))

    def test_merge_blocked_head_server_rejection_is_not_retried(self) -> None:
        # BLOCKED 头到达服务器后仍被否决（规则不满足或 API 失败）必须报错且不重试，只写一次。
        cases = (
            ("merged-false", "0", {"merged": False, "sha": MERGE, "message": "review required"}),
            ("api-error", "1", {"message": "Resource not accessible by integration"}),
        )
        for label, api_return, api_payload in cases:
            with self.subTest(case=label):
                self.calls_file.unlink(missing_ok=True)
                self.env["GH_API_RETURN"] = api_return
                try:
                    result = self.run_cli(
                        "merge",
                        "--pr",
                        "123",
                        "--expected-head",
                        HEAD,
                        payload=self.pr_payload(merge_state="BLOCKED"),
                        api_payload=api_payload,
                    )
                finally:
                    self.env["GH_API_RETURN"] = "0"
                self.assertNotEqual(0, result.returncode)
                self.assertEqual(1, len(self.merge_writes()))

    def merge_writes(self) -> list[list[str]]:
        return [
            call
            for call in self.calls()
            if call[:3] == ["api", "--method", "PUT"] and call[3].endswith("/merge")
        ]

    def _use_native_git(self) -> None:
        # 移除 PATH 上的 fake git 以暴露真实 git；fake gh 仍拦截网络变更。
        (self.bin_dir / "git").unlink()
        self.env.pop("GIT_DIR", None)
        self.env.pop("GIT_WORK_TREE", None)
        self.env["GIT_CEILING_DIRECTORIES"] = str(self.root)
        self.env["GIT_CONFIG_GLOBAL"] = str(self.root / "gitconfig")
        self.env["GIT_CONFIG_NOSYSTEM"] = "1"

    def _native_repo(self) -> Path:
        self._use_native_git()
        repo = self.root / "repo"
        repo.mkdir()
        for arguments in (
            ("init", "--quiet"),
            ("config", "user.name", "Landing test"),
            ("config", "user.email", "landing@example.invalid"),
            ("commit", "--allow-empty", "--quiet", "-m", "base"),
            ("checkout", "--quiet", "--detach"),
        ):
            result = subprocess.run(
                ["git", *arguments],
                cwd=repo,
                env=self.env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(0, result.returncode, result.stderr)
        return repo

    def test_real_git_absent_local_branch_reaches_exact_head_cas(self) -> None:
        # 真实 git 对缺失分支 `rev-parse --verify --quiet` 返回 1，而 `show-ref` 返回 128；
        # 缺失的本地 PR 分支必须视为 absent 并继续 refresh/merge 的精确 head CAS。
        repo = self._native_repo()

        refreshed = self.run_cli(
            "refresh",
            "--pr",
            "123",
            "--expected-head",
            HEAD,
            payload=self.pr_payload(merge_state="BEHIND"),
            cwd=repo,
        )
        self.assertEqual(0, refreshed.returncode, refreshed.stderr)
        self.assertIn("action=refresh-requested", refreshed.stdout)
        self.assertEqual(
            [
                "api",
                "--method",
                "PUT",
                "repos/owner/repo/pulls/123/update-branch",
                "-f",
                f"expected_head_sha={HEAD}",
            ],
            self.calls()[-1],
        )

        merged = self.run_cli(
            "merge",
            "--pr",
            "123",
            "--expected-head",
            HEAD,
            api_payload={"merged": True, "sha": MERGE, "message": "merged"},
            cwd=repo,
        )
        self.assertEqual(0, merged.returncode, merged.stderr)
        self.assertIn("action=merged", merged.stdout)
        self.assertEqual(
            [
                "api",
                "--method",
                "PUT",
                "repos/owner/repo/pulls/123/merge",
                "-f",
                f"sha={HEAD}",
                "-f",
                "merge_method=squash",
            ],
            self.calls()[-1],
        )

    def test_real_git_failure_is_not_treated_as_absent_branch(self) -> None:
        # 非 git 仓库中 `rev-parse --verify --quiet` 返回 128；必须失败而不是伪造 absent，
        # 且在任何 gh 变更之前停止。
        self._use_native_git()

        result = self.run_cli(
            "refresh",
            "--pr",
            "123",
            "--expected-head",
            HEAD,
            payload=self.pr_payload(merge_state="BEHIND"),
        )

        self.assertNotEqual(0, result.returncode)
        self.assertIn("cannot read local PR branch", result.stderr)
        self.assertEqual(["pr", "view", "123"], self.calls()[0][:3])
        self.assertEqual(1, len(self.calls()))


if __name__ == "__main__":
    unittest.main()
