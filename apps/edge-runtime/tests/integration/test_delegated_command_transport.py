"""委托命令 HTTP 传输适配器的真实本地 socket 接缝。"""

from __future__ import annotations

import io
import json
import threading
import unittest
import urllib.error
from email.message import Message
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar
from unittest.mock import patch

from nvsop_contracts import (
    ConnectionTestClaim,
    ConnectionTestCommand,
    ConnectionTestOutcome,
    ConnectionTestResult,
    Unverified,
    connection_test_claim_to_wire,
)

from edge_runtime.connectors.hikvision import CANDIDATE_PROFILE
from edge_runtime.runtime import LocalIsapiConnectorConfiguration, build_connection_test_loop
from edge_runtime.supervisor.delegated_transport import CommandTransportError, HttpCommandTransport


class DeviceHandler(BaseHTTPRequestHandler):
    paths: ClassVar[list[str]] = []

    def do_GET(self) -> None:
        self.paths.append(self.path)
        if self.path != CANDIDATE_PROFILE.device_info_path:
            self.send_error(404)
            return
        self.send_response(200)
        self.end_headers()

    def log_message(self, format_string: str, *args: object) -> None:
        del format_string, args


class CommandHandler(BaseHTTPRequestHandler):
    claim: ClassVar[ConnectionTestClaim]
    requests: ClassVar[list[tuple[str, dict[str, str], dict[str, object] | None]]] = []
    return_empty: ClassVar[bool] = False

    def do_GET(self) -> None:
        if self.path != "/api/v1/device-commands/next":
            self.send_error(404)
            return
        headers = {key.lower(): value for key, value in self.headers.items()}
        self.requests.append((self.command, headers, None))
        if self.return_empty:
            self.send_response(204)
            self.end_headers()
            return
        self._write_json(connection_test_claim_to_wire(self.claim))

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length))
        headers = {key.lower(): value for key, value in self.headers.items()}
        self.requests.append((self.command, headers, payload))
        self._write_json({"status": "succeeded"})

    def log_message(self, format_string: str, *args: object) -> None:
        del format_string, args

    def _write_json(self, payload: dict[str, object]) -> None:
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


class HttpCommandTransportTest(unittest.TestCase):
    server: ThreadingHTTPServer
    thread: threading.Thread

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), CommandHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.thread.join(timeout=5)
        cls.server.server_close()

    def setUp(self) -> None:
        CommandHandler.requests = []
        CommandHandler.return_empty = False
        DeviceHandler.paths = []
        CommandHandler.claim = ConnectionTestClaim(
            command=ConnectionTestCommand(
                command_id="command-1",
                connector_id="connector-1",
                connector_revision=3,
                connector_type="hikvision_isapi",
                configuration={"address": "10.0.8.21", "port": 80},
            ),
            claim_token="claim-1",
            lease_expires_at="2026-09-08T08:01:00Z",
        )

    def _transport(self) -> HttpCommandTransport:
        return HttpCommandTransport(
            center_url=f"http://127.0.0.1:{self.server.server_port}",
            host_id="host-1",
            host_token="test-host-token",  # pragma: allowlist secret
            timeout=2.0,
        )

    def test_claim_and_report_use_the_shared_wire_contract_and_host_identity(self) -> None:
        transport = self._transport()

        claim = transport.claim_next()
        self.assertEqual(CommandHandler.claim, claim)
        self.assertIsNotNone(claim)
        assert claim is not None
        transport.report(
            claim,
            ConnectionTestResult(
                outcome=ConnectionTestOutcome.REACHABLE,
                credentials_configured=True,
            ),
        )

        self.assertEqual(2, len(CommandHandler.requests))
        get_method, get_headers, _ = CommandHandler.requests[0]
        self.assertEqual("GET", get_method)
        self.assertEqual("host-1", get_headers["x-inference-host-id"])
        self.assertEqual("test-host-token", get_headers["x-inference-host-token"])
        self.assertNotIn("password", get_headers)

        post_method, post_headers, payload = CommandHandler.requests[1]
        self.assertEqual("POST", post_method)
        self.assertEqual("host-1", post_headers["x-inference-host-id"])
        self.assertEqual("claim-1", post_headers["x-command-claim-token"])
        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertEqual("reachable", payload["outcome"])
        self.assertNotIn("password", json.dumps(payload))

    def test_empty_queue_is_not_an_error(self) -> None:
        CommandHandler.return_empty = True

        self.assertIsNone(self._transport().claim_next())

    def test_production_loop_composes_transport_runner_and_real_connector(self) -> None:
        device_server = ThreadingHTTPServer(("127.0.0.1", 0), DeviceHandler)
        device_thread = threading.Thread(target=device_server.serve_forever, daemon=True)
        device_thread.start()
        try:
            loop = build_connection_test_loop(
                center_url=f"http://127.0.0.1:{self.server.server_port}",
                host_id="host-1",
                host_token="test-host-token",  # pragma: allowlist secret
                command_timeout=2.0,
                command_poll_interval=0.1,
                local_connectors=(
                    LocalIsapiConnectorConfiguration(
                        connector_id="connector-1",
                        revision=3,
                        credentials_configured=True,
                        base_url=f"http://127.0.0.1:{device_server.server_port}",
                        username="edge-user",
                        password="edge-password",  # pragma: allowlist secret
                        profile=CANDIDATE_PROFILE,
                        capability=Unverified(),
                    ),
                ),
            )

            self.assertTrue(loop.run_once())
            self.assertEqual(DeviceHandler.paths, [CANDIDATE_PROFILE.device_info_path])
            self.assertEqual(
                CommandHandler.requests[1][2],
                {
                    "outcome": "reachable",
                    "detail": None,
                    "credentials_configured": True,
                    "failure_code": None,
                },
            )
        finally:
            device_server.shutdown()
            device_thread.join(timeout=5)
            device_server.server_close()

    def test_http_error_response_is_closed_before_transport_error_is_raised(self) -> None:
        body = io.BytesIO(b"rejected")
        error = urllib.error.HTTPError(
            url="http://center.invalid/api/v1/device-commands/next",
            code=401,
            msg="Unauthorized",
            hdrs=Message(),
            fp=body,
        )

        with (
            patch("urllib.request.urlopen", side_effect=error),
            self.assertRaises(CommandTransportError),
        ):
            self._transport().claim_next()

        self.assertTrue(body.closed)


if __name__ == "__main__":
    unittest.main()
