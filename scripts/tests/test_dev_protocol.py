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
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("nvsop_dev_script", ROOT / "scripts" / "dev.py")
assert SPEC is not None and SPEC.loader is not None
DEV = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = DEV
SPEC.loader.exec_module(DEV)


class DevProtocolTest(unittest.TestCase):
    def test_https_is_the_default_and_http_is_explicit(self) -> None:
        self.assertEqual("https", DEV.configured_protocol({}))
        self.assertEqual("http", DEV.configured_protocol({"NVSOP_DEV_PROTOCOL": "http"}))
        self.assertEqual("http://localhost:8443", DEV.public_urls("http")["business"])
        self.assertEqual("https://localhost:8443", DEV.public_urls("https")["business"])

    def test_unknown_protocol_is_rejected(self) -> None:
        with self.assertRaises(DEV.DevError):
            DEV.configured_protocol({"NVSOP_DEV_PROTOCOL": "ftp"})

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

            self.assertTrue(DEV.usable_ca_certificate(item.tls / "ca.crt"))
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

    def test_tilt_commands_use_the_committed_snapshot_script(self) -> None:
        item = DEV.DevPaths(root=Path("/repo"), state=Path("/state"))
        environment = DEV.runtime_environment(item, sha="abc", source=Path("/snapshot"))

        self.assertEqual("/snapshot/scripts/dev.py", environment["NVSOP_LAUNCHER_SCRIPT"])
        self.assertEqual("/repo/scripts/dev.py", environment["NVSOP_HOST_LAUNCHER_SCRIPT"])

    def test_snapshot_archive_does_not_require_a_missing_lfs_object(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            environment = DEV.archive_environment()

        self.assertEqual("1", environment["GIT_LFS_SKIP_SMUDGE"])

    def test_tilt_manual_tests_use_host_launcher_and_do_not_run_on_start(self) -> None:
        tiltfile = (ROOT / "Tiltfile").read_text(encoding="utf-8")
        smoke = tiltfile.split('"functional-smoke"', 1)[1].split(")\n\nlocal_resource", 1)[0]
        ui = tiltfile.split('"visual-tests"', 1)[1].split(")\n", 1)[0]

        self.assertIn("cmd='python3 \"$NVSOP_HOST_LAUNCHER_SCRIPT\" smoke'", smoke)
        self.assertNotIn("cmd='python3 \"$NVSOP_LAUNCHER_SCRIPT\" smoke'", smoke)
        self.assertIn("auto_init=False", smoke)
        self.assertIn("auto_init=False", ui)

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
