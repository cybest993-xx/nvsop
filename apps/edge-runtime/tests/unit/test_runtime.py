"""推理机委托命令生产循环的行为。"""

from __future__ import annotations

import json
import tempfile
import unittest
import urllib.error
from dataclasses import dataclass
from pathlib import Path
from random import Random
from threading import Event, Thread
from time import monotonic
from typing import cast
from unittest.mock import patch

from nvsop_contracts import (
    HostIdentityKeyPair,
    Unverified,
    capability_to_wire,
    generate_host_identity_key_pair,
)

from edge_runtime.connectors.hikvision import CANDIDATE_PROFILE
from edge_runtime.judgment.model import HostInstant
from edge_runtime.runtime import (
    ConnectionTestCommandLoop,
    InputWaitExpired,
    SseStationInputSource,
    build_connection_test_loop_from_file,
)
from edge_runtime.stream_health import StreamFact, StreamHealthEvent
from edge_runtime.supervisor.delegated_commands import ConnectionTestCommandRunner
from edge_runtime.supervisor.delegated_transport import CommandTransportError
from edge_runtime.supervisor.inputs import ActionRecognized, StreamHealthObserved, SupervisorInput


def _fixture_host_identity(seed: int) -> HostIdentityKeyPair:
    """用固定伪随机流生成合成测试密钥, 避免测试依赖系统熵。"""
    random = Random(seed)
    with (
        patch("nvsop_contracts.host_identity.secrets.randbits", side_effect=random.getrandbits),
        patch("nvsop_contracts.host_identity.secrets.randbelow", side_effect=random.randrange),
    ):
        return generate_host_identity_key_pair()


HOST_IDENTITY = _fixture_host_identity(4)


def _valid_config(directory: Path, *, center_url: str, connector_url: str) -> Path:
    host_private_key_file = directory / "host-private-key"
    host_private_key_file.write_text(HOST_IDENTITY.private_key + "\n", encoding="utf-8")
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
        "host_private_key_file": str(host_private_key_file),
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


class FakeSseResponse:
    def __init__(self, *lines: bytes) -> None:
        self.lines = iter(lines)
        self.closed = False

    def readline(self) -> bytes:
        return next(self.lines, b"")

    def close(self) -> None:
        self.closed = True


class BlockingSseResponse:
    def __init__(self) -> None:
        self.started = Event()
        self.release = Event()
        self.closed = False

    def readline(self) -> bytes:
        self.started.set()
        self.release.wait()
        return b""

    def close(self) -> None:
        self.closed = True
        self.release.set()


class TimeoutSseResponse:
    def __init__(self) -> None:
        self.closed = False

    def readline(self) -> bytes:
        raise TimeoutError

    def close(self) -> None:
        self.closed = True


class BackpressureSseResponse:
    def __init__(self, *lines: bytes) -> None:
        self._lines = iter(lines)
        self._read_count = 0
        self.started = Event()
        self.allow_first_event = Event()
        self.first_event_queued = Event()
        self.release_second_event = Event()
        self.finished = Event()
        self.closed = False

    def readline(self) -> bytes:
        self._read_count += 1
        if self._read_count == 1:
            self.started.set()
            self.allow_first_event.wait()
        if self._read_count == 3:
            self.first_event_queued.set()
            self.release_second_event.wait()
        return next(self._lines, b"")

    def close(self) -> None:
        self.closed = True
        self.release_second_event.set()
        self.finished.set()


@dataclass
class FakeStationInputSource:
    inputs: list[SupervisorInput]
    closed: bool = False

    @property
    def ended(self) -> bool:
        return not self.inputs

    def next_input(self, *, timeout: float | None) -> SupervisorInput | None:
        del timeout
        return self.inputs.pop(0) if self.inputs else None

    def close(self) -> None:
        self.closed = True


class FailingThenIdleRunner:
    """只替换命令执行器 seam, 验证循环把传输失败交给下一轮。"""

    def __init__(self) -> None:
        self.calls = 0

    def run_once(self) -> bool:
        self.calls += 1
        if self.calls == 1:
            raise CommandTransportError("center unavailable")
        return False


class StationRuntimeTest(unittest.TestCase):
    def test_sse_source_turns_base_chunk_into_a_supervisor_input(self) -> None:
        chunk = json.dumps(
            {
                "choices": [
                    {
                        "delta": {"content": "(1) start"},
                        "chunk_metadata": {
                            "response": "(1) start",
                            "start_time": 1.5,
                            "first_timestamp": 8.0,
                        },
                    }
                ]
            }
        ).encode("utf-8")
        response = FakeSseResponse(
            b"data: " + chunk + b"\n",
            b"\n",
            b"data: [DONE]\n",
            b"\n",
        )
        source = SseStationInputSource(
            inference_url="http://inference.example/v1/chat/completions",
            request_body={"stream": True},
            timeout=1.0,
        )
        with (
            patch("edge_runtime.station_runtime.urllib.request.urlopen", return_value=response),
            patch("edge_runtime.station_runtime.monotonic", return_value=42.0),
        ):
            arriving = source.next_input(timeout=None)
            self.assertEqual(
                ActionRecognized(
                    signal="(1) start",
                    at=HostInstant(42.0),
                    source_time=1.5,
                    source_anchor=8.0,
                ),
                arriving,
            )
            disconnected = source.next_input(timeout=None)
            self.assertEqual(
                StreamHealthObserved(
                    event=StreamHealthEvent(
                        fact=StreamFact.STREAM_ENDED,
                        at_monotonic=42.0,
                    )
                ),
                disconnected,
            )
            source.close()
            self.assertTrue(source.ended)
        self.assertTrue(response.closed)

    def test_sse_source_reports_inference_timeout_when_the_stream_cannot_open(self) -> None:
        source = SseStationInputSource(
            inference_url="http://inference.example/v1/chat/completions",
            request_body={"stream": True},
            timeout=1.0,
        )
        with patch(
            "edge_runtime.station_runtime.urllib.request.urlopen",
            side_effect=TimeoutError,
        ):
            arriving = source.next_input(timeout=None)

        self.assertIsInstance(arriving, StreamHealthObserved)
        assert isinstance(arriving, StreamHealthObserved)
        self.assertEqual(StreamFact.INFERENCE_TIMEOUT, arriving.event.fact)
        source.close()

    def test_sse_source_reports_a_source_error_when_the_inference_stream_cannot_open(self) -> None:
        source = SseStationInputSource(
            inference_url="http://inference.example/v1/chat/completions",
            request_body={"stream": True},
            timeout=1.0,
        )
        with patch(
            "edge_runtime.station_runtime.urllib.request.urlopen",
            side_effect=urllib.error.URLError("offline"),
        ):
            arriving = source.next_input(timeout=None)

        self.assertIsInstance(arriving, StreamHealthObserved)
        assert isinstance(arriving, StreamHealthObserved)
        self.assertEqual(StreamFact.SOURCE_ERROR, arriving.event.fact)
        self.assertFalse(source.ended)
        source.close()
        self.assertTrue(source.ended)

    def test_close_fails_if_http_stream_open_does_not_stop_within_timeout(self) -> None:
        opened = Event()
        release = Event()
        response = FakeSseResponse()

        def open_stream(*args: object, **kwargs: object) -> FakeSseResponse:
            del args, kwargs
            opened.set()
            release.wait()
            return response

        source = SseStationInputSource(
            inference_url="http://inference.example/v1/chat/completions",
            request_body={"stream": True},
            timeout=0.05,
        )
        arriving: list[object] = []
        with patch(
            "edge_runtime.station_runtime.urllib.request.urlopen",
            side_effect=open_stream,
        ):
            reader = Thread(
                target=lambda: arriving.append(source.next_input(timeout=None)),
                daemon=True,
            )
            reader.start()
            self.assertTrue(opened.wait(1.0))
            with self.assertRaisesRegex(RuntimeError, "SSE reader did not stop"):
                source.close()
            reader.join(timeout=1.0)
            self.assertFalse(reader.is_alive())
            self.assertEqual([None], arriving)
            release.set()
            reader.join(timeout=1.0)
            self.assertFalse(reader.is_alive())
            source.close()

        self.assertTrue(response.closed)

    def test_sse_source_timeout_returns_control_to_the_supervisor_deadline(self) -> None:
        response = BlockingSseResponse()
        source = SseStationInputSource(
            inference_url="http://inference.example/v1/chat/completions",
            request_body={"stream": True},
            timeout=1.0,
        )
        started = monotonic()
        with patch("edge_runtime.station_runtime.urllib.request.urlopen", return_value=response):
            timed_out = source.next_input(timeout=0.02)
            expired = source.next_input(timeout=0.0)
        elapsed = monotonic() - started
        self.assertIsInstance(timed_out, StreamHealthObserved)
        assert isinstance(timed_out, StreamHealthObserved)
        self.assertEqual(StreamFact.INFERENCE_TIMEOUT, timed_out.event.fact)
        self.assertIsInstance(expired, InputWaitExpired)
        self.assertTrue(response.started.wait(1.0))
        self.assertLess(elapsed, 0.5)
        self.assertFalse(source.ended)
        response.release.set()
        source.close()
        self.assertTrue(response.closed)

    def test_sse_source_reports_backlog_exceeded_instead_of_growing_unbounded(self) -> None:
        chunk = json.dumps(
            {
                "choices": [
                    {
                        "delta": {"content": "(1) start"},
                        "chunk_metadata": {
                            "response": "(1) start",
                            "start_time": 1.5,
                            "first_timestamp": 8.0,
                        },
                    }
                ]
            }
        ).encode("utf-8")
        response = BackpressureSseResponse(
            b"data: " + chunk + b"\n",
            b"\n",
            b"data: " + chunk + b"\n",
            b"\n",
        )
        source = SseStationInputSource(
            inference_url="http://inference.example/v1/chat/completions",
            request_body={"stream": True},
            timeout=1.0,
            queue_size=1,
        )
        with patch(
            "edge_runtime.station_runtime.urllib.request.urlopen",
            return_value=response,
        ):
            initial = source.next_input(timeout=0.01)
            self.assertIsInstance(initial, StreamHealthObserved)
            assert isinstance(initial, StreamHealthObserved)
            self.assertEqual(StreamFact.INFERENCE_TIMEOUT, initial.event.fact)
            self.assertTrue(response.started.wait(1.0))
            response.allow_first_event.set()
            self.assertTrue(response.first_event_queued.wait(1.0))
            response.release_second_event.set()
            self.assertTrue(response.finished.wait(1.0))
            backlog = source.next_input(timeout=1.0)

        self.assertIsInstance(backlog, StreamHealthObserved)
        assert isinstance(backlog, StreamHealthObserved)
        self.assertEqual(StreamFact.CHUNK_BACKLOG_EXCEEDED, backlog.event.fact)
        self.assertFalse(source.ended)
        source.close()
        self.assertTrue(response.closed)

    def test_sse_source_reports_inference_timeout_for_a_silent_open_stream(self) -> None:
        response = TimeoutSseResponse()
        source = SseStationInputSource(
            inference_url="http://inference.example/v1/chat/completions",
            request_body={"stream": True},
            timeout=1.0,
        )
        with patch("edge_runtime.station_runtime.urllib.request.urlopen", return_value=response):
            arriving = source.next_input(timeout=None)

        self.assertIsInstance(arriving, StreamHealthObserved)
        assert isinstance(arriving, StreamHealthObserved)
        self.assertEqual(StreamFact.INFERENCE_TIMEOUT, arriving.event.fact)
        self.assertFalse(source.ended)
        source.close()
        self.assertTrue(response.closed)

    def test_sse_source_reconnects_after_a_temporary_stream_failure(self) -> None:
        response = FakeSseResponse(
            b"data: "
            + json.dumps(
                {
                    "choices": [
                        {
                            "delta": {"content": "(1) start"},
                            "chunk_metadata": {
                                "response": "(1) start",
                                "start_time": 1.5,
                                "first_timestamp": 8.0,
                            },
                        }
                    ]
                }
            ).encode("utf-8")
            + b"\n",
            b"\n",
        )
        source = SseStationInputSource(
            inference_url="http://inference.example/v1/chat/completions",
            request_body={"stream": True},
            timeout=0.01,
        )
        calls = 0

        def open_stream(*args: object, **kwargs: object) -> FakeSseResponse:
            del args, kwargs
            nonlocal calls
            calls += 1
            if calls == 1:
                raise urllib.error.URLError("offline")
            return response

        with patch(
            "edge_runtime.station_runtime.urllib.request.urlopen",
            side_effect=open_stream,
        ):
            failed = source.next_input(timeout=None)
            self.assertIsInstance(failed, StreamHealthObserved)
            arriving = source.next_input(timeout=1.0)
            self.assertIsInstance(arriving, ActionRecognized)
            self.assertEqual(2, calls)
        source.close()


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
    def test_loader_rejects_an_unsupported_connector_type_at_startup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            config_file = _valid_config(
                directory,
                center_url="https://center.example",
                connector_url="http://camera.example",
            )
            config = json.loads(config_file.read_text(encoding="utf-8"))
            config["connectors"][0]["connector_type"] = "board_card"
            config_file.write_text(json.dumps(config), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "connector_type is unsupported"):
                build_connection_test_loop_from_file(config_file)

    def test_loader_rejects_an_invalid_host_private_key_at_startup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            config_file = _valid_config(
                directory,
                center_url="https://center.example",
                connector_url="http://camera.example",
            )
            config = json.loads(config_file.read_text(encoding="utf-8"))
            Path(config["host_private_key_file"]).write_text("not-a-private-key", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "主机身份密钥"):
                build_connection_test_loop_from_file(config_file)

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
            ("connector_url", "http://camera.example/device", "connector base_url"),
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
