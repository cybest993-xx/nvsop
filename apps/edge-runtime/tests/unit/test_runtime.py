"""推理机委托命令生产循环的行为。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import cast

from nvsop_contracts import Unverified, capability_to_wire

from edge_runtime.connectors.hikvision import CANDIDATE_PROFILE
from edge_runtime.runtime import ConnectionTestCommandLoop, build_connection_test_loop_from_file
from edge_runtime.supervisor.delegated_commands import ConnectionTestCommandRunner
from edge_runtime.supervisor.delegated_transport import CommandTransportError


def _valid_config(directory: Path, *, center_url: str, connector_url: str) -> Path:
    host_token_file = directory / "host-token"
    host_token_file.write_text("host-token-for-test\n", encoding="utf-8")
    username_file = directory / "username"
    username_file.write_text("edge-user-for-test\n", encoding="utf-8")
    password_file = directory / "password"
    password_file.write_text("edge-password-for-test\n", encoding="utf-8")
    profile = {
        "input_status_path": CANDIDATE_PROFILE.input_status_path,
        "output_trigger_path": CANDIDATE_PROFILE.output_trigger_path,
        "output_body": CANDIDATE_PROFILE.output_body,
        "device_info_path": CANDIDATE_PROFILE.device_info_path,
        "input_state_element": CANDIDATE_PROFILE.input_state_element,
        "input_tokens": [
            {"token": token, "state": state.value}
            for token, state in CANDIDATE_PROFILE.input_tokens
        ],
        "output_tokens": [
            {"state": state.value, "token": token}
            for state, token in CANDIDATE_PROFILE.output_tokens
        ],
    }
    config = {
        "center_url": center_url,
        "host_id": "host-for-test",
        "host_token_file": str(host_token_file),
        "command_timeout_seconds": 2.0,
        "command_poll_interval_seconds": 1.0,
        "connectors": [
            {
                "connector_id": "connector-for-test",
                "revision": 1,
                "credentials_configured": True,
                "base_url": connector_url,
                "username_file": str(username_file),
                "password_file": str(password_file),
                "profile": profile,
                "capability": capability_to_wire(Unverified()),
            }
        ],
    }
    config_file = directory / "config.json"
    config_file.write_text(json.dumps(config), encoding="utf-8")
    return config_file


class FailingThenIdleRunner:
    """只替换命令执行器 seam, 验证循环把传输失败交给下一轮。"""

    def __init__(self) -> None:
        self.calls = 0

    def run_once(self) -> bool:
        self.calls += 1
        if self.calls == 1:
            raise CommandTransportError("center unavailable")
        return False


class ConnectionTestCommandLoopTest(unittest.TestCase):
    def test_transport_failure_does_not_stop_the_autonomous_poll_loop(self) -> None:
        runner = FailingThenIdleRunner()
        slept: list[float] = []
        stop_states = iter((False, False, True))
        loop = ConnectionTestCommandLoop(
            runner=cast(ConnectionTestCommandRunner, runner),
            poll_interval=0.25,
            sleep_fn=slept.append,
        )

        loop.run_forever(should_stop=lambda: next(stop_states))

        self.assertEqual(2, runner.calls)
        self.assertEqual([0.25, 0.25], slept)


class RuntimeConfigurationTest(unittest.TestCase):
    def test_loader_rejects_url_userinfo_query_fragment_and_wrong_scheme(self) -> None:
        cases = (
            ("center_url", "https://edge-password@center.example", "center_url"),
            ("center_url", "https://center.example/?token=edge-password", "center_url"),
            ("center_url", "https://center.example/#edge-password", "center_url"),
            ("center_url", "https://center.example?", "center_url"),
            ("center_url", "https://center.example#", "center_url"),
            ("center_url", "https://center.example:edge-password", "center_url"),
            ("center_url", "http://center.example", "center_url"),
            ("connector_url", "ftp://camera.example", "connector base_url"),
            ("connector_url", "http://edge-password@camera.example", "connector base_url"),
            (
                "connector_url",
                "http://camera.example/?password=edge-password",
                "connector base_url",
            ),
            ("connector_url", "http://camera.example/#edge-password", "connector base_url"),
            ("connector_url", "http://camera.example?", "connector base_url"),
            ("connector_url", "http://camera.example#", "connector base_url"),
            ("connector_url", "http://camera.example:edge-password", "connector base_url"),
        )
        for field, value, label in cases:
            with self.subTest(field=field, value=value), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                config_file = _valid_config(
                    directory,
                    center_url=(value if field == "center_url" else "https://center.example"),
                    connector_url=(value if field == "connector_url" else "http://camera.example"),
                )
                with self.assertRaises(ValueError) as raised:
                    build_connection_test_loop_from_file(config_file)
                self.assertIn(label, str(raised.exception))
                self.assertNotIn("edge-password", str(raised.exception))
                self.assertIsNone(raised.exception.__cause__)


if __name__ == "__main__":
    unittest.main()
