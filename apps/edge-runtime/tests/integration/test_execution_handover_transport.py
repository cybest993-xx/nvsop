"""正常交权停机命令的主机签名传输与生产组合接线(#214).

真 loopback socket 验证: 领取只走专用 ``/device-commands/execution-handover/next``(默认
``/device-commands/next`` 不领取新类型), 回报覆盖冻结身份与 claim token, 签名经真实公钥
复核. 组合接线走生产 ``build_execution_handover_loop``.
"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from time import monotonic
from typing import ClassVar

from nvsop_contracts import (
    ExecutionHandoverClaim,
    ExecutionHandoverCommand,
    ExecutionHandoverOutcome,
    ExecutionHandoverResult,
    HostIdentityRequest,
    execution_handover_claim_to_wire,
    execution_handover_result_from_wire,
    verify_host_identity_request,
)
from test_delegated_command_transport import _fixture_host_identity

from edge_runtime.center_client import CenterClient
from edge_runtime.local_state.store import open_local_state
from edge_runtime.runtime import AutonomousRuntime, build_connection_test_loop
from edge_runtime.supervisor.execution_handover_transport import (
    EXECUTION_HANDOVER_CLAIM_PATH,
    ExecutionHandoverCommandLoop,
    ExecutionHandoverTransportError,
    HttpExecutionHandoverTransport,
)

HOST_IDENTITY = _fixture_host_identity(11)

CLAIM = ExecutionHandoverClaim(
    command=ExecutionHandoverCommand(
        command_id="command-1",
        handover_id="handover-1",
        station_id="line-3/station-7",
        from_host_id="host-a",
        to_host_id="host-b",
        grant_id="grant-1",
    ),
    claim_token="claim-1",
    lease_expires_at="2026-10-09T00:05:00Z",
)

RESULT = ExecutionHandoverResult(
    handover_id="handover-1",
    grant_id="grant-1",
    outcome=ExecutionHandoverOutcome.CONFIRMED,
)


class HandoverHandler(BaseHTTPRequestHandler):
    requests: ClassVar[list[tuple[str, str, dict[str, str], dict[str, object] | None]]] = []
    claim: ClassVar[ExecutionHandoverClaim] = CLAIM
    return_empty: ClassVar[bool] = False

    def do_GET(self) -> None:
        if self.path != EXECUTION_HANDOVER_CLAIM_PATH:
            self.send_error(404)
            return
        self._record(None)
        if HandoverHandler.return_empty:
            self.send_response(204)
            self.end_headers()
            return
        self._write_json(execution_handover_claim_to_wire(HandoverHandler.claim))

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length))
        self._record(payload)
        self._write_json({"accepted": True, "handover_id": "handover-1", "status": "confirmed"})

    def _record(self, body: dict[str, object] | None) -> None:
        headers = {key.lower(): value for key, value in self.headers.items()}
        HandoverHandler.requests.append((self.command, self.path, headers, body))

    def _write_json(self, payload: dict[str, object]) -> None:
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, *args: object, **kwargs: object) -> None:
        return None


class HttpExecutionHandoverTransportTest(unittest.TestCase):
    server: ThreadingHTTPServer
    thread: threading.Thread

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), HandoverHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.thread.join(timeout=5)
        cls.server.server_close()

    def setUp(self) -> None:
        HandoverHandler.requests = []
        HandoverHandler.return_empty = False
        HandoverHandler.claim = CLAIM

    def _transport(self) -> HttpExecutionHandoverTransport:
        return HttpExecutionHandoverTransport(
            client=CenterClient(
                center_url=f"http://127.0.0.1:{self.server.server_port}",
                host_id="host-a",
                host_private_key=HOST_IDENTITY.private_key,
                timeout=2.0,
            )
        )

    def test_claim_and_result_use_the_strict_wire_contract_and_host_identity(self) -> None:
        transport = self._transport()

        claim = transport.claim_next()
        self.assertEqual(CLAIM, claim)
        assert claim is not None
        transport.report(claim, RESULT)

        self.assertEqual(2, len(HandoverHandler.requests))
        get_method, get_path, get_headers, _ = HandoverHandler.requests[0]
        self.assertEqual("GET", get_method)
        self.assertEqual(EXECUTION_HANDOVER_CLAIM_PATH, get_path)
        self.assertEqual("host-a", get_headers["x-inference-host-id"])
        get_identity = HostIdentityRequest(
            method="GET",
            path=EXECUTION_HANDOVER_CLAIM_PATH,
            host_id="host-a",
            timestamp=int(get_headers["x-inference-host-timestamp"]),
            nonce=get_headers["x-inference-host-nonce"],
            body=None,
        )
        self.assertTrue(
            verify_host_identity_request(
                get_identity,
                public_key=HOST_IDENTITY.public_key,
                signature=get_headers["x-inference-host-signature"],
            )
        )

        post_method, post_path, post_headers, payload = HandoverHandler.requests[1]
        post_url = "/api/v1/device-commands/command-1/execution-handover-result"
        self.assertEqual("POST", post_method)
        self.assertEqual(post_url, post_path)
        self.assertEqual("claim-1", post_headers["x-command-claim-token"])
        post_identity = HostIdentityRequest(
            method="POST",
            path=post_url,
            host_id="host-a",
            timestamp=int(post_headers["x-inference-host-timestamp"]),
            nonce=post_headers["x-inference-host-nonce"],
            body=payload,
        )
        self.assertTrue(
            verify_host_identity_request(
                post_identity,
                public_key=HOST_IDENTITY.public_key,
                signature=post_headers["x-inference-host-signature"],
            )
        )
        # 回报覆盖冻结身份: outcome/detail/failure_code 全部落 wire.
        parsed = execution_handover_result_from_wire(payload or {})
        self.assertEqual(RESULT, parsed)

    def test_failure_result_carries_the_failure_code(self) -> None:
        transport = self._transport()

        claim = transport.claim_next()
        assert claim is not None
        transport.report(
            claim,
            ExecutionHandoverResult(
                handover_id="handover-1",
                grant_id="grant-1",
                outcome=ExecutionHandoverOutcome.FAILED,
                failure_code="COMMAND_STOP_FAILED",
                detail="station resources did not close safely",
            ),
        )

        _, _, _, payload = HandoverHandler.requests[1]
        parsed = execution_handover_result_from_wire(payload or {})
        self.assertEqual("COMMAND_STOP_FAILED", parsed.failure_code)

    def test_empty_queue_is_not_an_error(self) -> None:
        HandoverHandler.return_empty = True

        self.assertIsNone(self._transport().claim_next())

    def test_rejected_claim_raises_transport_error(self) -> None:
        class _RejectingHandler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                self.send_response(500)
                self.end_headers()

            def log_message(self, *args: object, **kwargs: object) -> None:
                return None

        with ThreadingHTTPServer(("127.0.0.1", 0), _RejectingHandler) as server:
            port = int(server.server_address[1])
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                transport = HttpExecutionHandoverTransport(
                    client=CenterClient(
                        center_url=f"http://127.0.0.1:{port}",
                        host_id="host-a",
                        host_private_key=HOST_IDENTITY.private_key,
                        timeout=2.0,
                    )
                )
                with self.assertRaises(ExecutionHandoverTransportError):
                    transport.claim_next()
            finally:
                server.shutdown()

    def test_runtime_starts_the_handover_worker(self) -> None:
        executed = threading.Event()

        def execute(command: ExecutionHandoverCommand) -> ExecutionHandoverResult:
            executed.set()
            return RESULT

        with tempfile.TemporaryDirectory() as directory:
            runtime = AutonomousRuntime(
                command_loop=build_connection_test_loop(
                    center_url=f"http://127.0.0.1:{self.server.server_port}",
                    host_id="host-a",
                    host_private_key=HOST_IDENTITY.private_key,
                    command_timeout=1.0,
                    command_poll_interval=0.01,
                    local_connectors=(),
                ),
                state=open_local_state(str(Path(directory) / "state.sqlite")),
                stations=(),
                execution_handover_loop=ExecutionHandoverCommandLoop(
                    transport=self._transport(), executor=execute, poll_interval=0.01
                ),
            )
            deadline = monotonic() + 1.0
            runtime.run_forever(should_stop=lambda: executed.is_set() or monotonic() >= deadline)
        self.assertTrue(executed.is_set(), "production runtime never polled the handover queue")
        self.assertTrue(any(method == "POST" for method, *_ in HandoverHandler.requests))


if __name__ == "__main__":
    unittest.main()
