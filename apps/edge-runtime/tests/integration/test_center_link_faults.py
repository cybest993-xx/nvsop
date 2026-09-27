"""中心链路故障注入: 真实本地 socket 上的断连与半截响应不得逃出传输边界。

推理机自治要求中心不可达时判定持续; 这些异常若逃出传输适配器, 会终止整个运行周期。
"""

from __future__ import annotations

import socketserver
import threading
import unittest
from typing import ClassVar
from unittest.mock import patch

from edge_runtime.configuration_sync import ConfigurationPullError, HttpConfigurationPuller
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
            "edge_runtime.supervisor.delegated_transport.sign_host_identity_request",
            return_value="signature",
        )
        signing.start()
        self.addCleanup(signing.stop)
        config_signing = patch(
            "edge_runtime.configuration_sync.sign_host_identity_request",
            return_value="signature",
        )
        config_signing.start()
        self.addCleanup(config_signing.stop)

    def _center_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def _command_transport(self) -> HttpCommandTransport:
        return HttpCommandTransport(
            center_url=self._center_url(),
            host_id="host-1",
            host_private_key="unused",  # pragma: allowlist secret
            timeout=2.0,
        )

    def test_command_claim_maps_disconnect_and_truncated_body_to_transport_error(self) -> None:
        for response in (_DISCONNECT, _TRUNCATED_BODY):
            with self.subTest(response=response[:12]):
                _FaultyCenterHandler.response = response
                with self.assertRaises(CommandTransportError):
                    self._command_transport().claim_next()

    def test_configuration_pull_maps_disconnect_and_truncated_body_to_unreachable(self) -> None:
        puller = HttpConfigurationPuller(
            center_url=self._center_url(),
            host_id="host-1",
            host_private_key="unused",  # pragma: allowlist secret
            timeout=2.0,
        )
        for response in (_DISCONNECT, _TRUNCATED_BODY):
            with self.subTest(response=response[:12]):
                _FaultyCenterHandler.response = response
                with self.assertRaises(ConfigurationPullError) as raised:
                    puller.pull()
                self.assertEqual(raised.exception.code, "center_unreachable")

    def test_command_loop_keeps_running_while_center_link_is_faulty(self) -> None:
        _FaultyCenterHandler.response = _TRUNCATED_BODY
        loop = ConnectionTestCommandLoop(
            runner=ConnectionTestCommandRunner(
                transport=self._command_transport(),
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
