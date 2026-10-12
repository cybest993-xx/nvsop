from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest
from pathlib import Path
from threading import Event
from types import ModuleType
from unittest.mock import patch

from scripts import dev_snapshot

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("nvsop_dev_script", ROOT / "scripts" / "dev.py")
assert SPEC is not None and SPEC.loader is not None
DEV = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = DEV
SPEC.loader.exec_module(DEV)


class DevProtocolTest(unittest.TestCase):
    def test_http_is_the_default_and_https_is_explicit(self) -> None:
        self.assertEqual("http", DEV.configured_protocol({}))
        self.assertEqual("http", DEV.configured_protocol({"NVSOP_DEV_PROTOCOL": "http"}))
        self.assertEqual("https", DEV.configured_protocol({"NVSOP_DEV_PROTOCOL": "https"}))
        self.assertEqual("http://localhost:8443", DEV.public_urls("http")["business"])
        self.assertEqual("https://localhost:8443", DEV.public_urls("https")["business"])

    def test_default_state_dir_uses_repository_local_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.dict(os.environ, {"NVSOP_DEV_STATE_DIR": ""}):
                item = DEV.paths(root=root)
        self.assertEqual(root.resolve() / ".nvsop" / "dev-main", item.state)

    def test_state_without_protocol_keeps_legacy_https_meaning(self) -> None:
        item = DEV.DevPaths(root=Path("/repo"), state=Path("/state"))
        with patch.object(DEV, "read_state", return_value={}):
            self.assertEqual("https", DEV.state_protocol(item))

    def test_default_http_runtime_environment_uses_local_http_contract(self) -> None:
        item = DEV.DevPaths(root=Path("/repo"), state=Path("/state"))

        environment = DEV.runtime_environment(
            item,
            sha="abc",
            source=Path("/snapshot"),
            protocol=DEV.configured_protocol({}),
        )

        self.assertEqual("http", environment["NVSOP_DEV_PROTOCOL"])
        self.assertEqual("allow_http", environment["NVSOP_DEV_COOKIE_TRANSPORT"])
        self.assertEqual("http://localhost:8443", environment["NVSOP_DEV_BASE_URL"])
        self.assertEqual("http://localhost:8444", environment["NVSOP_DEV_MEDIA_URL"])
        self.assertNotIn("SSL_CERT_FILE", environment)

    def test_explicit_https_runtime_environment_keeps_tls_contract(self) -> None:
        item = DEV.DevPaths(root=Path("/repo"), state=Path("/state"))

        environment = DEV.runtime_environment(
            item,
            sha="abc",
            source=Path("/snapshot"),
            protocol="https",
        )

        self.assertEqual("https", environment["NVSOP_DEV_PROTOCOL"])
        self.assertEqual("require_https", environment["NVSOP_DEV_COOKIE_TRANSPORT"])
        self.assertEqual("https://localhost:8443", environment["NVSOP_DEV_BASE_URL"])
        self.assertEqual("https://localhost:8444", environment["NVSOP_DEV_MEDIA_URL"])
        # 未显式选择自定义 CA 时使用系统信任库，不注入 SSL_CERT_FILE。
        self.assertNotIn("SSL_CERT_FILE", environment)
        self.assertEqual("", environment["NVSOP_DEV_TLS_HEALTHCHECK_ARGS"])

    def test_unknown_protocol_is_rejected(self) -> None:
        with self.assertRaises(DEV.DevError):
            DEV.configured_protocol({"NVSOP_DEV_PROTOCOL": "ftp"})

    def test_archive_environment_removes_global_lfs_skip_smudge(self) -> None:
        source = {"GIT_LFS_SKIP_SMUDGE": "1", "EXAMPLE": "value"}

        environment = DEV.archive_environment(source)

        self.assertNotIn("GIT_LFS_SKIP_SMUDGE", environment)
        self.assertEqual("value", environment["EXAMPLE"])
        self.assertEqual("1", source["GIT_LFS_SKIP_SMUDGE"])

    @staticmethod
    def git(root: Path, *arguments: str) -> str:
        result = subprocess.run(
            ["git", *arguments], cwd=root, check=False, capture_output=True, text=True
        )
        if result.returncode != 0:
            raise AssertionError(result.stderr)
        return result.stdout.strip()

    def make_git_repo(self, root: Path) -> None:
        root.mkdir()
        self.git(root, "init")
        self.git(root, "config", "user.email", "test@example.invalid")
        self.git(root, "config", "user.name", "NVSOP test")

    def require_git_lfs(self) -> None:
        if shutil.which("git-lfs") is None:
            self.fail("关键 Git-LFS 测试需要 git-lfs；环境缺失时不能跳过")

    def test_archive_main_extracts_a_real_complete_git_snapshot(self) -> None:
        self.require_git_lfs()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "repo"
            self.make_git_repo(root)
            (root / "README.md").write_text("snapshot\n", encoding="utf-8")
            self.git(root, "add", "README.md")
            self.git(root, "commit", "-m", "snapshot")
            sha = self.git(root, "rev-parse", "HEAD")
            item = DEV.DevPaths(root=root, state=Path(directory) / "state")

            snapshot = DEV.archive_main(item, sha)

            self.assertEqual("snapshot\n", (snapshot / "README.md").read_text())
            self.assertEqual(sha, (snapshot / ".nvsop-source-sha").read_text().strip())
            manifest = DEV.read_snapshot_manifest(snapshot)
            self.assertIsNotNone(manifest)
            assert manifest is not None
            self.assertTrue(manifest["complete"])
            self.assertEqual([], manifest["missing_lfs_paths"])

    def test_missing_lfs_always_fails_and_records_audit(self) -> None:
        self.require_git_lfs()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "repo"
            self.make_git_repo(root)
            self.git(root, "lfs", "track", "*.png")
            relative = "assets/missing.png"
            pointer = root / relative
            pointer.parent.mkdir(parents=True)
            oid = "0" * 64
            pointer.write_text(
                f"version https://git-lfs.github.com/spec/v1\noid sha256:{oid}\nsize 123\n",
                encoding="ascii",
            )
            self.git(root, "add", ".gitattributes", relative)
            self.git(root, "commit", "-m", "missing optional asset")
            sha = self.git(root, "rev-parse", "HEAD")
            item = DEV.DevPaths(root=root, state=Path(directory) / "state")

            with self.assertRaises(DEV.MissingLfsError) as failure:
                DEV.archive_main(item, sha)
            self.assertIn(relative, str(failure.exception))
            self.assertIn(oid, str(failure.exception))
            audit = item.logs / f"snapshot-{sha}.json"
            self.assertIn(relative, audit.read_text(encoding="utf-8"))

    def test_hydrated_lfs_without_local_object_fails_before_archive(self) -> None:
        self.require_git_lfs()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "repo"
            self.make_git_repo(root)
            self.git(root, "lfs", "track", "*.png")
            relative = "hydrated.png"
            pointer = root / relative
            pointer.parent.mkdir(parents=True, exist_ok=True)
            pointer.write_bytes(b"available content\\n")
            self.git(root, "add", ".gitattributes", relative)
            self.git(root, "commit", "-m", "hydrated but unavailable asset")
            sha = self.git(root, "rev-parse", "HEAD")
            oid = self.git(root, "lfs", "ls-files", "--long", sha).split(maxsplit=1)[0]
            media_directory = Path(
                next(
                    line.partition("=")[2]
                    for line in self.git(root, "lfs", "env").splitlines()
                    if line.startswith("LocalMediaDir=")
                )
            )
            object_path = media_directory / oid[:2] / oid[2:4] / oid
            object_path.unlink()
            item = DEV.DevPaths(root=root, state=Path(directory) / "state")

            with self.assertRaises(DEV.MissingLfsError) as failure:
                DEV.archive_main(item, sha)

            self.assertIn(relative, str(failure.exception))
            self.assertIn(oid, str(failure.exception))
            audit = item.logs / f"snapshot-{sha}.json"
            self.assertIn(relative, audit.read_text(encoding="utf-8"))

    def test_available_lfs_object_is_hydrated_as_real_file(self) -> None:
        self.require_git_lfs()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "repo"
            self.make_git_repo(root)
            self.git(root, "lfs", "track", "*.bin")
            available = root / "available.bin"
            available.write_bytes(b"available content\n")
            self.git(root, "add", ".gitattributes", "available.bin")
            self.git(root, "commit", "-m", "available lfs")
            sha = self.git(root, "rev-parse", "HEAD")
            item = DEV.DevPaths(root=root, state=Path(directory) / "state")

            snapshot = DEV.archive_main(item, sha)

            self.assertEqual(b"available content\n", (snapshot / "available.bin").read_bytes())
            manifest = DEV.read_snapshot_manifest(snapshot)
            self.assertIsNotNone(manifest)
            assert manifest is not None
            self.assertTrue(manifest["complete"])
            self.assertEqual([], manifest["missing_lfs_paths"])

    def test_archive_stream_failure_is_wrapped_and_cleans_temporary_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "repo"
            self.make_git_repo(root)
            (root / "README.md").write_text("snapshot\n", encoding="utf-8")
            self.git(root, "add", "README.md")
            self.git(root, "commit", "-m", "snapshot")
            sha = self.git(root, "rev-parse", "HEAD")
            item = DEV.DevPaths(root=root, state=Path(directory) / "state")
            with (
                patch.object(
                    dev_snapshot.tarfile, "open", side_effect=tarfile.ReadError("broken tar")
                ),
                self.assertRaises(DEV.DevError) as failure,
            ):
                DEV.archive_main(item, sha)

            self.assertIn("Git archive 流失败", str(failure.exception))
            self.assertFalse((item.snapshots / f".{sha}.tmp-{os.getpid()}").exists())
            self.assertFalse((item.snapshots / sha).exists())

    def test_tilt_commands_use_the_committed_snapshot_script(self) -> None:
        item = DEV.DevPaths(root=Path("/repo"), state=Path("/state"))
        environment = DEV.runtime_environment(item, sha="abc", source=Path("/snapshot"))

        self.assertEqual("/snapshot/scripts/dev.py", environment["NVSOP_LAUNCHER_SCRIPT"])
        self.assertEqual("/repo/scripts/dev.py", environment["NVSOP_HOST_LAUNCHER_SCRIPT"])

    @unittest.skipUnless(shutil.which("tilt"), "需要 Tilt v0.37.7")
    def test_tilt_manual_resources_are_idle_until_triggered(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "scripts").mkdir()
            (root / "apps/control-web").mkdir(parents=True)
            for path in (
                root / "scripts/dev.py",
                root / "scripts/dev_smoke.py",
                root / "apps/control-web/playwright.config.ts",
            ):
                path.touch()
            fake_bin = root / "bin"
            fake_bin.mkdir()
            calls = root / "launcher.calls"
            fake_python = fake_bin / "python3"
            fake_python.write_text(
                '#!/bin/sh\nprintf \'%s\\n\' "$*" >> "$NVSOP_TILT_TEST_CALLS"\n',
                encoding="utf-8",
            )
            fake_python.chmod(0o700)
            launcher = (root / "scripts/dev.py").as_posix()
            helper = (ROOT / "scripts/dev_tilt.star").as_posix()
            tiltfile = root / "Tiltfile"
            tiltfile.write_text(
                f'''\nload("{helper}", "configure_manual_test_resources")
local_resource("sample-data", cmd="true")
local_resource("gateway", cmd="true")
configure_manual_test_resources(
    base_url="http://example",
    media_url="http://media",
    smoke_report="file://smoke.json",
    ui_reports="file://ui",
    host_launcher_script="{launcher}",
)
''',
                encoding="utf-8",
            )
            environment = os.environ.copy()
            environment.update(
                {
                    "PATH": f"{fake_bin}{os.pathsep}{environment['PATH']}",
                    "NVSOP_TILT_TEST_CALLS": str(calls),
                }
            )
            ci = subprocess.run(
                [
                    "tilt",
                    "ci",
                    "--file",
                    str(tiltfile),
                    "--port",
                    "0",
                    "--timeout",
                    "10s",
                    "--log-level",
                    "error",
                ],
                check=False,
                capture_output=True,
                env=environment,
                text=True,
            )
            self.assertEqual(0, ci.returncode, ci.stdout + ci.stderr)
            self.assertFalse(calls.exists(), "manual resources must not run during tilt ci")

            port = self.free_port()
            server = subprocess.Popen(
                [
                    "tilt",
                    "up",
                    "--file",
                    str(tiltfile),
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port),
                    "--stream",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=environment,
            )
            try:
                self.wait_for_tilt_resource(port, "functional-smoke")
                for resource in ("functional-smoke", "visual-tests"):
                    triggered = subprocess.run(
                        [
                            "tilt",
                            "trigger",
                            resource,
                            "--host",
                            "127.0.0.1",
                            "--port",
                            str(port),
                        ],
                        check=False,
                        capture_output=True,
                        text=True,
                    )
                    self.assertEqual(0, triggered.returncode, triggered.stderr)
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    if calls.is_file() and {"smoke", "test-ui"} <= {
                        line.rsplit(" ", 1)[-1] for line in calls.read_text().splitlines()
                    }:
                        break
                    time.sleep(0.1)
                self.assertTrue(calls.is_file())
                call_lines = calls.read_text(encoding="utf-8").splitlines()
                self.assertIn(f"{launcher} smoke", call_lines)
                self.assertIn(f"{launcher} test-ui", call_lines)
            finally:
                server.terminate()
                try:
                    server.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait()

    @staticmethod
    def free_port() -> int:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    @staticmethod
    def wait_for_tilt_resource(port: int, name: str) -> None:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            result = subprocess.run(
                [
                    "tilt",
                    "get",
                    "uiresource",
                    name,
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port),
                    "--output",
                    "json",
                ],
                check=False,
                capture_output=True,
            )
            if result.returncode == 0:
                return
            time.sleep(0.1)
        raise AssertionError(f"Tilt resource {name} did not become available")

    def test_smoke_keyboard_interrupt_records_an_aborted_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            item = DEV.DevPaths(root=Path(directory), state=Path(directory) / "state")
            DEV.ensure_directories(item)
            snapshot = item.snapshots / "sha"
            snapshot.mkdir()
            DEV.write_state(
                item, DEV.initial_state("http") | {"status": "ready", "running_sha": "sha"}
            )
            urls = DEV.public_urls("http")
            with (
                patch.object(DEV, "require_setup"),
                patch.object(
                    DEV,
                    "ready_instance",
                    return_value=("sha", snapshot, "http", urls),
                ),
                patch.object(DEV, "run_checked", side_effect=KeyboardInterrupt),
                self.assertRaises(KeyboardInterrupt),
            ):
                DEV.run_smoke(item)

            state = DEV.read_state(item)
            self.assertIsNone(state["test"])
            self.assertEqual("aborted", state["last_smoke"]["status"])
            self.assertEqual(130, state["last_smoke"]["exit_code"])

    @staticmethod
    def ui_spec_keys(*titles: str) -> frozenset[tuple[str, int, int, str]]:
        return frozenset(
            ("tests/e2e/example.spec.ts", index, 1, title)
            for index, title in enumerate(titles, start=1)
        )

    @staticmethod
    def ui_report(stats: dict[str, int], *titles: str) -> dict[str, object]:
        return {
            "stats": stats,
            "suites": [
                {
                    "title": "example.spec.ts",
                    "specs": [
                        {
                            "file": "tests/e2e/example.spec.ts",
                            "line": index,
                            "column": 1,
                            "title": title,
                            "tests": [],
                        }
                        for index, title in enumerate(titles, start=1)
                    ],
                }
            ],
        }

    def test_ui_launch_failure_records_a_failed_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            item = DEV.DevPaths(root=Path(directory), state=Path(directory) / "state")
            DEV.ensure_directories(item)
            snapshot = item.snapshots / "sha"
            snapshot.mkdir()
            DEV.write_state(
                item, DEV.initial_state("http") | {"status": "ready", "running_sha": "sha"}
            )
            urls = DEV.public_urls("http")
            with (
                patch.object(DEV, "require_setup"),
                patch.object(
                    DEV,
                    "ready_instance",
                    return_value=("sha", snapshot, "http", urls),
                ),
                patch.object(DEV, "ensure_snapshot_node_modules"),
                patch.object(
                    DEV,
                    "playwright_ui_expected_specs",
                    return_value=self.ui_spec_keys("spec"),
                ),
                patch.object(DEV, "run_checked", side_effect=DEV.DevError("launcher unavailable")),
                self.assertRaises(DEV.DevError),
            ):
                DEV.run_ui(item)

            state = DEV.read_state(item)
            self.assertIsNone(state["test"])
            self.assertEqual("failed", state["last_ui"]["status"])
            self.assertIsNone(state["last_ui"]["exit_code"])
            self.assertIn("launcher unavailable", state["last_ui"]["error"])

    def test_ui_sigint_after_complete_passing_report_records_passed_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            item = DEV.DevPaths(root=Path(directory), state=Path(directory) / "state")
            DEV.ensure_directories(item)
            snapshot = item.snapshots / "sha"
            snapshot.mkdir()
            DEV.write_state(
                item, DEV.initial_state("http") | {"status": "ready", "running_sha": "sha"}
            )
            urls = DEV.public_urls("http")
            report = item.reports / "ui-sha.json"

            def finish_ui(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
                report.write_text(
                    json.dumps(
                        self.ui_report(
                            {
                                "expected": 1,
                                "skipped": 1,
                                "unexpected": 0,
                                "flaky": 0,
                            },
                            "passed spec",
                            "skipped spec",
                        )
                    ),
                    encoding="utf-8",
                )
                return subprocess.CompletedProcess(["playwright"], 130)

            with (
                patch.object(DEV, "require_setup"),
                patch.object(
                    DEV,
                    "ready_instance",
                    return_value=("sha", snapshot, "http", urls),
                ),
                patch.object(DEV, "ensure_snapshot_node_modules"),
                patch.object(
                    DEV,
                    "playwright_ui_expected_specs",
                    return_value=self.ui_spec_keys("passed spec", "skipped spec"),
                ),
                patch.object(DEV, "run_checked", side_effect=finish_ui),
            ):
                DEV.run_ui(item)

            state = DEV.read_state(item)
            self.assertIsNone(state["test"])
            self.assertEqual("passed", state["last_ui"]["status"])
            self.assertEqual(130, state["last_ui"]["exit_code"])

    def test_ui_sigint_with_unexpected_or_all_skipped_report_remains_failed(self) -> None:
        cases = (
            {"expected": 15, "skipped": 2, "unexpected": 1, "flaky": 0},
            {"expected": 0, "skipped": 18, "unexpected": 0, "flaky": 0},
        )
        for stats in cases:
            with self.subTest(stats=stats), tempfile.TemporaryDirectory() as directory:
                item = DEV.DevPaths(root=Path(directory), state=Path(directory) / "state")
                DEV.ensure_directories(item)
                snapshot = item.snapshots / "sha"
                snapshot.mkdir()
                DEV.write_state(
                    item,
                    DEV.initial_state("http") | {"status": "ready", "running_sha": "sha"},
                )
                urls = DEV.public_urls("http")
                report = item.reports / "ui-sha.json"

                def finish_ui(
                    *_args: object,
                    _report: Path = report,
                    _stats: dict[str, int] = stats,
                    **_kwargs: object,
                ) -> subprocess.CompletedProcess[bytes]:
                    _report.write_text(
                        json.dumps(self.ui_report(_stats, "spec")),
                        encoding="utf-8",
                    )
                    return subprocess.CompletedProcess(["playwright"], 130)

                with (
                    patch.object(DEV, "require_setup"),
                    patch.object(
                        DEV,
                        "ready_instance",
                        return_value=("sha", snapshot, "http", urls),
                    ),
                    patch.object(DEV, "ensure_snapshot_node_modules"),
                    patch.object(
                        DEV,
                        "playwright_ui_expected_specs",
                        return_value=self.ui_spec_keys("spec"),
                    ),
                    patch.object(DEV, "run_checked", side_effect=finish_ui),
                    self.assertRaises(DEV.DevError),
                ):
                    DEV.run_ui(item)

                state = DEV.read_state(item)
                self.assertIsNone(state["test"])
                self.assertEqual("failed", state["last_ui"]["status"])
                self.assertEqual(130, state["last_ui"]["exit_code"])

    def test_ui_parent_interrupt_after_complete_report_records_passed_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            item = DEV.DevPaths(root=Path(directory), state=Path(directory) / "state")
            DEV.ensure_directories(item)
            snapshot = item.snapshots / "sha"
            snapshot.mkdir()
            DEV.write_state(
                item, DEV.initial_state("http") | {"status": "ready", "running_sha": "sha"}
            )
            urls = DEV.public_urls("http")
            report = item.reports / "ui-sha.json"

            def interrupt_after_report(
                *_args: object, **_kwargs: object
            ) -> subprocess.CompletedProcess[bytes]:
                report.write_text(
                    json.dumps(
                        self.ui_report(
                            {
                                "expected": 2,
                                "skipped": 0,
                                "unexpected": 0,
                                "flaky": 0,
                            },
                            "first spec",
                            "second spec",
                        )
                    ),
                    encoding="utf-8",
                )
                raise DEV.DevInterrupted("收到停止请求")

            with (
                patch.object(DEV, "require_setup"),
                patch.object(
                    DEV,
                    "ready_instance",
                    return_value=("sha", snapshot, "http", urls),
                ),
                patch.object(DEV, "ensure_snapshot_node_modules"),
                patch.object(
                    DEV,
                    "playwright_ui_expected_specs",
                    return_value=self.ui_spec_keys("first spec", "second spec"),
                ),
                patch.object(DEV, "run_checked", side_effect=interrupt_after_report),
            ):
                DEV.run_ui(item)

            state = DEV.read_state(item)
            self.assertIsNone(state["test"])
            self.assertEqual("passed", state["last_ui"]["status"])
            self.assertEqual(130, state["last_ui"]["exit_code"])

    def test_ui_parent_interrupt_with_partial_report_remains_aborted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            item = DEV.DevPaths(root=Path(directory), state=Path(directory) / "state")
            DEV.ensure_directories(item)
            snapshot = item.snapshots / "sha"
            snapshot.mkdir()
            DEV.write_state(
                item, DEV.initial_state("http") | {"status": "ready", "running_sha": "sha"}
            )
            urls = DEV.public_urls("http")
            report = item.reports / "ui-sha.json"

            def interrupt_after_partial_report(
                *_args: object, **_kwargs: object
            ) -> subprocess.CompletedProcess[bytes]:
                report.write_text(
                    json.dumps(
                        self.ui_report(
                            {
                                "expected": 1,
                                "skipped": 0,
                                "unexpected": 0,
                                "flaky": 0,
                            },
                            "first spec",
                        )
                    ),
                    encoding="utf-8",
                )
                raise DEV.DevInterrupted("收到停止请求")

            with (
                patch.object(DEV, "require_setup"),
                patch.object(
                    DEV,
                    "ready_instance",
                    return_value=("sha", snapshot, "http", urls),
                ),
                patch.object(DEV, "ensure_snapshot_node_modules"),
                patch.object(
                    DEV,
                    "playwright_ui_expected_specs",
                    return_value=self.ui_spec_keys("first spec", "second spec"),
                ),
                patch.object(DEV, "run_checked", side_effect=interrupt_after_partial_report),
                self.assertRaises(DEV.DevInterrupted),
            ):
                DEV.run_ui(item)

            state = DEV.read_state(item)
            self.assertIsNone(state["test"])
            self.assertEqual("aborted", state["last_ui"]["status"])
            self.assertEqual(130, state["last_ui"]["exit_code"])

    def test_ui_sigint_does_not_reuse_a_stale_passing_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            item = DEV.DevPaths(root=Path(directory), state=Path(directory) / "state")
            DEV.ensure_directories(item)
            snapshot = item.snapshots / "sha"
            snapshot.mkdir()
            DEV.write_state(
                item, DEV.initial_state("http") | {"status": "ready", "running_sha": "sha"}
            )
            urls = DEV.public_urls("http")
            report = item.reports / "ui-sha.json"
            report.write_text(
                json.dumps(
                    self.ui_report(
                        {
                            "expected": 1,
                            "skipped": 0,
                            "unexpected": 0,
                            "flaky": 0,
                        },
                        "spec",
                    )
                ),
                encoding="utf-8",
            )

            with (
                patch.object(DEV, "require_setup"),
                patch.object(
                    DEV,
                    "ready_instance",
                    return_value=("sha", snapshot, "http", urls),
                ),
                patch.object(DEV, "ensure_snapshot_node_modules"),
                patch.object(
                    DEV,
                    "playwright_ui_expected_specs",
                    return_value=self.ui_spec_keys("spec"),
                ),
                patch.object(
                    DEV,
                    "run_checked",
                    return_value=subprocess.CompletedProcess(["playwright"], 130),
                ),
                self.assertRaises(DEV.DevError),
            ):
                DEV.run_ui(item)

            state = DEV.read_state(item)
            self.assertIsNone(state["test"])
            self.assertEqual("failed", state["last_ui"]["status"])
            self.assertEqual(130, state["last_ui"]["exit_code"])

    def test_failed_tilt_stage_stops_new_target_services(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            item = DEV.DevPaths(root=Path(directory), state=Path(directory) / "state")
            DEV.ensure_directories(item)
            snapshot = item.snapshots / "new"
            snapshot.mkdir()
            with (
                patch.object(DEV, "archive_main", return_value=snapshot),
                patch.object(DEV, "render_gateway_config"),
                patch.object(DEV, "build_target", return_value=item.logs / "build-new.log"),
                patch.object(DEV, "run_checked", return_value=subprocess.CompletedProcess([], 0)),
                patch.object(DEV, "stop_tilt") as stopped,
                patch.object(DEV, "compose_down") as down,
                patch.object(DEV, "start_tilt"),
                patch.object(DEV, "wait_until_ready", return_value=(False, "migration")),
            ):
                self.assertFalse(DEV.update_to(item, sha="new", protocol="http"))

            self.assertEqual(2, stopped.call_count)
            self.assertEqual(2, down.call_count)
            self.assertEqual("migration", DEV.read_state(item)["failure"]["phase"])

    def test_failed_migration_never_starts_new_target_services(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            item = DEV.DevPaths(root=Path(directory), state=Path(directory) / "state")
            DEV.ensure_directories(item)
            snapshot = item.snapshots / "new"
            snapshot.mkdir()
            DEV.write_state(
                item, DEV.initial_state("http") | {"status": "ready", "running_sha": "old"}
            )
            with (
                patch.object(DEV, "archive_main", return_value=snapshot),
                patch.object(DEV, "render_gateway_config"),
                patch.object(DEV, "build_target", return_value=item.logs / "build-new.log"),
                patch.object(DEV, "stop_tilt"),
                patch.object(DEV, "compose_down"),
                patch.object(
                    DEV, "run_checked", return_value=subprocess.CompletedProcess([], 1)
                ) as migration,
                patch.object(DEV, "start_tilt") as started,
                patch.object(DEV, "wait_until_ready", return_value=(False, "migration")),
            ):
                self.assertFalse(DEV.update_to(item, sha="new", protocol="http"))

            started.assert_not_called()
            self.assertIn("center-migrate", migration.call_args.args[0])
            state = DEV.read_state(item)
            self.assertEqual("failed", state["status"])
            self.assertIsNone(state["running_sha"])
            self.assertEqual("migration", state["failure"]["phase"])

    def test_successful_migration_precedes_target_start(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            item = DEV.DevPaths(root=Path(directory), state=Path(directory) / "state")
            DEV.ensure_directories(item)
            snapshot = item.snapshots / "new"
            snapshot.mkdir()

            def migration_done(
                *_args: object, **_kwargs: object
            ) -> subprocess.CompletedProcess[bytes]:
                started.assert_not_called()
                return subprocess.CompletedProcess([], 0)

            with (
                patch.object(DEV, "archive_main", return_value=snapshot),
                patch.object(DEV, "render_gateway_config"),
                patch.object(DEV, "build_target", return_value=item.logs / "build-new.log"),
                patch.object(DEV, "stop_tilt"),
                patch.object(DEV, "compose_down"),
                patch.object(DEV, "run_checked", side_effect=migration_done) as migration,
                patch.object(DEV, "start_tilt") as started,
                patch.object(DEV, "wait_until_ready", return_value=(True, None)),
            ):
                self.assertTrue(DEV.update_to(item, sha="new", protocol="http"))

            self.assertIn("center-migrate", migration.call_args.args[0])
            started.assert_called_once()
            state = DEV.read_state(item)
            self.assertEqual("ready", state["status"])
            self.assertEqual("new", state["running_sha"])

    def test_build_failure_marks_old_test_results_stale_immediately(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            item = DEV.DevPaths(root=Path(directory), state=Path(directory) / "state")
            DEV.ensure_directories(item)
            snapshot = item.snapshots / "new"
            snapshot.mkdir()
            state = DEV.initial_state("http") | {
                "status": "ready",
                "target_sha": "old",
                "running_sha": "old",
                "last_smoke": {"tested_sha": "old", "status": "passed"},
                "last_ui": {"tested_sha": "old", "status": "passed"},
            }
            DEV.write_state(item, state)
            with (
                patch.object(DEV, "archive_main", return_value=snapshot),
                patch.object(DEV, "render_gateway_config"),
                patch.object(DEV, "build_target", return_value=None),
            ):
                self.assertFalse(DEV.update_to(item, sha="new", protocol="http"))

            updated = DEV.read_state(item)
            self.assertEqual("new", updated["target_sha"])
            self.assertEqual("failed", updated["status"])
            self.assertEqual("stale", updated["last_smoke"]["status"])
            self.assertEqual("stale", updated["last_ui"]["status"])

    def test_port_preflight_reports_the_occupied_service_port(self) -> None:
        with socket.socket() as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen()
            port = int(occupied.getsockname()[1])
            with self.assertRaises(DEV.DevError) as failure:
                DEV.require_ports({"测试服务": port})

        self.assertIn(f"测试服务={port}", str(failure.exception))

    def test_launcher_pid_uses_lifecycle_lock_instead_of_pid_liveness(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "launcher.pid"
            first = DEV.LauncherPid(path)
            second = DEV.LauncherPid(path)
            first.acquire()
            try:
                with self.assertRaises(DEV.DevError):
                    second.acquire()
            finally:
                first.release()
            path.write_text("999999", encoding="ascii")
            second.acquire()
            second.release()
            self.assertFalse(path.exists())

    @unittest.skipUnless(os.name != "nt", "需要 Linux /proc")
    def test_signal_launcher_stops_a_matching_real_process(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / "scripts" / "dev.py"
            script.parent.mkdir()
            script.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
            item = DEV.DevPaths(root=root, state=root / "state")
            owner = DEV.LauncherPid(item.launcher_pid)
            owner.acquire()
            child = subprocess.Popen([sys.executable, str(script), "run"], cwd=root)
            try:
                signaled = False
                last_error: DEV.DevError | None = None
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline and child.poll() is None:
                    try:
                        signaled = DEV.signal_launcher(item, child.pid, signal.SIGTERM)
                    except DEV.DevError as error:
                        last_error = error
                    if signaled:
                        break
                    time.sleep(0.01)
                if not signaled and last_error is not None:
                    self.fail(str(last_error))
                self.assertTrue(signaled)
                child.wait(timeout=5)
                self.assertEqual(-signal.SIGTERM, child.returncode)
            finally:
                if child.poll() is None:
                    child.terminate()
                    child.wait(timeout=5)
                owner.release()

    def test_down_cancels_a_running_manual_test_before_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            item = DEV.DevPaths(root=Path(directory), state=Path(directory) / "state")
            DEV.ensure_directories(item)
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
            DEV.write_state(
                item,
                DEV.initial_state("https")
                | {
                    "test": {
                        "kind": "smoke",
                        "pid": child.pid,
                        "status": "running",
                    }
                },
            )
            try:
                with patch.object(DEV, "test_process_matches", return_value=True):
                    DEV.cancel_test(item, timeout=2)
                child.wait(timeout=2)
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait()
            self.assertIsNotNone(child.returncode)

    def test_stop_event_terminates_a_running_command(self) -> None:
        stopping = Event()
        timer = threading.Timer(0.2, stopping.set)
        timer.start()
        try:
            with self.assertRaises(DEV.DevInterrupted):
                DEV.run_checked(
                    [sys.executable, "-c", "import time; time.sleep(30)"],
                    stop_event=stopping,
                )
        finally:
            timer.cancel()
        with self.assertRaises(DEV.DevError):
            DEV.run_checked(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                stop_event=Event(),
                timeout=0.1,
            )

    def test_tilt_resource_failure_is_observable_before_sample_report(self) -> None:
        payload = {
            "status": {
                "updateStatus": "error",
                "buildHistory": [{"error": "seed failed before report"}],
            }
        }
        result = subprocess.CompletedProcess([], 0, stdout=json.dumps(payload).encode(), stderr=b"")
        with patch.object(DEV, "run_checked", return_value=result):
            self.assertEqual("seed failed before report", DEV.tilt_resource_failure("sample-data"))

    def test_failed_required_service_is_reported(self) -> None:
        self.assertTrue(
            DEV.failed_service({"gateway": {"state": "exited", "exit_code": 1}}, "gateway")
        )
        self.assertFalse(
            DEV.failed_service({"gateway": {"state": "running", "exit_code": 0}}, "gateway")
        )

    def test_status_failure_is_nonzero_even_when_compose_is_unavailable(self) -> None:
        self.assertEqual(
            1,
            DEV.status_exit_code({"status": "failed", "api_liveness": {"ok": False}}, {}),
        )

    def test_persisted_ready_with_unhealthy_service_reports_degraded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            item = DEV.DevPaths(root=root, state=root / ".tmp")
            DEV.ensure_directories(item)
            state = DEV.initial_state("http")
            state.update({"status": "ready", "running_sha": "a" * 40})
            DEV.write_state(item, state)
            item.setup_file.write_text("{}\n", encoding="utf-8")
            rows = [
                {"Service": name, "State": "running", "Health": "healthy"}
                for name in sorted(DEV._REQUIRED_READY_SERVICES)
            ]
            rows = [row for row in rows if row["Service"] != "worker"] + [
                {"Service": "worker", "State": "exited", "Health": ""}
            ]
            output = io.StringIO()
            with (
                patch.object(DEV, "service_rows", return_value=(rows, None)),
                patch.object(DEV, "http_probe", return_value={"ok": True}),
                contextlib.redirect_stdout(output),
            ):
                result = DEV.status(item)

        self.assertEqual(1, result)
        self.assertIn('"status": "degraded"', output.getvalue())

    def test_status_command_rejects_a_failed_instance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            item = DEV.DevPaths(root=Path(directory), state=Path(directory) / ".tmp")
            DEV.ensure_directories(item)
            state = DEV.initial_state("https")
            state.update({"status": "failed", "failure": {"phase": "readiness"}})
            DEV.write_state(item, state)
            output = io.StringIO()
            with (
                patch.object(DEV, "http_probe", return_value={"ok": False}),
                contextlib.redirect_stdout(output),
            ):
                result = DEV.status(item)

        self.assertEqual(1, result)
        self.assertIn('"status": "failed"', output.getvalue())

    def test_old_test_result_is_marked_stale_for_a_new_sha(self) -> None:
        entry = {"tested_sha": "old", "status": "passed", "report": "/reports/old.json"}

        actual = DEV.stale_test_entry(entry, current_sha="new")

        self.assertIsInstance(actual, dict)
        assert isinstance(actual, dict)
        self.assertEqual("old", actual["tested_sha"])
        self.assertEqual("stale", actual["status"])
        self.assertEqual("/reports/old.json", actual["report"])
        self.assertEqual(True, actual["needs_rerun"])
        self.assertEqual("new", actual["current_sha"])
        self.assertIsInstance(actual["stale_at"], str)

    def test_task_branch_cannot_start_the_fixed_instance(self) -> None:
        item = DEV.DevPaths(root=Path("/repo"), state=Path("/state"))
        branch = subprocess.CompletedProcess(
            [], 0, stdout=b"issue-119-implementation\n", stderr=b""
        )
        with patch.object(DEV, "run_checked", return_value=branch), self.assertRaises(DEV.DevError):
            DEV.require_main_checkout(item)

    def test_main_checkout_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            item = DEV.DevPaths(root=root, state=root / ".tmp")
            branch = subprocess.CompletedProcess([], 0, stdout=b"main\n", stderr=b"")
            top_level = subprocess.CompletedProcess([], 0, stdout=f"{root}\n".encode(), stderr=b"")
            with patch.object(DEV, "run_checked", side_effect=[branch, top_level]):
                DEV.require_main_checkout(item)


class ExternalTlsFixture:
    """在临时目录生成合成外部 TLS 材料，仅用于测试，不写入仓库。"""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.root_crt = directory / "root.crt"
        self.root_key = directory / "root.key"
        self.other_ca_crt = directory / "other-ca.crt"
        self.other_ca_key = directory / "other-ca.key"
        self.intermediate_crt = directory / "intermediate.crt"
        self.intermediate_key = directory / "intermediate.key"
        self.leaf_crt = directory / "leaf.crt"
        self.leaf_key = directory / "leaf.key"
        self.chain = directory / "chain.pem"
        self.wrong_host_crt = directory / "wrong-host.crt"
        self.wrong_host_chain = directory / "wrong-host-chain.pem"
        self.wrong_host_key = directory / "wrong-host.key"
        self.expired_crt = directory / "expired.crt"
        self.expired_chain = directory / "expired-chain.pem"
        self.future_crt = directory / "future.crt"
        self.future_chain = directory / "future-chain.pem"
        self.encrypted_key = directory / "encrypted.key"
        self.malformed_crt = directory / "malformed.crt"
        self.trust_dir = directory / "trust"

    def openssl(self, *arguments: str) -> None:
        subprocess.run(["openssl", *arguments], check=True, capture_output=True, text=True)

    def extension_file(self, name: str, text: str) -> Path:
        path = self.directory / name
        path.write_text(text, encoding="ascii")
        return path

    def build(self) -> None:
        ca_extensions = self.extension_file(
            "ca.ext",
            "basicConstraints=critical,CA:true,pathlen:0\nkeyUsage=critical,keyCertSign,cRLSign\n",
        )
        server_extensions = self.extension_file(
            "server.ext",
            "subjectAltName=DNS:localhost,IP:127.0.0.1\nextendedKeyUsage=serverAuth\n",
        )
        wrong_extensions = self.extension_file(
            "wrong.ext", "subjectAltName=DNS:other.example\nextendedKeyUsage=serverAuth\n"
        )
        self.openssl(
            "req",
            "-x509",
            "-new",
            "-nodes",
            "-newkey",
            "rsa:2048",
            "-keyout",
            str(self.root_key),
            "-out",
            str(self.root_crt),
            "-days",
            "825",
            "-subj",
            "/CN=Test Root CA",
            "-addext",
            "basicConstraints=critical,CA:true",
            "-addext",
            "keyUsage=critical,keyCertSign,cRLSign",
        )
        self.openssl(
            "req",
            "-x509",
            "-new",
            "-nodes",
            "-newkey",
            "rsa:2048",
            "-keyout",
            str(self.other_ca_key),
            "-out",
            str(self.other_ca_crt),
            "-days",
            "825",
            "-subj",
            "/CN=Other Root CA",
            "-addext",
            "basicConstraints=critical,CA:true",
            "-addext",
            "keyUsage=critical,keyCertSign,cRLSign",
        )
        intermediate_csr = self.directory / "intermediate.csr"
        self.openssl(
            "req",
            "-new",
            "-nodes",
            "-newkey",
            "rsa:2048",
            "-keyout",
            str(self.intermediate_key),
            "-out",
            str(intermediate_csr),
            "-subj",
            "/CN=Test Intermediate CA",
        )
        self.openssl(
            "x509",
            "-req",
            "-in",
            str(intermediate_csr),
            "-CA",
            str(self.root_crt),
            "-CAkey",
            str(self.root_key),
            "-CAcreateserial",
            "-out",
            str(self.intermediate_crt),
            "-days",
            "825",
            "-sha256",
            "-extfile",
            str(ca_extensions),
        )
        leaf_csr = self.directory / "leaf.csr"
        self.openssl(
            "req",
            "-new",
            "-nodes",
            "-newkey",
            "rsa:2048",
            "-keyout",
            str(self.leaf_key),
            "-out",
            str(leaf_csr),
            "-subj",
            "/CN=localhost",
        )
        self.openssl(
            "x509",
            "-req",
            "-in",
            str(leaf_csr),
            "-CA",
            str(self.intermediate_crt),
            "-CAkey",
            str(self.intermediate_key),
            "-CAcreateserial",
            "-out",
            str(self.leaf_crt),
            "-days",
            "825",
            "-sha256",
            "-extfile",
            str(server_extensions),
        )
        self.chain.write_bytes(self.leaf_crt.read_bytes() + self.intermediate_crt.read_bytes())
        wrong_csr = self.directory / "wrong.csr"
        self.openssl(
            "req",
            "-new",
            "-nodes",
            "-newkey",
            "rsa:2048",
            "-keyout",
            str(self.wrong_host_key),
            "-out",
            str(wrong_csr),
            "-subj",
            "/CN=other.example",
        )
        self.openssl(
            "x509",
            "-req",
            "-in",
            str(wrong_csr),
            "-CA",
            str(self.intermediate_crt),
            "-CAkey",
            str(self.intermediate_key),
            "-CAcreateserial",
            "-out",
            str(self.wrong_host_crt),
            "-days",
            "825",
            "-sha256",
            "-extfile",
            str(wrong_extensions),
        )
        self.wrong_host_chain.write_bytes(
            self.wrong_host_crt.read_bytes() + self.intermediate_crt.read_bytes()
        )
        self.openssl(
            "x509",
            "-req",
            "-in",
            str(leaf_csr),
            "-CA",
            str(self.intermediate_crt),
            "-CAkey",
            str(self.intermediate_key),
            "-CAcreateserial",
            "-out",
            str(self.expired_crt),
            "-days",
            "-1",
            "-sha256",
            "-extfile",
            str(server_extensions),
        )
        self.expired_chain.write_bytes(
            self.expired_crt.read_bytes() + self.intermediate_crt.read_bytes()
        )
        self.build_future_certificate(leaf_csr, server_extensions)
        self.future_chain.write_bytes(
            self.future_crt.read_bytes() + self.intermediate_crt.read_bytes()
        )
        self.openssl(
            "pkey",
            "-in",
            str(self.leaf_key),
            "-aes256",
            "-passout",
            "pass:secret",
            "-out",
            str(self.encrypted_key),
        )
        self.malformed_crt.write_text("not a certificate\n", encoding="ascii")
        self.trust_dir.mkdir()
        (self.trust_dir / "root.crt").write_bytes(self.root_crt.read_bytes())
        self.openssl("rehash", str(self.trust_dir))

    def build_future_certificate(self, leaf_csr: Path, server_extensions: Path) -> None:
        ca_directory = self.directory / "ca"
        (ca_directory / "newcerts").mkdir(parents=True, exist_ok=True)
        (ca_directory / "index.txt").touch()
        (ca_directory / "serial").write_text("1000\n", encoding="ascii")
        config = self.directory / "ca.cnf"
        config.write_text(
            "[ca]\n"
            "default_ca = CA_default\n"
            "[CA_default]\n"
            f"dir = {ca_directory}\n"
            "database = $dir/index.txt\n"
            "new_certs_dir = $dir/newcerts\n"
            "serial = $dir/serial\n"
            f"certificate = {self.intermediate_crt}\n"
            f"private_key = {self.intermediate_key}\n"
            "default_md = sha256\n"
            "policy = policy_any\n"
            "copy_extensions = copy\n"
            "[policy_any]\n"
            "commonName = supplied\n",
            encoding="ascii",
        )
        self.openssl(
            "ca",
            "-batch",
            "-config",
            str(config),
            "-in",
            str(leaf_csr),
            "-out",
            str(self.future_crt),
            "-startdate",
            "20990101000000Z",
            "-enddate",
            "20991231235959Z",
            "-extfile",
            str(server_extensions),
        )


def assert_missing_external_certificate_rejected(module: ModuleType) -> None:
    """HTTPS 缺失显式外部 cert/key 时必须拒绝且不生成任何 TLS 材料。"""
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        item = module.DevPaths(root=root, state=root / ".state")
        module.ensure_directories(item)
        template = root / "deploy" / "dev" / "nginx.conf"
        template.parent.mkdir(parents=True, exist_ok=True)
        template.write_text(
            "server {\n"
            "    listen 8443__NVSOP_LISTEN_SUFFIX__;\n"
            "    # __NVSOP_TLS_DIRECTIVES__\n"
            "    server_name localhost nginx;\n"
            "}\n",
            encoding="utf-8",
        )
        environment = {
            "NVSOP_DEV_TLS_CERT_FILE": "",
            "NVSOP_DEV_TLS_KEY_FILE": "",
            "NVSOP_DEV_TLS_CA_FILE": "",
        }
        with patch.dict(os.environ, environment, clear=False):
            os.environ.pop("SSL_CERT_FILE", None)
            try:
                module.render_gateway_config(item, source=root, protocol="https")
            except module.DevError:
                pass
            else:
                raise AssertionError("HTTPS 缺失外部 cert/key 时未拒绝")
        generated = sorted(path.name for path in item.tls.iterdir())
        if generated:
            raise AssertionError(f"HTTPS 缺失外部 cert/key 时仍生成 TLS 材料：{generated}")


class DevExternalTlsTest(unittest.TestCase):
    """显式外部 TLS 材料：缺失/损坏/不匹配必须拒绝，有效材料只读暂存。"""

    module = DEV

    @classmethod
    def setUpClass(cls) -> None:
        cls._fixture_directory = tempfile.TemporaryDirectory()
        cls.tls = ExternalTlsFixture(Path(cls._fixture_directory.name))
        cls.tls.build()

    @classmethod
    def tearDownClass(cls) -> None:
        cls._fixture_directory.cleanup()

    def make_item(self, directory: str) -> DEV.DevPaths:
        root = Path(directory)
        item = self.module.DevPaths(root=root, state=root / ".state")
        self.module.ensure_directories(item)
        template = root / "deploy" / "dev" / "nginx.conf"
        template.parent.mkdir(parents=True, exist_ok=True)
        template.write_text(
            "server {\n"
            "    listen 8443__NVSOP_LISTEN_SUFFIX__;\n"
            "    # __NVSOP_TLS_DIRECTIVES__\n"
            "    server_name localhost nginx;\n"
            "}\n",
            encoding="utf-8",
        )
        return item

    def render_https(
        self,
        item: DEV.DevPaths,
        root: Path,
        *,
        cert: Path | None = None,
        key: Path | None = None,
        ca: Path | None = None,
        ssl_cert_file: Path | None = None,
        ssl_cert_dir: Path | None = None,
    ) -> Path | None:
        environment = {
            "NVSOP_DEV_TLS_CERT_FILE": str(cert) if cert is not None else "",
            "NVSOP_DEV_TLS_KEY_FILE": str(key) if key is not None else "",
            "NVSOP_DEV_TLS_CA_FILE": str(ca) if ca is not None else "",
        }
        with patch.dict(os.environ, environment, clear=False):
            for name, value in (("SSL_CERT_FILE", ssl_cert_file), ("SSL_CERT_DIR", ssl_cert_dir)):
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = str(value)
            return self.module.render_gateway_config(item, source=root, protocol="https")

    def assert_https_rejected(
        self, *, cert: Path, key: Path, ca: Path | None = None, reason: str | None = None
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            item = self.make_item(directory)
            with self.assertRaises(self.module.DevError) as caught:
                self.render_https(item, root, cert=cert, key=key, ca=ca)
            if reason is not None:
                self.assertIn(reason, str(caught.exception))
            self.assertEqual([], sorted(path.name for path in item.tls.iterdir()))

    def test_https_without_external_cert_key_is_rejected_without_generating_material(self) -> None:
        assert_missing_external_certificate_rejected(self.module)

    def test_https_accepts_valid_external_full_chain_and_stages_untouched_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            item = self.make_item(directory)
            chain_before = self.tls.chain.read_bytes()
            key_before = self.tls.leaf_key.read_bytes()
            ca_before = self.tls.root_crt.read_bytes()
            ca = self.render_https(
                item, root, cert=self.tls.chain, key=self.tls.leaf_key, ca=self.tls.root_crt
            )
            self.assertEqual(item.tls / "ca.crt", ca)
            self.assertEqual(chain_before, self.tls.chain.read_bytes())
            self.assertEqual(key_before, self.tls.leaf_key.read_bytes())
            self.assertEqual(ca_before, self.tls.root_crt.read_bytes())
            self.assertEqual(chain_before, (item.tls / "dev.crt").read_bytes())
            self.assertEqual(key_before, (item.tls / "dev.key").read_bytes())
            self.assertEqual(ca_before, (item.tls / "ca.crt").read_bytes())
            self.assertEqual(0o600, (item.tls / "dev.key").stat().st_mode & 0o777)
            self.assertEqual(0o644, (item.tls / "dev.crt").stat().st_mode & 0o777)
            self.assertEqual(item.tls / "ca.crt", self.module.configured_ca_certificate(item))
            rendered = (item.state / "nginx.conf").read_text(encoding="utf-8")
            self.assertIn("listen 8443 ssl;", rendered)
            self.assertIn("ssl_certificate /etc/nginx/tls/dev.crt;", rendered)
            self.assertIn("ssl_certificate_key /etc/nginx/tls/dev.key;", rendered)

    def test_https_without_custom_ca_uses_system_trust_without_staged_ca(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            item = self.make_item(directory)
            with patch.dict(os.environ, {"SSL_CERT_DIR": str(self.tls.trust_dir)}, clear=False):
                ca = self.render_https(
                    item,
                    root,
                    cert=self.tls.chain,
                    key=self.tls.leaf_key,
                    ssl_cert_dir=self.tls.trust_dir,
                )
                self.assertIsNone(ca)
                self.assertIsNone(self.module.configured_ca_certificate(item))
                self.assertFalse((item.tls / "ca.crt").exists())
                bundle = item.tls / "system-ca.crt"
                self.assertTrue(bundle.is_file())
                self.assertIn(b"BEGIN CERTIFICATE", bundle.read_bytes())
                self.assertEqual(0o644, bundle.stat().st_mode & 0o777)
                environment = self.module.runtime_environment(
                    item, sha="abc", source=root, protocol="https"
                )
            self.assertNotIn("SSL_CERT_FILE", environment)
            self.assertEqual(str(self.tls.trust_dir), environment["SSL_CERT_DIR"])
            self.assertEqual(
                "--cacert /etc/nginx/tls/system-ca.crt",
                environment["NVSOP_DEV_TLS_HEALTHCHECK_ARGS"],
            )
            verified = subprocess.run(
                [
                    "openssl",
                    "verify",
                    "-no-CApath",
                    "-no-CAstore",
                    "-CAfile",
                    str(bundle),
                    "-untrusted",
                    str(self.tls.chain),
                    "-purpose",
                    "sslserver",
                    "-verify_hostname",
                    "localhost",
                    str(self.tls.chain),
                ],
                check=False,
                capture_output=True,
            )
            self.assertEqual(0, verified.returncode, verified.stderr.decode())

    def test_https_without_custom_ca_preserves_user_ssl_cert_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            item = self.make_item(directory)
            user_bundle = root / "user-system-bundle.pem"
            user_bundle.write_bytes(self.tls.root_crt.read_bytes())
            self.render_https(
                item,
                root,
                cert=self.tls.chain,
                key=self.tls.leaf_key,
                ssl_cert_file=user_bundle,
            )
            self.assertTrue((item.tls / "system-ca.crt").is_file())
            with patch.dict(os.environ, {"SSL_CERT_FILE": str(user_bundle)}, clear=False):
                environment = self.module.runtime_environment(
                    item, sha="abc", source=root, protocol="https"
                )
            self.assertEqual(str(user_bundle), environment["SSL_CERT_FILE"])
            self.assertEqual(
                "--cacert /etc/nginx/tls/system-ca.crt",
                environment["NVSOP_DEV_TLS_HEALTHCHECK_ARGS"],
            )

    def test_https_system_bundle_excludes_unindexed_certificates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            item = self.make_item(directory)
            trust_dir = root / "trust"
            shutil.copytree(self.tls.trust_dir, trust_dir)
            (trust_dir / "untrusted.pem").write_bytes(self.tls.other_ca_crt.read_bytes())

            self.render_https(
                item, root, cert=self.tls.chain, key=self.tls.leaf_key, ssl_cert_dir=trust_dir
            )

            bundle = item.tls / "system-ca.crt"
            self.assertTrue(bundle.is_file())
            indexed = subprocess.run(
                [
                    "openssl",
                    "verify",
                    "-no-CApath",
                    "-no-CAstore",
                    "-CAfile",
                    str(bundle),
                    str(self.tls.root_crt),
                ],
                check=False,
                capture_output=True,
            )
            self.assertEqual(0, indexed.returncode, indexed.stderr.decode())
            unindexed = subprocess.run(
                [
                    "openssl",
                    "verify",
                    "-no-CApath",
                    "-no-CAstore",
                    "-CAfile",
                    str(bundle),
                    str(self.tls.other_ca_crt),
                ],
                check=False,
                capture_output=True,
            )
            self.assertNotEqual(0, unindexed.returncode)

    def test_https_system_bundle_reads_colon_separated_trust_directories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            item = self.make_item(directory)
            second = root / "second"
            second.mkdir()
            shutil.copy(self.tls.other_ca_crt, second / "other.pem")
            subprocess.run(["openssl", "rehash", str(second)], check=True, capture_output=True)
            combined = Path(f"{self.tls.trust_dir}{os.pathsep}{second}")

            self.render_https(
                item, root, cert=self.tls.chain, key=self.tls.leaf_key, ssl_cert_dir=combined
            )

            bundle = item.tls / "system-ca.crt"
            self.assertTrue(bundle.is_file())
            for certificate in (self.tls.root_crt, self.tls.other_ca_crt):
                verified = subprocess.run(
                    [
                        "openssl",
                        "verify",
                        "-no-CApath",
                        "-no-CAstore",
                        "-CAfile",
                        str(bundle),
                        str(certificate),
                    ],
                    check=False,
                    capture_output=True,
                )
                self.assertEqual(0, verified.returncode, verified.stderr.decode())

    def test_https_system_bundle_joins_cafile_without_trailing_newline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            item = self.make_item(directory)
            cafile = root / "system-cafile.pem"
            cafile.write_bytes(self.tls.other_ca_crt.read_bytes().rstrip(b"\n"))
            self.assertFalse(cafile.read_bytes().endswith(b"\n"))
            capath = root / "trust"
            shutil.copytree(self.tls.trust_dir, capath)
            cafile_before = cafile.read_bytes()
            entries_before = {
                path.name: path.read_bytes() for path in capath.iterdir() if path.is_file()
            }

            self.render_https(
                item,
                root,
                cert=self.tls.chain,
                key=self.tls.leaf_key,
                ssl_cert_file=cafile,
                ssl_cert_dir=capath,
            )

            bundle = item.tls / "system-ca.crt"
            self.assertTrue(bundle.is_file())
            ssl.create_default_context(cafile=str(bundle))
            verified = subprocess.run(
                [
                    "openssl",
                    "verify",
                    "-no-CApath",
                    "-no-CAstore",
                    "-CAfile",
                    str(bundle),
                    "-untrusted",
                    str(self.tls.chain),
                    "-purpose",
                    "sslserver",
                    "-verify_hostname",
                    "localhost",
                    str(self.tls.chain),
                ],
                check=False,
                capture_output=True,
            )
            self.assertEqual(0, verified.returncode, verified.stderr.decode())
            self.assertEqual(cafile_before, cafile.read_bytes())
            self.assertEqual(
                entries_before,
                {path.name: path.read_bytes() for path in capath.iterdir() if path.is_file()},
            )

    def test_https_system_bundle_preserves_explicitly_empty_cafile(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            item = self.make_item(directory)
            capath = root / "trust"
            shutil.copytree(self.tls.trust_dir, capath)
            default_root = self.tls.other_ca_crt
            fake_paths = ssl.get_default_verify_paths()._replace(
                openssl_cafile=str(default_root), openssl_capath=str(capath)
            )
            environment = {
                "NVSOP_DEV_TLS_CERT_FILE": str(self.tls.chain),
                "NVSOP_DEV_TLS_KEY_FILE": str(self.tls.leaf_key),
                "NVSOP_DEV_TLS_CA_FILE": "",
                "SSL_CERT_FILE": "",
                "SSL_CERT_DIR": str(capath),
            }
            with (
                patch.object(DEV.ssl, "get_default_verify_paths", return_value=fake_paths),
                patch.dict(os.environ, environment, clear=False),
            ):
                self.module.render_gateway_config(item, source=root, protocol="https")

            bundle = item.tls / "system-ca.crt"
            self.assertTrue(bundle.is_file())
            trusted = subprocess.run(
                [
                    "openssl",
                    "verify",
                    "-no-CApath",
                    "-no-CAstore",
                    "-CAfile",
                    str(bundle),
                    str(self.tls.root_crt),
                ],
                check=False,
                capture_output=True,
            )
            self.assertEqual(0, trusted.returncode, trusted.stderr.decode())
            broadened = subprocess.run(
                [
                    "openssl",
                    "verify",
                    "-no-CApath",
                    "-no-CAstore",
                    "-CAfile",
                    str(bundle),
                    str(default_root),
                ],
                check=False,
                capture_output=True,
            )
            self.assertNotEqual(0, broadened.returncode)

    def test_https_system_bundle_preserves_explicitly_empty_trust_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            item = self.make_item(directory)
            cafile = root / "system-cafile.pem"
            cafile.write_bytes(self.tls.root_crt.read_bytes())
            default_capath = root / "default-trust"
            default_capath.mkdir()
            shutil.copy(self.tls.other_ca_crt, default_capath / "other.pem")
            subprocess.run(
                ["openssl", "rehash", str(default_capath)], check=True, capture_output=True
            )
            fake_paths = ssl.get_default_verify_paths()._replace(
                openssl_cafile=str(cafile), openssl_capath=str(default_capath)
            )
            environment = {
                "NVSOP_DEV_TLS_CERT_FILE": str(self.tls.chain),
                "NVSOP_DEV_TLS_KEY_FILE": str(self.tls.leaf_key),
                "NVSOP_DEV_TLS_CA_FILE": "",
                "SSL_CERT_FILE": str(cafile),
                "SSL_CERT_DIR": "",
            }
            with (
                patch.object(DEV.ssl, "get_default_verify_paths", return_value=fake_paths),
                patch.dict(os.environ, environment, clear=False),
            ):
                self.module.render_gateway_config(item, source=root, protocol="https")

            bundle = item.tls / "system-ca.crt"
            self.assertTrue(bundle.is_file())
            trusted = subprocess.run(
                [
                    "openssl",
                    "verify",
                    "-no-CApath",
                    "-no-CAstore",
                    "-CAfile",
                    str(bundle),
                    str(self.tls.root_crt),
                ],
                check=False,
                capture_output=True,
            )
            self.assertEqual(0, trusted.returncode, trusted.stderr.decode())
            broadened = subprocess.run(
                [
                    "openssl",
                    "verify",
                    "-no-CApath",
                    "-no-CAstore",
                    "-CAfile",
                    str(bundle),
                    str(self.tls.other_ca_crt),
                ],
                check=False,
                capture_output=True,
            )
            self.assertNotEqual(0, broadened.returncode)

    def test_https_runtime_environment_uses_explicit_custom_ca(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            item = self.make_item(directory)
            self.render_https(
                item, root, cert=self.tls.chain, key=self.tls.leaf_key, ca=self.tls.root_crt
            )
            environment = self.module.runtime_environment(
                item, sha="abc", source=root, protocol="https"
            )
            self.assertEqual(str(item.tls / "ca.crt"), environment["SSL_CERT_FILE"])
            self.assertEqual(
                "--cacert /etc/nginx/tls/ca.crt",
                environment["NVSOP_DEV_TLS_HEALTHCHECK_ARGS"],
            )

    def test_https_rejects_missing_configured_ca_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            item = self.make_item(directory)
            self.render_https(
                item, root, cert=self.tls.chain, key=self.tls.leaf_key, ca=self.tls.root_crt
            )
            (item.tls / "ca.crt").unlink()
            with self.assertRaises(self.module.DevError):
                self.module.configured_ca_certificate(item)

    def test_http_does_not_read_or_generate_certificates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            item = self.make_item(directory)
            with patch.dict(
                os.environ,
                {
                    "NVSOP_DEV_TLS_CERT_FILE": "",
                    "NVSOP_DEV_TLS_KEY_FILE": "",
                    "NVSOP_DEV_TLS_CA_FILE": "",
                },
                clear=False,
            ):
                ca = self.module.render_gateway_config(item, source=root, protocol="http")
            self.assertIsNone(ca)
            self.assertEqual([], sorted(path.name for path in item.tls.iterdir()))
            rendered = (item.state / "nginx.conf").read_text(encoding="utf-8")
            self.assertIn("listen 8443;", rendered)
            self.assertNotIn("ssl_certificate", rendered)

    def test_https_rejects_expired_certificate(self) -> None:
        self.assert_https_rejected(
            cert=self.tls.expired_chain,
            key=self.tls.leaf_key,
            ca=self.tls.root_crt,
            reason="certificate has expired",
        )

    def test_https_rejects_not_yet_valid_certificate(self) -> None:
        self.assert_https_rejected(
            cert=self.tls.future_chain,
            key=self.tls.leaf_key,
            ca=self.tls.root_crt,
            reason="certificate is not yet valid",
        )

    def test_https_rejects_hostname_mismatch(self) -> None:
        self.assert_https_rejected(
            cert=self.tls.wrong_host_chain,
            key=self.tls.wrong_host_key,
            ca=self.tls.root_crt,
            reason="hostname mismatch",
        )

    def test_https_rejects_mismatched_private_key(self) -> None:
        self.assert_https_rejected(
            cert=self.tls.chain,
            key=self.tls.other_ca_key,
            ca=self.tls.root_crt,
            reason="不匹配",
        )

    def test_https_rejects_encrypted_private_key(self) -> None:
        self.assert_https_rejected(
            cert=self.tls.chain,
            key=self.tls.encrypted_key,
            ca=self.tls.root_crt,
            reason="私钥无效",
        )

    def test_https_rejects_malformed_certificate(self) -> None:
        self.assert_https_rejected(
            cert=self.tls.malformed_crt,
            key=self.tls.leaf_key,
            ca=self.tls.root_crt,
            reason="不可解析",
        )

    def test_https_rejects_untrusted_chain_without_custom_ca(self) -> None:
        self.assert_https_rejected(
            cert=self.tls.chain,
            key=self.tls.leaf_key,
            reason="unable to get local issuer certificate",
        )

    def test_https_rejects_unknown_custom_ca(self) -> None:
        self.assert_https_rejected(
            cert=self.tls.chain,
            key=self.tls.leaf_key,
            ca=self.tls.other_ca_crt,
            reason="unable to get local issuer certificate",
        )

    def test_https_rejects_explicitly_wrong_ca_despite_trusted_default_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            item = self.make_item(directory)
            with self.assertRaises(self.module.DevError) as caught:
                self.render_https(
                    item,
                    root,
                    cert=self.tls.chain,
                    key=self.tls.leaf_key,
                    ca=self.tls.other_ca_crt,
                    ssl_cert_dir=self.tls.trust_dir,
                )
            self.assertIn("unable to get local issuer certificate", str(caught.exception))
            self.assertEqual([], sorted(path.name for path in item.tls.iterdir()))

    def test_https_staging_replaces_symlinked_targets_with_usable_copies(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            item = self.make_item(directory)
            external_cert = root / "external-cert.pem"
            external_key = root / "external-key.pem"
            external_cert.write_bytes(self.tls.chain.read_bytes())
            external_key.write_bytes(self.tls.leaf_key.read_bytes())
            (item.tls / "dev.crt").symlink_to(external_cert)
            (item.tls / "dev.key").symlink_to(external_key)

            self.render_https(
                item, root, cert=external_cert, key=external_key, ca=self.tls.root_crt
            )

            staged_cert = item.tls / "dev.crt"
            staged_key = item.tls / "dev.key"
            self.assertFalse(staged_cert.is_symlink())
            self.assertFalse(staged_key.is_symlink())
            self.assertEqual(external_cert.read_bytes(), staged_cert.read_bytes())
            self.assertEqual(external_key.read_bytes(), staged_key.read_bytes())
            self.assertEqual(0o600, staged_key.stat().st_mode & 0o777)
            usable = subprocess.run(
                ["openssl", "pkey", "-in", str(staged_key), "-noout", "-check", "-passin", "pass:"],
                check=False,
                capture_output=True,
            )
            self.assertEqual(0, usable.returncode, usable.stderr.decode())
            self.assertEqual(self.tls.chain.read_bytes(), external_cert.read_bytes())
            self.assertEqual(self.tls.leaf_key.read_bytes(), external_key.read_bytes())


CSP_HOST_ONE = "11111111-1111-4111-8111-111111111111"
CSP_HOST_TWO = "22222222-2222-4222-8222-222222222222"
# 合成 URL userinfo 片段：构造非法凭据样本，不固化为可用凭据。
SYNTHETIC_USERINFO = "user" + ":" + "pass" + "@"
CSP_GATEWAY_TEMPLATE = (
    "server {\n"
    "    listen 8443__NVSOP_LISTEN_SUFFIX__;\n"
    "    # __NVSOP_TLS_DIRECTIVES__\n"
    "    server_name localhost nginx;\n"
    "    add_header Content-Security-Policy \"default-src 'self'; "
    "script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; media-src 'self' blob: $scheme://localhost:8444; "
    "connect-src 'self' $scheme://localhost:8444; object-src 'none'; "
    "base-uri 'self'; frame-ancestors 'self'\" always;\n"
    "}\n"
)
CSP_RENDERED_BASELINE = CSP_GATEWAY_TEMPLATE.replace("__NVSOP_LISTEN_SUFFIX__", "").replace(
    "    # __NVSOP_TLS_DIRECTIVES__\n", ""
)


class DevEvidenceOriginCspTest(unittest.TestCase):
    """部署来源映射必须同源进入网关 CSP；无映射保持模板字节，非法输入 fail-fast。"""

    module = DEV

    def make_root(self, directory: str) -> tuple[DEV.DevPaths, Path]:
        root = Path(directory)
        item = self.module.DevPaths(root=root, state=root / ".state")
        self.module.ensure_directories(item)
        template = root / "deploy" / "dev" / "nginx.conf"
        template.parent.mkdir(parents=True, exist_ok=True)
        template.write_text(CSP_GATEWAY_TEMPLATE, encoding="utf-8")
        return item, root

    def render(
        self, item: DEV.DevPaths, root: Path, raw: str | None, *, protocol: str = "http"
    ) -> str:
        environment = {} if raw is None else {self.module.EVIDENCE_ORIGINS_ENVIRONMENT: raw}
        self.module.render_gateway_config(item, source=root, protocol=protocol, environ=environment)
        return (item.state / "nginx.conf").read_text(encoding="utf-8")

    def test_two_configured_origins_are_appended_only_to_connect_and_media(self) -> None:
        first = "http://127.0.0.1:9501"
        second = "https://evidence-secondary.internal:9443"
        with tempfile.TemporaryDirectory() as directory:
            item, root = self.make_root(directory)
            rendered = self.render(
                item, root, json.dumps({CSP_HOST_ONE: first, CSP_HOST_TWO: second})
            )
        self.assertIn(
            f"media-src 'self' blob: $scheme://localhost:8444 {first} {second};", rendered
        )
        self.assertIn(f"connect-src 'self' $scheme://localhost:8444 {first} {second};", rendered)
        self.assertEqual(2, rendered.count("$scheme://localhost:8444"))
        self.assertIn("default-src 'self'", rendered)
        self.assertIn("img-src 'self' data: blob:", rendered)
        self.assertIn("object-src 'none'", rendered)

    def test_empty_mapping_keeps_the_template_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            item, root = self.make_root(directory)
            self.assertEqual(CSP_RENDERED_BASELINE, self.render(item, root, "{}"))

    def test_absent_mapping_keeps_the_template_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            item, root = self.make_root(directory)
            self.assertEqual(CSP_RENDERED_BASELINE, self.render(item, root, None))

    def test_duplicate_origins_are_appended_once(self) -> None:
        origin = "https://evidence-shared.internal:9443"
        with tempfile.TemporaryDirectory() as directory:
            item, root = self.make_root(directory)
            rendered = self.render(
                item, root, json.dumps({CSP_HOST_ONE: origin, CSP_HOST_TWO: origin})
            )
        self.assertEqual(
            1, rendered.count(f"connect-src 'self' $scheme://localhost:8444 {origin};")
        )

    def test_invalid_environment_is_rejected(self) -> None:
        cases = {
            "malformed-json": "not-json",
            "not-an-object": json.dumps([CSP_HOST_ONE]),
            "non-canonical-uuid": json.dumps(
                {"11111111-1111-4111-8111-11111111111": "http://127.0.0.1:9501"}
            ),
            "origin-path": json.dumps({CSP_HOST_ONE: "https://evidence.internal:9443/path"}),
            "origin-query": json.dumps({CSP_HOST_ONE: "https://evidence.internal:9443?x=1"}),
            "origin-fragment": json.dumps({CSP_HOST_ONE: "https://evidence.internal:9443#x"}),
            "origin-credentials": json.dumps(
                {CSP_HOST_ONE: "https://" + SYNTHETIC_USERINFO + "evidence.internal:9443"}
            ),
            "origin-bad-port": json.dumps({CSP_HOST_ONE: "https://evidence.internal:99999"}),
            "origin-semicolon": json.dumps(
                {CSP_HOST_ONE: "https://evidence.internal:9443;connect-src *"}
            ),
            "origin-space": json.dumps({CSP_HOST_ONE: "https://evidence.internal:9443 *"}),
            "origin-quote": json.dumps({CSP_HOST_ONE: 'https://evidence.internal:9443"'}),
            "origin-dollar": json.dumps({CSP_HOST_ONE: "https://evidence.internal:9443$x"}),
            "origin-wildcard": json.dumps({CSP_HOST_ONE: "https://*.internal:9443"}),
            "non-string-value": json.dumps({CSP_HOST_ONE: 9443}),
        }
        for name, raw in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                item, root = self.make_root(directory)
                with self.assertRaises(self.module.DevError):
                    self.render(item, root, raw)

    def test_explicit_empty_or_whitespace_is_rejected(self) -> None:
        for name, raw in {"empty": "", "whitespace": "   "}.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                item, root = self.make_root(directory)
                with self.assertRaises(self.module.DevError):
                    self.render(item, root, raw)

    def test_error_message_does_not_echo_configured_origin(self) -> None:
        sensitive = "https://" + SYNTHETIC_USERINFO + "evidence.internal:9443"
        with tempfile.TemporaryDirectory() as directory:
            item, root = self.make_root(directory)
            with self.assertRaises(self.module.DevError) as caught:
                self.render(item, root, json.dumps({CSP_HOST_ONE: sensitive}))
        message = str(caught.exception)
        self.assertNotIn(SYNTHETIC_USERINFO, message)
        self.assertNotIn("evidence.internal", message)
        self.assertNotIn(sensitive, message)

    def test_error_message_does_not_echo_invalid_key(self) -> None:
        sensitive_key = "user" + ":" + "pass" + "@" + "evidence.internal"
        with tempfile.TemporaryDirectory() as directory:
            item, root = self.make_root(directory)
            with self.assertRaises(self.module.DevError) as caught:
                self.render(
                    item, root, json.dumps({sensitive_key: "https://evidence.internal:9443"})
                )
        message = str(caught.exception)
        self.assertNotIn("pass", message)
        self.assertNotIn("evidence.internal", message)

    def test_http_origin_is_rejected_for_https_entry(self) -> None:
        mapping = json.dumps({CSP_HOST_ONE: "http://127.0.0.1:9501"})
        with tempfile.TemporaryDirectory() as directory:
            item, root = self.make_root(directory)
            with self.assertRaises(self.module.DevError):
                self.render(item, root, mapping, protocol="https")

    def test_configured_origins_without_csp_header_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            item = self.module.DevPaths(root=root, state=root / ".state")
            self.module.ensure_directories(item)
            template = root / "deploy" / "dev" / "nginx.conf"
            template.parent.mkdir(parents=True, exist_ok=True)
            template.write_text(
                "server {\n"
                "    listen 8443__NVSOP_LISTEN_SUFFIX__;\n"
                "    # __NVSOP_TLS_DIRECTIVES__\n"
                "}\n",
                encoding="utf-8",
            )
            with self.assertRaises(self.module.DevError):
                self.render(item, root, json.dumps({CSP_HOST_ONE: "http://127.0.0.1:9501"}))


if __name__ == "__main__":
    unittest.main()
