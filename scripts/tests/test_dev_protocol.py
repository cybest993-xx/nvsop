from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest
from pathlib import Path
from threading import Event
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

    def test_state_without_protocol_keeps_legacy_https_meaning(self) -> None:
        item = DEV.DevPaths(root=Path("/repo"), state=Path("/state"))
        with patch.object(DEV, "read_state", return_value={}):
            self.assertEqual("https", DEV.state_protocol(item))

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

    def test_tls_repairs_a_legacy_ca_without_ca_usage_extensions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            item = DEV.DevPaths(root=Path(directory), state=Path(directory) / ".tmp")
            DEV.ensure_directories(item)
            legacy_ca = subprocess.run(
                [
                    "openssl",
                    "req",
                    "-x509",
                    "-new",
                    "-nodes",
                    "-newkey",
                    "rsa:2048",
                    "-keyout",
                    str(item.tls / "ca.key"),
                    "-out",
                    str(item.tls / "ca.crt"),
                    "-days",
                    "1",
                    "-subj",
                    "/CN=Legacy NVSOP Development CA",
                ],
                check=False,
                capture_output=True,
            )
            self.assertEqual(0, legacy_ca.returncode, legacy_ca.stderr.decode())

            DEV.ensure_tls(item)

            self.assertTrue(DEV.usable_ca_certificate(item.tls / "ca.crt", item.tls / "ca.key"))
            self.assertTrue(
                DEV.usable_server_certificate(
                    item.tls / "ca.crt", item.tls / "dev.crt", item.tls / "dev.key"
                )
            )
            verified = subprocess.run(
                [
                    "openssl",
                    "verify",
                    "-CAfile",
                    str(item.tls / "ca.crt"),
                    str(item.tls / "dev.crt"),
                ],
                check=False,
                capture_output=True,
            )
            self.assertEqual(0, verified.returncode, verified.stderr.decode())

    def test_tls_replaces_a_mismatched_ca_key_before_signing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            item = DEV.DevPaths(root=Path(directory), state=Path(directory) / ".tmp")
            DEV.ensure_directories(item)
            DEV.ensure_tls(item)
            replacement = subprocess.run(
                ["openssl", "genrsa", "-out", str(item.tls / "ca.key"), "2048"],
                check=False,
                capture_output=True,
            )
            self.assertEqual(0, replacement.returncode, replacement.stderr.decode())
            self.assertFalse(DEV.usable_ca_certificate(item.tls / "ca.crt", item.tls / "ca.key"))

            DEV.ensure_tls(item)

            self.assertTrue(DEV.usable_ca_certificate(item.tls / "ca.crt", item.tls / "ca.key"))
            self.assertTrue(
                DEV.usable_server_certificate(
                    item.tls / "ca.crt", item.tls / "dev.crt", item.tls / "dev.key"
                )
            )

    def test_tls_replaces_a_non_self_signed_ca_before_signing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            item = DEV.DevPaths(root=Path(directory), state=Path(directory) / ".tmp")
            DEV.ensure_directories(item)
            DEV.ensure_tls(item)
            external_key = item.tls / "external.key"
            external_crt = item.tls / "external.crt"
            root = subprocess.run(
                [
                    "openssl",
                    "req",
                    "-x509",
                    "-new",
                    "-nodes",
                    "-newkey",
                    "rsa:2048",
                    "-keyout",
                    str(external_key),
                    "-out",
                    str(external_crt),
                    "-days",
                    "825",
                    "-subj",
                    "/CN=External Development Root",
                ],
                check=False,
                capture_output=True,
            )
            self.assertEqual(0, root.returncode, root.stderr.decode())
            csr = item.tls / "ca.csr"
            request = subprocess.run(
                [
                    "openssl",
                    "req",
                    "-new",
                    "-key",
                    str(item.tls / "ca.key"),
                    "-out",
                    str(csr),
                    "-subj",
                    "/CN=Non-self-signed Development CA",
                ],
                check=False,
                capture_output=True,
            )
            self.assertEqual(0, request.returncode, request.stderr.decode())
            signed = subprocess.run(
                [
                    "openssl",
                    "x509",
                    "-req",
                    "-in",
                    str(csr),
                    "-CA",
                    str(external_crt),
                    "-CAkey",
                    str(external_key),
                    "-CAcreateserial",
                    "-CAserial",
                    str(item.tls / "external.srl"),
                    "-out",
                    str(item.tls / "ca.crt"),
                    "-days",
                    "825",
                    "-sha256",
                    "-extfile",
                    str(item.tls / "openssl.cnf"),
                    "-extensions",
                    "v3_ca",
                ],
                check=False,
                capture_output=True,
            )
            self.assertEqual(0, signed.returncode, signed.stderr.decode())

            self.assertFalse(DEV.usable_ca_certificate(item.tls / "ca.crt", item.tls / "ca.key"))
            DEV.ensure_tls(item)

            self.assertTrue(DEV.usable_ca_certificate(item.tls / "ca.crt", item.tls / "ca.key"))
            self.assertTrue(
                DEV.usable_server_certificate(
                    item.tls / "ca.crt", item.tls / "dev.crt", item.tls / "dev.key"
                )
            )

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
                patch.object(DEV, "run_checked", side_effect=DEV.DevError("launcher unavailable")),
                self.assertRaises(DEV.DevError),
            ):
                DEV.run_ui(item)

            state = DEV.read_state(item)
            self.assertIsNone(state["test"])
            self.assertEqual("failed", state["last_ui"]["status"])
            self.assertIsNone(state["last_ui"]["exit_code"])
            self.assertIn("launcher unavailable", state["last_ui"]["error"])

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


if __name__ == "__main__":
    unittest.main()
