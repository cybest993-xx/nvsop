"""工位输入源的真实 HTTP SSE 接缝。"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from random import Random
from threading import Event
from typing import ClassVar
from unittest.mock import patch

from nvsop_contracts import HostIdentityKeyPair, generate_host_identity_key_pair

from edge_runtime.judgment.evidence import EvidenceMargins
from edge_runtime.judgment.model import HostInstant, Ordering, RuntimeParameters, Template
from edge_runtime.local_state.store import open_local_state
from edge_runtime.runtime import AutonomousStation, build_autonomous_runtime_from_file
from edge_runtime.station_runtime import SseStationInputSource
from edge_runtime.stream_health import StreamFact
from edge_runtime.supervisor.inputs import ActionRecognized, StreamHealthObserved, SupervisorInput
from edge_runtime.supervisor.startup import resume_station
from edge_runtime.supervisor.station import StationSupervisor


def _fixture_host_identity(seed: int) -> HostIdentityKeyPair:
    """用固定伪随机流生成合成测试密钥, 避免测试依赖系统熵。"""
    random = Random(seed)
    with (
        patch("nvsop_contracts.host_identity.secrets.randbits", side_effect=random.getrandbits),
        patch("nvsop_contracts.host_identity.secrets.randbelow", side_effect=random.randrange),
    ):
        return generate_host_identity_key_pair()


class _SseHandler(BaseHTTPRequestHandler):
    payloads: ClassVar[list[bytes]] = []
    request_bodies: ClassVar[list[bytes]] = []
    lock: ClassVar[threading.Lock] = threading.Lock()

    def do_POST(self) -> None:
        size = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(size)
        with self.lock:
            self.request_bodies.append(body)
            payload = self.payloads.pop(0)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(payload)
        self.wfile.flush()

    def log_message(self, format_string: str, *args: object) -> None:
        del format_string, args


class _ProcessInferenceHandler(BaseHTTPRequestHandler):
    payload: ClassVar[bytes]
    request_received: ClassVar[Event]

    def do_POST(self) -> None:
        type(self).request_received.set()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(self.payload)
        self.wfile.flush()

    def log_message(self, format_string: str, *args: object) -> None:
        del format_string, args


class _BlockingSseHandler(BaseHTTPRequestHandler):
    ready: ClassVar[Event]
    release: ClassVar[Event]

    def do_POST(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        self.wfile.flush()
        self.ready.set()
        self.release.wait()

    def log_message(self, format_string: str, *args: object) -> None:
        del format_string, args


@contextmanager
def _sse_server(payloads: list[bytes]) -> Iterator[str]:
    _SseHandler.payloads = list(payloads)
    _SseHandler.request_bodies = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _SseHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1/chat/completions"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


@contextmanager
def _process_inference_server(payload: bytes) -> Iterator[tuple[str, Event]]:
    _ProcessInferenceHandler.payload = payload
    _ProcessInferenceHandler.request_received = Event()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ProcessInferenceHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield (
            f"http://127.0.0.1:{server.server_port}/v1/chat/completions",
            _ProcessInferenceHandler.request_received,
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


@contextmanager
def _blocking_sse_server() -> Iterator[str]:
    _BlockingSseHandler.ready = Event()
    _BlockingSseHandler.release = Event()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _BlockingSseHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1/chat/completions"
    finally:
        _BlockingSseHandler.release.set()
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


class _OneInputSource:
    def __init__(self) -> None:
        self._input: SupervisorInput | None = ActionRecognized(
            signal="(1) start",
            at=HostInstant(1.0),
            source_time=1.0,
            source_anchor=1.0,
        )
        self.closed = False

    @property
    def ended(self) -> bool:
        return self._input is None

    def next_input(self, *, timeout: float | None) -> SupervisorInput | None:
        del timeout
        arriving = self._input
        self._input = None
        return arriving

    def close(self) -> None:
        self.closed = True


def _action(signal: str, *, source_time: float, source_anchor: float) -> bytes:
    return (
        b"data: "
        + json.dumps(
            {
                "choices": [
                    {
                        "delta": {"content": signal},
                        "chunk_metadata": {
                            "response": signal,
                            "start_time": source_time,
                            "first_timestamp": source_anchor,
                        },
                    }
                ]
            }
        ).encode("utf-8")
        + b"\n\n"
    )


class RealSseStationInputSourceTest(unittest.TestCase):
    def test_close_releases_a_real_http_sse_reader(self) -> None:
        with _blocking_sse_server() as url:
            source = SseStationInputSource(
                inference_url=url,
                request_body={"stream": True},
                timeout=1.0,
            )
            result: list[object] = []
            reader = threading.Thread(
                target=lambda: result.append(source.next_input(timeout=None)),
                daemon=True,
            )
            reader.start()
            self.assertTrue(_BlockingSseHandler.ready.wait(1.0))
            source.close()
            reader.join(timeout=1.0)
            self.assertFalse(reader.is_alive())
            self.assertEqual([None], result)

    def test_real_http_sse_preserves_health_and_action_then_reconnects(self) -> None:
        first_payload = b"".join(
            (
                b": keep-alive\n\n",
                b"data: "
                + json.dumps(
                    {
                        "choices": [
                            {
                                "chunk_metadata": {
                                    "stream_health": {
                                        "fact": "delivering",
                                        "at_monotonic": 1.0,
                                        "source_anchor": 0.0,
                                    }
                                }
                            }
                        ]
                    }
                ).encode("utf-8")
                + b"\n\n",
                _action("(1) start", source_time=1.5, source_anchor=8.0),
                b"data: [DONE]\n\n",
            )
        )
        second_payload = b"".join(
            (
                b"data: "
                + json.dumps(
                    {
                        "choices": [
                            {
                                "chunk_metadata": {
                                    "stream_health": {
                                        "fact": "delivering",
                                        "at_monotonic": 2.0,
                                        "source_anchor": 8.0,
                                    }
                                }
                            }
                        ]
                    }
                ).encode("utf-8")
                + b"\n\n",
            )
        )
        with _sse_server([first_payload, second_payload]) as url:
            source = SseStationInputSource(
                inference_url=url,
                request_body={"stream": True},
                timeout=0.01,
            )
            health = source.next_input(timeout=None)
            action = source.next_input(timeout=None)
            disconnected = source.next_input(timeout=None)
            recovered = source.next_input(timeout=1.0)
            not_ended_before_close = not source.ended
            source.close()

        self.assertIsInstance(health, StreamHealthObserved)
        assert isinstance(health, StreamHealthObserved)
        self.assertEqual(StreamFact.DELIVERING, health.event.fact)
        self.assertIsInstance(action, ActionRecognized)
        assert isinstance(action, ActionRecognized)
        self.assertEqual("(1) start", action.signal)
        self.assertIsInstance(disconnected, StreamHealthObserved)
        assert isinstance(disconnected, StreamHealthObserved)
        self.assertEqual(StreamFact.STREAM_ENDED, disconnected.event.fact)
        self.assertIsInstance(recovered, StreamHealthObserved)
        assert isinstance(recovered, StreamHealthObserved)
        self.assertEqual(StreamFact.DELIVERING, recovered.event.fact)
        self.assertTrue(not_ended_before_close)
        self.assertEqual(
            [{"stream": True}, {"stream": True}],
            [json.loads(body) for body in _SseHandler.request_bodies],
        )


class RuntimeCompositionIntegrationTest(unittest.TestCase):
    def test_builder_composes_local_state_resume_and_real_station_stream(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            host_private_key_file = directory / "host-private-key"
            host_private_key_file.write_text(
                _fixture_host_identity(6).private_key + "\n", encoding="utf-8"
            )
            config_file = directory / "config.json"
            config_file.write_text(
                json.dumps(
                    {
                        "center_url": "https://center.example",
                        "host_id": "host-for-test",
                        "host_private_key_file": str(host_private_key_file),
                        "command_timeout_seconds": 2.0,
                        "command_poll_interval_seconds": 1.0,
                        "connectors": [],
                        "local_state_path": str(directory / "state.sqlite"),
                        "stations": [
                            {
                                "station_id": "station-1",
                                "inference_url": "http://inference.example/v1/chat/completions",
                                "request": {"messages": [], "stream": True},
                                "template": {
                                    "steps": ["(1) start"],
                                    "ordering": "ordered",
                                    "start_signal": "(1) start",
                                    "end_signals": [],
                                },
                                "parameters": {"idle_timeout": 30, "step_deadline": 10},
                                "margins": {"leading": 1, "trailing": 1},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            runtime = build_autonomous_runtime_from_file(config_file)
            try:
                self.assertEqual(1, len(runtime.stations))
                self.assertIsInstance(runtime.stations[0].supervisor, StationSupervisor)
                self.assertEqual(
                    "(1) start", runtime.stations[0].supervisor.state.template.steps[0]
                )
            finally:
                runtime.close()


class ProductionEntrypointIntegrationTest(unittest.TestCase):
    def test_python_module_entrypoint_persists_a_real_station_decision(self) -> None:
        payload = (
            b"data: "
            + json.dumps(
                {
                    "choices": [
                        {
                            "delta": {"content": "(1) start"},
                            "chunk_metadata": {
                                "response": "(1) start",
                                "start_time": 1.0,
                                "first_timestamp": 1.0,
                            },
                        }
                    ]
                }
            ).encode("utf-8")
            + b"\n\n"
            + b"data: [DONE]\n\n"
        )
        with (
            tempfile.TemporaryDirectory() as temporary,
            _process_inference_server(payload) as (inference_url, request_received),
        ):
            directory = Path(temporary)
            private_key_file = directory / "host-private-key"
            private_key_file.write_text(
                _fixture_host_identity(7).private_key + "\n", encoding="utf-8"
            )
            state_path = directory / "state.sqlite"
            config_path = directory / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "center_url": "https://127.0.0.1:9",
                        "host_id": "host-for-test",
                        "host_private_key_file": str(private_key_file),
                        "local_state_path": str(state_path),
                        "stations": [
                            {
                                "station_id": "station-process",
                                "inference_url": inference_url,
                                "request": {"stream": True, "messages": []},
                                "template": {
                                    "steps": ["(1) start"],
                                    "ordering": "ordered",
                                    "start_signal": "(1) start",
                                    "end_signals": [],
                                },
                                "parameters": {"idle_timeout": 10, "step_deadline": 10},
                                "margins": {"leading": 0, "trailing": 0},
                            }
                        ],
                        "command_timeout_seconds": 0.1,
                        "command_poll_interval_seconds": 0.1,
                        "connectors": [],
                    }
                ),
                encoding="utf-8",
            )
            repo_root = Path(__file__).resolve().parents[4]
            process = subprocess.Popen(
                [sys.executable, "-m", "edge_runtime"],
                cwd=repo_root,
                env={
                    **os.environ,
                    "PYTHONPATH": os.pathsep.join(
                        (
                            str(repo_root / "apps" / "edge-runtime" / "src"),
                            str(repo_root / "packages" / "contracts" / "src"),
                        )
                    ),
                    "NVSOP_EDGE_COMMAND_CONFIG_FILE": str(config_path),
                    "NO_PROXY": "127.0.0.1,localhost",
                    "no_proxy": "127.0.0.1,localhost",
                    "PYTHONUNBUFFERED": "1",
                },
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                self.assertTrue(request_received.wait(5.0))
                deadline = time.monotonic() + 5.0
                decision_count = 0
                while time.monotonic() < deadline:
                    with sqlite3.connect(state_path) as state:
                        decision_count = state.execute(
                            "SELECT count(*) FROM local_decision WHERE station_id = ?",
                            ("station-process",),
                        ).fetchone()[0]
                    if decision_count >= 1:
                        break
                    time.sleep(0.05)
                self.assertGreaterEqual(decision_count, 1)
                with sqlite3.connect(state_path) as state:
                    instance = state.execute(
                        """
                        SELECT instance_id, seen, expected_index, impairments,
                               settled, closed_at, lifecycle
                          FROM local_sop_instance
                         WHERE station_id = ?
                        """,
                        ("station-process",),
                    ).fetchone()
                self.assertIsNotNone(instance)
                assert instance is not None
                self.assertEqual(
                    (1, '["(1) start"]', 1, "[]", "[]", instance[5], "closed_by_complete_set"),
                    instance,
                )
                self.assertIsNotNone(instance[5])
            finally:
                if process.poll() is None:
                    process.terminate()
                stdout, stderr = process.communicate(timeout=10)
                self.assertEqual(0, process.returncode, f"stdout={stdout} stderr={stderr}")


class AutonomousStationIntegrationTest(unittest.TestCase):
    def test_station_loop_resumes_sqlite_state_and_commits_a_real_input_reaction(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = open_local_state(str(Path(temporary) / "state.sqlite"))
            store = state.station("station-1")
            supervisor = resume_station(
                store,
                template=Template(
                    steps=("(1) start", "(2) finish"),
                    ordering=Ordering.ORDERED,
                    start_signal="(1) start",
                ),
                parameters=RuntimeParameters(idle_timeout=10.0, step_deadline=10.0),
                margins=EvidenceMargins(leading=0.0, trailing=0.0),
            )
            source = _OneInputSource()
            station = AutonomousStation(supervisor=supervisor, source=source)

            station.run_forever(should_stop=lambda: False)

            self.assertTrue(source.closed)
            self.assertEqual(1, len(store.pending_reports()))
            state.close()


if __name__ == "__main__":
    unittest.main()
