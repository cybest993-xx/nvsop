"""固定开发实例状态的公开 CLI 回归。

使用真实状态文件与临时 main checkout，仅在 HTTP/compose probe seam 注入合成输入；
不是真实 Docker 或现场验收。保留健康探测与只读状态查询的既有缺陷回归。
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

SPEC = importlib.util.spec_from_file_location("nvsop_dev_script_e2e", ROOT / "scripts" / "dev.py")
assert SPEC is not None and SPEC.loader is not None
DEV = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = DEV
SPEC.loader.exec_module(DEV)


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
