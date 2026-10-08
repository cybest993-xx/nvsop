"""跨脚本工作流回归：经真实公开 CLI 入口验证两个已确认缺陷。

证据层级：这是 CLI 级集成测试，不是 parser 单测。landing-queue 路径用真实临时
Git worktree + 真实绑定，经 `landing_queue.main` 的 enqueue/prepare 子命令驱动；
仅在既有 Backend 边界注入 synthetic adapter（明确非 live GitHub）。dev-status 路径
经 `dev.main` 驱动，使用真实 state 文件与真实 main checkout fixture，只在 HTTP/compose
probe 边界注入合成输入（明确非 live Docker）。不进行任何真实 GitHub 写入、入队或发布。
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(HERE))

import test_landing_queue as tq  # noqa: E402

import landing_queue as lq  # noqa: E402

SPEC = importlib.util.spec_from_file_location("nvsop_dev_script_e2e", ROOT / "scripts" / "dev.py")
assert SPEC is not None and SPEC.loader is not None
DEV = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = DEV
SPEC.loader.exec_module(DEV)


class LandingQueueCliTest(tq.GitTaskFixture):
    """docs 佐证经真实 enqueue/prepare CLI 进入队列；错误佐证不得入队。"""

    def _backend(self, body: str, *, edited: bool = False) -> tq.FakeBackend:
        att = tq.comment(
            500,
            body,
            pr=1,
            updated="2024-02-01T00:00:00Z" if edited else "2024-01-01T00:00:00Z",
        )
        return tq.FakeBackend(pr=tq.pr_state(head=self.head), comments={500: att}, main=tq.BASE)

    def _enqueue(self, backend: tq.FakeBackend) -> int:
        with (
            mock.patch.object(lq, "GitHub", return_value=backend),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            return lq.main(
                ["enqueue", "--pr", "1", "--expected-head", self.head, "--attestation", "500"]
            )

    def _prepare(self, backend: tq.FakeBackend) -> int:
        with (
            mock.patch.object(lq, "GitHub", return_value=backend),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            return lq.main(
                ["prepare", "--run-id", "9", "--attempt", "1", "--controller-sha", self.head]
            )

    @staticmethod
    def _docs_check(
        root: str, *, command: str = "make check-docs", result: str = "passed"
    ) -> dict[str, str]:
        return {"root": root, "command": command, "result": result, "evidence": "docs lane output"}

    def test_docs_attestation_enqueues_and_is_consumed_from_inbox(self) -> None:
        body = tq.attestation_body(pr=1, root=self.head, check=self._docs_check(self.head))
        backend = self._backend(body)
        self.assertEqual(0, self._enqueue(backend))
        self.assertEqual(1, len(backend.posted))
        # 把 CLI 真正投递的请求放入 inbox，再由 prepare CLI 消费成队列条目。
        backend.comments[1] = tq.comment(1, backend.posted[0], pr=1)
        self.assertEqual(0, self._prepare(backend))
        # prepare 会把 FIFO 队首自动 promote 为 ACTIVE（生产行为）；root 必须仍是 self.head。
        entry = lq.find(backend.state_obj, 1)
        self.assertEqual(lq.ACTIVE, entry.state)
        self.assertEqual(self.head, entry.authorization_root)

    def test_wrong_root_result_and_edited_authority_never_enqueue(self) -> None:
        wrong_root = tq.attestation_body(
            pr=1, root=tq.NEW_HEAD, check=self._docs_check(tq.NEW_HEAD)
        )
        backend = self._backend(wrong_root)
        self.assertEqual(1, self._enqueue(backend))
        self.assertEqual([], backend.posted)

        wrong_result = tq.attestation_body(
            pr=1, root=self.head, check=self._docs_check(self.head, result="failed")
        )
        backend = self._backend(wrong_result)
        self.assertEqual(1, self._enqueue(backend))
        self.assertEqual([], backend.posted)

        edited = tq.attestation_body(pr=1, root=self.head, check=self._docs_check(self.head))
        backend = self._backend(edited, edited=True)
        self.assertEqual(1, self._enqueue(backend))
        self.assertEqual([], backend.posted)


class DevStatusCliTest(unittest.TestCase):
    """dev status 经真实 CLI：persisted ready 但实测不健康时显示 degraded，只读不回写。"""

    def _main_repo(self, root: Path) -> Path:
        repo = root / "repo"
        repo.mkdir()
        environment = os.environ.copy()
        environment.update(
            {
                "GIT_CONFIG_GLOBAL": str(root / "global"),
                "GIT_CONFIG_NOSYSTEM": "1",
                "HOME": str(root),
            }
        )

        def git(*arguments: str) -> None:
            result = subprocess.run(
                ["git", *arguments], cwd=repo, env=environment, capture_output=True, text=True
            )
            if result.returncode != 0:
                self.fail(result.stderr or result.stdout)

        git("init", "--quiet", "-b", "main")
        git("config", "user.name", "dev test")
        git("config", "user.email", "dev@example.invalid")
        (repo / "base.txt").write_text("base\n", encoding="utf-8")
        git("add", "base.txt")
        git("commit", "--quiet", "-m", "base")
        return repo.resolve()

    def _run_status(self, *, healthy: bool) -> tuple[int, str, bytes, bytes]:
        with tempfile.TemporaryDirectory(prefix="dev-status-e2e-") as directory:
            root = Path(directory)
            repo = self._main_repo(root)
            item = DEV.DevPaths(root=repo, state=root / "state")
            DEV.ensure_directories(item)
            persisted = DEV.initial_state("http")
            persisted.update({"status": "ready", "running_sha": "a" * 40, "target_sha": "a" * 40})
            DEV.write_state(item, persisted)
            item.setup_file.write_text("{}\n", encoding="utf-8")
            rows = [
                {"Service": name, "State": "running", "Health": "healthy"}
                for name in sorted(DEV._REQUIRED_READY_SERVICES)
            ]
            if not healthy:
                rows = [row for row in rows if row["Service"] != "worker"]
                rows.append({"Service": "worker", "State": "exited", "ExitCode": 1, "Health": ""})
            before = item.state_file.read_bytes()
            output = io.StringIO()
            with (
                mock.patch.object(DEV, "paths", return_value=item),
                mock.patch.object(DEV, "service_rows", return_value=(rows, None)),
                mock.patch.object(DEV, "http_probe", return_value={"ok": True, "status": 200}),
                contextlib.redirect_stdout(output),
            ):
                code = DEV.main(["status"])
            after = item.state_file.read_bytes()
        return code, output.getvalue(), before, after

    def test_healthy_ready_reports_ready_and_exit_zero(self) -> None:
        code, output, before, after = self._run_status(healthy=True)
        self.assertEqual(0, code)
        self.assertIn('"status": "ready"', output)
        self.assertEqual(before, after)

    def test_unhealthy_persisted_ready_reports_degraded_and_exit_one(self) -> None:
        code, output, before, after = self._run_status(healthy=False)
        self.assertEqual(1, code)
        self.assertIn('"status": "degraded"', output)
        self.assertNotIn('"status": "ready"', output)
        # 只读：state 文件必须逐字节不变。
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
