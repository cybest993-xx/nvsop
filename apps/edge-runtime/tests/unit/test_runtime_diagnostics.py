"""干净进程经过正式 __main__ 初始化并验证真实点位诊断渲染。"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path


class RuntimeDiagnosticsTest(unittest.TestCase):
    def test_entrypoint_emits_write_replay_and_failure_without_secrets(self) -> None:
        # 只替换长驻运行循环和设备 transport。日志配置、dispatcher、sink 均为真实实现。
        program = """
import logging
import os
import runpy
from unittest.mock import patch
from nvsop_contracts import Unverified
from test_connector_writes import RaisingConnector, RecordingConnector, request
from edge_runtime.connectors.port import Failed, Refused, WriteRefusal
from edge_runtime.connectors.writes import InMemoryWriteLedger, OutputDispatcher
from edge_runtime.runtime import _log_write_attempt

assert not logging.getLogger().handlers
class Runtime:
    def run_forever(self, *, should_stop):
        connector = RecordingConnector()
        dispatcher = OutputDispatcher(
            connector=connector, ledger=InMemoryWriteLedger(), diagnostics=_log_write_attempt
        )
        dispatcher.write(request())
        dispatcher.write(request())
        assert len(connector.writes) == 1
        for key, connector in (
            ("failed", RecordingConnector(
                outcome=Failed(detail="synthetic-password-and-private-key")
            )),
            ("refused", RecordingConnector(capability=Unverified())),
            ("refused_secret", RecordingConnector(outcome=Refused(
                reason=WriteRefusal.POINT_UNREACHABLE,
                detail="synthetic-password-and-private-key",
            ))),
        ):
            OutputDispatcher(
                connector=connector, ledger=InMemoryWriteLedger(), diagnostics=_log_write_attempt
            ).write(request(key))
        broken = OutputDispatcher(
            connector=RaisingConnector(RuntimeError("synthetic-password-and-private-key")),
            ledger=InMemoryWriteLedger(), diagnostics=_log_write_attempt,
        )
        broken.write(request("raised"))
        broken.write(request("raised"))

os.environ["NVSOP_EDGE_COMMAND_CONFIG_FILE"] = "synthetic-config.json"
with patch("edge_runtime.runtime.build_autonomous_runtime_from_file", return_value=Runtime()):
    runpy.run_module("edge_runtime", run_name="__main__")
"""
        result = subprocess.run(
            [sys.executable, "-c", program],
            cwd=Path(__file__).parent,
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = [
            line for line in result.stderr.splitlines() if "edge.connector.write_attempt" in line
        ]
        self.assertEqual(len(rows), 7, result.stderr)
        for row in rows:
            self.assertIn("INFO edge_runtime edge.connector.write_attempt", row)
            self.assertIn("actor=supervisor:station-3", row)
            self.assertIn("point=停线联锁@1", row)
        self.assertIn("key=disposal-7", rows[0])
        self.assertIn("replayed=False outcome=Written", rows[0])
        self.assertIn("replayed=True outcome=Written", rows[1])
        self.assertIn("key=failed", rows[2])
        self.assertIn("outcome=Failed", rows[2])
        self.assertIn("key=refused", rows[3])
        self.assertIn("outcome=Refused", rows[3])
        self.assertIn("key=refused_secret", rows[4])
        self.assertIn("reason=point_unreachable", rows[4])
        self.assertIn("key=raised", rows[5])
        self.assertIn("replayed=False outcome=Failed", rows[5])
        self.assertIn("replayed=True outcome=Failed", rows[6])
        self.assertNotIn("synthetic-password-and-private-key", result.stdout + result.stderr)
        self.assertIn("error_type=RuntimeError", result.stderr)
