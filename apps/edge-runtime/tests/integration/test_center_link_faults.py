"""中心链路故障注入: 真实本地 socket 上的断连与半截响应不得逃出传输边界。

推理机自治要求中心不可达时判定持续; 这些异常若逃出传输适配器, 会终止整个运行周期。
"""

from __future__ import annotations

import socketserver
import threading
import unittest
from typing import ClassVar
from unittest.mock import patch

from nvsop_contracts import ReportedHealth

from edge_runtime.center_client import CenterClient, CenterUnreachableError
from edge_runtime.configuration_sync import ConfigurationPullError, HttpConfigurationPuller
from edge_runtime.reporting_transport import HttpDecisionReportTransport, ReportTransportError
from edge_runtime.runtime import ConfiguredLocalConnectorRegistry, ConnectionTestCommandLoop
from edge_runtime.supervisor.delegated_commands import (
    ConnectionTestCommandRunner,
    ConnectionTestExecutor,
)
from edge_runtime.supervisor.delegated_transport import CommandTransportError, HttpCommandTransport

# 读完请求后立即关闭连接: 客户端在读取状态行时遇到断连。
_DISCONNECT = b""
# 声明 100 字节正文却只发送几个字节后关闭: 客户端在读取正文时遇到半截响应。
_TRUNCATED_BODY = (
    b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 100\r\n\r\n{"a"'
)


class _FaultyCenterHandler(socketserver.BaseRequestHandler):
    response: ClassVar[bytes] = _DISCONNECT

    def handle(self) -> None:
        self.request.recv(65536)
        if self.response:
            self.request.sendall(self.response)


class _FaultyCenter(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


class CenterLinkFaultTest(unittest.TestCase):
    server: _FaultyCenter
    thread: threading.Thread

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = _FaultyCenter(("127.0.0.1", 0), _FaultyCenterHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.thread.join(timeout=5)
        cls.server.server_close()

    def setUp(self) -> None:
        signing = patch(
            "edge_runtime.center_client.sign_host_identity_request", return_value="signature"
        )
        signing.start()
        self.addCleanup(signing.stop)

    def _client(self) -> CenterClient:
        return CenterClient(
            center_url=f"http://127.0.0.1:{self.server.server_address[1]}",
            host_id="host-1",
            host_private_key="unused",  # pragma: allowlist secret
            timeout=2.0,
        )

    def test_center_client_maps_disconnect_and_truncated_body_to_unreachable(self) -> None:
        for response in (_DISCONNECT, _TRUNCATED_BODY):
            with self.subTest(response=response[:12]):
                _FaultyCenterHandler.response = response
                with self.assertRaises(CenterUnreachableError):
                    self._client().get("/api/v1/device-commands/next")

    def test_every_center_adapter_turns_link_faults_into_its_retryable_error(self) -> None:
        _FaultyCenterHandler.response = _TRUNCATED_BODY
        with self.assertRaises(CommandTransportError):
            HttpCommandTransport(client=self._client()).claim_next()
        with self.assertRaises(ConfigurationPullError) as raised:
            HttpConfigurationPuller(client=self._client(), host_id="host-1").pull()
        self.assertEqual(raised.exception.code, "center_unreachable")
        with self.assertRaises(ReportTransportError):
            HttpDecisionReportTransport(client=self._client(), host_id="host-1").send_health(
                ReportedHealth(
                    event_id="host-1:health",
                    trace_id="host-1:health",
                    host_id="host-1",
                    station_id="station-1",
                    status="healthy",
                    reason_code=None,
                    detail=None,
                    reported_at="2026-09-28T00:00:00Z",
                )
            )

    def test_command_loop_keeps_running_while_center_link_is_faulty(self) -> None:
        _FaultyCenterHandler.response = _TRUNCATED_BODY
        loop = ConnectionTestCommandLoop(
            runner=ConnectionTestCommandRunner(
                transport=HttpCommandTransport(client=self._client()),
                executor=ConnectionTestExecutor(
                    registry=ConfiguredLocalConnectorRegistry(()), timeout=1.0
                ),
            ),
            poll_interval=0.01,
        )
        rounds = iter(range(3))

        loop.run_forever(should_stop=lambda: next(rounds, None) is None)


if __name__ == "__main__":
    unittest.main()
