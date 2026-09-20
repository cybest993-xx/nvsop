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
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from random import Random
from threading import Event
from typing import ClassVar, cast
from unittest.mock import patch

from nvsop_contracts import HostIdentityKeyPair, generate_host_identity_key_pair

from edge_runtime.judgment import ReasonCode, Verdict
from edge_runtime.judgment.evidence import EvidenceMargins
from edge_runtime.judgment.model import HostInstant, Ordering, RuntimeParameters, Template
from edge_runtime.local_state.queues import BackendReportContext
from edge_runtime.local_state.store import open_local_state
from edge_runtime.runtime import (
    AutonomousStation,
    _station_input_source,
    build_autonomous_runtime_from_file,
)
from edge_runtime.runtime_configuration import StationRuntimeBinding
from edge_runtime.station_runtime import (
    InputWaitExpired,
    MultiplexedStationInputSource,
    ProvenancedSupervisorInput,
    SseStationInputSource,
    StationRuntimeConfiguration,
)
from edge_runtime.stream_health import StreamFact
from edge_runtime.supervisor.inputs import (
    ActionRecognized,
    StreamHealthObserved,
    SupervisorInput,
    Validity,
    ValidityChanged,
)
from edge_runtime.supervisor.startup import resume_station
from edge_runtime.supervisor.station import Reaction, StationSupervisor


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


class _TricklingSseHandler(BaseHTTPRequestHandler):
    first_byte_sent: ClassVar[Event]
    release: ClassVar[Event]

    def do_POST(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        self.wfile.flush()
        while not self.release.wait(0.05):
            try:
                self.wfile.write(b"x")
                self.wfile.flush()
            except OSError:
                return
            self.first_byte_sent.set()

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


@contextmanager
def _trickling_sse_server() -> Iterator[str]:
    _TricklingSseHandler.first_byte_sent = Event()
    _TricklingSseHandler.release = Event()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _TricklingSseHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1/chat/completions"
    finally:
        _TricklingSseHandler.release.set()
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


class _BackendReachabilityInputSource:
    def __init__(self, backend_id: str) -> None:
        self._backend_id = backend_id
        self._phase = 0
        self.waiting_for_restore = Event()
        self.allow_restore = Event()
        self._closed = Event()

    @property
    def ended(self) -> bool:
        return False

    def next_input(
        self, *, timeout: float | None
    ) -> ProvenancedSupervisorInput | InputWaitExpired | None:
        if self._phase == 0:
            self._phase = 1
            return self._event(Validity.IMPAIRED)
        if self._phase == 1:
            self.waiting_for_restore.set()
            if not self.allow_restore.wait(timeout):
                return InputWaitExpired()
            self._phase = 2
            return self._event(Validity.RESTORED)
        if self._closed.wait(timeout):
            return None
        return InputWaitExpired()

    def close(self) -> None:
        self._closed.set()
        self.allow_restore.set()

    def _event(self, now: Validity) -> ProvenancedSupervisorInput:
        return ProvenancedSupervisorInput(
            arriving=ValidityChanged(
                reason=ReasonCode.INFERENCE_BACKEND_UNREACHABLE,
                now=now,
            ),
            provenance=BackendReportContext(backend_id=self._backend_id, model_ids=()),
        )


class _ReachabilityRaceInputSource:
    def __init__(self, backend_id: str, *, first: SupervisorInput | None = None) -> None:
        self._backend_id = backend_id
        self._first = first
        self._emitted_first = first is None
        self.allow_transition = Event()
        self.transition_ready = Event()
        self._closed = Event()

    @property
    def ended(self) -> bool:
        return False

    def next_input(
        self, *, timeout: float | None
    ) -> ProvenancedSupervisorInput | InputWaitExpired | None:
        if not self._emitted_first:
            self._emitted_first = True
            assert self._first is not None
            return ProvenancedSupervisorInput(
                arriving=self._first,
                provenance=BackendReportContext(backend_id=self._backend_id, model_ids=()),
            )
        self.transition_ready.set()
        if not self.allow_transition.wait(timeout):
            return InputWaitExpired()
        self.allow_transition.clear()
        return ProvenancedSupervisorInput(
            arriving=ValidityChanged(
                reason=ReasonCode.INFERENCE_BACKEND_UNREACHABLE,
                now=Validity.IMPAIRED,
            ),
            provenance=BackendReportContext(backend_id=self._backend_id, model_ids=()),
        )

    def close(self) -> None:
        self._closed.set()
        self.allow_transition.set()


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


class _FinishedInputSource:
    @property
    def ended(self) -> bool:
        return True

    def next_input(self, *, timeout: float | None) -> None:
        del timeout
        return None

    def close(self) -> None:
        pass


class _ExplodingInputSource:
    @property
    def ended(self) -> bool:
        return False

    def next_input(self, *, timeout: float | None) -> None:
        del timeout
        raise RuntimeError("synthetic input failure")

    def close(self) -> None:
        pass


class _DelayedInputSource:
    def __init__(self, *, fail: bool = False) -> None:
        self.started = Event()
        self.release = Event()
        self._ended = False
        self._fail = fail

    @property
    def ended(self) -> bool:
        return self._ended

    def next_input(self, *, timeout: float | None) -> None:
        del timeout
        self.started.set()
        self.release.wait()
        if self._fail:
            raise RuntimeError("delayed synthetic input failure")
        self._ended = True
        return None

    def close(self) -> None:
        self.release.set()


class _CloseTrackingInputSource:
    def __init__(self, *, fail_close: bool = False) -> None:
        self.started = Event()
        self.release = Event()
        self.closed = False
        self._fail_close = fail_close

    @property
    def ended(self) -> bool:
        return self.closed

    def next_input(self, *, timeout: float | None) -> None:
        del timeout
        self.started.set()
        self.release.wait()
        return None

    def close(self) -> None:
        self.closed = True
        self.release.set()
        if self._fail_close:
            raise RuntimeError("synthetic close failure")


class _CoordinatedCloseInputSource(_CloseTrackingInputSource):
    def __init__(self, *, close_started: Event, wait_for_close_started: Event) -> None:
        super().__init__()
        self._close_started = close_started
        self._wait_for_close_started = wait_for_close_started

    def close(self) -> None:
        self._close_started.set()
        if not self._wait_for_close_started.wait(0.2):
            raise RuntimeError("source close was serialized")
        super().close()


class _TrackingSseInputSource:
    instances: ClassVar[list[_TrackingSseInputSource]] = []

    def __init__(
        self, *, inference_url: str, request_body: dict[str, object], timeout: float
    ) -> None:
        del request_body, timeout
        self.inference_url = inference_url
        self.started = Event()
        self.closed = False
        self._emitted = False
        type(self).instances.append(self)

    @property
    def ended(self) -> bool:
        return False

    def next_input(self, *, timeout: float | None) -> SupervisorInput | InputWaitExpired:
        del timeout
        self.started.set()
        if not self._emitted:
            self._emitted = True
            return ActionRecognized(
                signal=self.inference_url,
                at=HostInstant(1.0),
                source_time=1.0,
                source_anchor=1.0,
            )
        return InputWaitExpired()

    def close(self) -> None:
        self.closed = True


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

    def test_close_is_bounded_when_real_http_sse_never_finishes_a_line(self) -> None:
        with _trickling_sse_server() as url:
            source = SseStationInputSource(
                inference_url=url,
                request_body={"stream": True},
                timeout=0.2,
            )
            result: list[object] = []
            reader = threading.Thread(
                target=lambda: result.append(source.next_input(timeout=None)),
                daemon=True,
            )
            reader.start()
            self.assertTrue(_TricklingSseHandler.first_byte_sent.wait(1.0))

            errors: list[BaseException] = []

            def close_source() -> None:
                try:
                    source.close()
                except BaseException as error:
                    errors.append(error)

            closer = threading.Thread(target=close_source, daemon=True)
            closer.start()
            closer.join(timeout=0.75)
            self.assertFalse(closer.is_alive())
            self.assertEqual(1, len(errors))
            self.assertIsInstance(errors[0], RuntimeError)
            self.assertIn("SSE reader did not stop", str(errors[0]))
            reader.join(timeout=1.0)
            self.assertFalse(reader.is_alive())
            self.assertEqual([None], result)

            _TricklingSseHandler.release.set()
            source.close()

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


class MultiplexedStationInputSourceTest(unittest.TestCase):
    def test_backend_reachability_keeps_each_impairment_provenance_and_restores_last(self) -> None:
        first = _BackendReachabilityInputSource("backend-a")
        second = _BackendReachabilityInputSource("backend-b")
        source = MultiplexedStationInputSource(sources=(first, second))
        try:
            impaired = (source.next_input(timeout=1.0), source.next_input(timeout=1.0))
            self.assertTrue(first.waiting_for_restore.wait(1.0))
            self.assertTrue(second.waiting_for_restore.wait(1.0))
            provenanced = tuple(
                item for item in impaired if isinstance(item, ProvenancedSupervisorInput)
            )
            self.assertEqual(2, len(provenanced))
            self.assertEqual(
                {"backend-a", "backend-b"},
                {item.provenance.backend_id for item in provenanced},
            )
            self.assertTrue(
                all(
                    item.arriving
                    == ValidityChanged(
                        reason=ReasonCode.INFERENCE_BACKEND_UNREACHABLE,
                        now=Validity.IMPAIRED,
                    )
                    for item in provenanced
                )
            )

            first.allow_restore.set()
            self.assertIsInstance(source.next_input(timeout=0.1), InputWaitExpired)
            second.allow_restore.set()
            restored = source.next_input(timeout=1.0)

            self.assertIsInstance(restored, ProvenancedSupervisorInput)
            assert isinstance(restored, ProvenancedSupervisorInput)
            self.assertEqual(
                ValidityChanged(
                    reason=ReasonCode.INFERENCE_BACKEND_UNREACHABLE,
                    now=Validity.RESTORED,
                ),
                restored.arriving,
            )
        finally:
            source.close()

    def test_reachability_transition_order_is_serialized_with_queue_publication(self) -> None:
        first = _BackendReachabilityInputSource("backend-a")
        filler = ActionRecognized(
            signal="(1) start",
            at=HostInstant(1.0),
            source_time=1.0,
            source_anchor=1.0,
        )
        second = _ReachabilityRaceInputSource("backend-b", first=filler)
        source = MultiplexedStationInputSource(sources=(first, second), queue_size=1)
        try:
            impaired = source.next_input(timeout=1.0)
            self.assertIsInstance(impaired, ProvenancedSupervisorInput)
            self.assertTrue(first.waiting_for_restore.wait(1.0))
            self.assertTrue(second.transition_ready.wait(1.0))

            first.allow_restore.set()
            second.allow_transition.set()
            queued = source.next_input(timeout=1.0)
            restored = source.next_input(timeout=1.0)
            impaired_second = source.next_input(timeout=1.0)

            self.assertIsInstance(queued, ProvenancedSupervisorInput)
            assert isinstance(queued, ProvenancedSupervisorInput)
            self.assertIsInstance(queued.arriving, ActionRecognized)
            self.assertIsInstance(restored, ProvenancedSupervisorInput)
            assert isinstance(restored, ProvenancedSupervisorInput)
            self.assertEqual(Validity.RESTORED, cast(ValidityChanged, restored.arriving).now)
            self.assertIsInstance(impaired_second, ProvenancedSupervisorInput)
            assert isinstance(impaired_second, ProvenancedSupervisorInput)
            self.assertEqual(Validity.IMPAIRED, cast(ValidityChanged, impaired_second.arriving).now)
        finally:
            source.close()

    def test_blocked_wait_wakes_when_a_source_later_ends_or_fails(self) -> None:
        def wait_for_input(
            source: MultiplexedStationInputSource,
            result: list[object],
            errors: list[BaseException],
        ) -> None:
            try:
                result.append(source.next_input(timeout=None))
            except BaseException as error:
                errors.append(error)

        for fail, expected in ((False, None), (True, "delayed synthetic input failure")):
            child = _DelayedInputSource(fail=fail)
            source = MultiplexedStationInputSource(sources=(child,))
            result: list[object] = []
            errors: list[BaseException] = []

            reader = threading.Thread(
                target=wait_for_input,
                args=(source, result, errors),
                daemon=True,
            )
            reader.start()
            self.assertTrue(child.started.wait(1.0))
            self.assertTrue(reader.is_alive())
            child.release.set()
            reader.join(1.0)
            source.close()

            self.assertFalse(reader.is_alive())
            if expected is None:
                self.assertEqual([None], result)
                self.assertEqual([], errors)
            else:
                self.assertEqual([], result)
                self.assertEqual([expected], [str(error) for error in errors])

    def test_close_finishes_all_children_and_workers_before_propagating_failure(self) -> None:
        failing = _CloseTrackingInputSource(fail_close=True)
        later = _CloseTrackingInputSource()
        source = MultiplexedStationInputSource(sources=(failing, later))

        self.assertIsInstance(source.next_input(timeout=0.01), InputWaitExpired)
        self.assertTrue(failing.started.wait(1.0))
        self.assertTrue(later.started.wait(1.0))

        with self.assertRaisesRegex(RuntimeError, "synthetic close failure"):
            source.close()

        self.assertTrue(failing.closed)
        self.assertTrue(later.closed)
        self.assertTrue(source.ended)

    def test_close_starts_all_child_shutdowns_before_waiting(self) -> None:
        first_started = Event()
        second_started = Event()
        first = _CoordinatedCloseInputSource(
            close_started=first_started,
            wait_for_close_started=second_started,
        )
        second = _CoordinatedCloseInputSource(
            close_started=second_started,
            wait_for_close_started=first_started,
        )
        source = MultiplexedStationInputSource(sources=(first, second))

        source.close()

        self.assertTrue(first.closed)
        self.assertTrue(second.closed)
        self.assertTrue(source.ended)

    def test_wait_without_timeout_wakes_when_sources_end_or_fail(self) -> None:
        ended = MultiplexedStationInputSource(
            sources=(_FinishedInputSource(), _FinishedInputSource())
        )
        self.assertIsNone(ended.next_input(timeout=None))
        ended.close()

        failed = MultiplexedStationInputSource(sources=(_ExplodingInputSource(),))
        with self.assertRaisesRegex(RuntimeError, "synthetic input failure"):
            failed.next_input(timeout=None)
        failed.close()


class MultiBackendRuntimeCompositionTest(unittest.TestCase):
    def test_two_backend_sse_sources_start_and_feed_one_station_source(self) -> None:
        first = StationRuntimeConfiguration(
            station_id="station-multi",
            backend_id="backend-a",
            inference_url="http://backend-a.example/v1/chat/completions",
            request_body={"stream": True, "messages": []},
            template=Template(
                steps=("(1) start",),
                ordering=Ordering.ORDERED,
                start_signal="(1) start",
            ),
            parameters=RuntimeParameters(idle_timeout=10.0, step_deadline=5.0),
            margins=EvidenceMargins(leading=0.0, trailing=0.0),
            model_ids=("model-a",),
        )
        second = replace(
            first,
            backend_id="backend-b",
            inference_url="http://backend-b.example/v1/chat/completions",
            model_ids=("model-b",),
        )
        binding = StationRuntimeBinding(
            configuration=first,
            input_points=(),
            output_points=(),
            connector_ids=(),
            configurations=(first, second),
        )
        _TrackingSseInputSource.instances = []
        with patch("edge_runtime.runtime.SseStationInputSource", _TrackingSseInputSource):
            source = _station_input_source(binding, timeout=0.1)
            try:
                self.assertIsInstance(source, MultiplexedStationInputSource)
                events = (
                    source.next_input(timeout=0.2),
                    source.next_input(timeout=0.2),
                )
                provenanced = tuple(
                    event for event in events if isinstance(event, ProvenancedSupervisorInput)
                )
                self.assertEqual(2, len(provenanced))
                by_backend = {event.provenance.backend_id: event for event in provenanced}
                self.assertEqual(
                    "http://backend-a.example/v1/chat/completions",
                    cast(ActionRecognized, by_backend["backend-a"].arriving).signal,
                )
                self.assertEqual(
                    "http://backend-b.example/v1/chat/completions",
                    cast(ActionRecognized, by_backend["backend-b"].arriving).signal,
                )
                self.assertEqual(("model-a",), by_backend["backend-a"].provenance.model_ids)
                self.assertEqual(("model-b",), by_backend["backend-b"].provenance.model_ids)
                self.assertEqual(2, len(_TrackingSseInputSource.instances))
                self.assertTrue(
                    all(
                        instance.started.wait(1.0) for instance in _TrackingSseInputSource.instances
                    )
                )
            finally:
                source.close()
        self.assertTrue(all(instance.closed for instance in _TrackingSseInputSource.instances))


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


class NormalizedContractReplayTest(unittest.TestCase):
    """同一 SSE 契约输入经真实输入 seam 重放时必须得到同一完整反应。"""

    @staticmethod
    def _payload() -> bytes:
        first_anchor = 1_700_000_000.0
        second_anchor = first_anchor + 30.0

        def health(fact: str, *, at_monotonic: float, source_anchor: float) -> bytes:
            return (
                b"data: "
                + json.dumps(
                    {
                        "choices": [
                            {
                                "chunk_metadata": {
                                    "stream_health": {
                                        "fact": fact,
                                        "at_monotonic": at_monotonic,
                                        "source_anchor": source_anchor,
                                    }
                                }
                            }
                        ]
                    }
                ).encode("utf-8")
                + b"\n\n"
            )

        return b"".join(
            (
                _action("(1) start", source_time=1.0, source_anchor=first_anchor),
                health("source_error", at_monotonic=101.0, source_anchor=first_anchor),
                health("delivering", at_monotonic=102.0, source_anchor=first_anchor),
                _action("(2) finish", source_time=2.0, source_anchor=second_anchor),
                b"data: [DONE]\n\n",
            )
        )

    @staticmethod
    def _replay(payload: bytes, path: Path) -> tuple[Reaction, ...]:
        state = open_local_state(str(path))
        try:
            supervisor = resume_station(
                state.station("station-replay"),
                template=Template(
                    steps=("(1) start", "(2) finish"),
                    ordering=Ordering.ORDERED,
                    start_signal="(1) start",
                ),
                parameters=RuntimeParameters(idle_timeout=10.0, step_deadline=10.0),
                margins=EvidenceMargins(leading=0.0, trailing=0.0),
            )
            with _sse_server([payload]) as url:
                source = SseStationInputSource(
                    inference_url=url,
                    request_body={"stream": True},
                    timeout=1.0,
                )
                try:
                    reactions: list[Reaction] = []
                    with patch(
                        "edge_runtime.station_runtime.monotonic",
                        side_effect=(0.0, 100.0, 103.0),
                    ):
                        for _ in range(4):
                            arriving = source.next_input(timeout=None)
                            if not isinstance(arriving, (ActionRecognized, StreamHealthObserved)):
                                raise AssertionError(f"unexpected SSE input: {arriving!r}")
                            reactions.append(supervisor.receive(arriving))
                    return tuple(reactions)
                finally:
                    source.close()
        finally:
            state.close()

    def test_same_sse_replay_reproduces_verdict_reasons_evidence_and_next_wake(self) -> None:
        payload = self._payload()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = self._replay(payload, root / "first.sqlite")
            second = self._replay(payload, root / "second.sqlite")

        self.assertEqual(first, second)
        self.assertEqual(HostInstant(110.0), first[0].wake_at)
        final = first[-1]
        self.assertIsNone(final.wake_at)
        self.assertEqual(1, len(final.decisions))
        decision = final.decisions[0]
        self.assertIs(Verdict.INDETERMINATE, decision.verdict)
        self.assertEqual(
            {ReasonCode.STREAM_LOST, ReasonCode.TIMESTAMP_DISCONTINUITY},
            set(decision.reasons),
        )
        self.assertEqual(HostInstant(103.0), decision.evidence.anchor)
        self.assertEqual(HostInstant(103.0), decision.evidence.required_from)
        self.assertEqual(HostInstant(103.0), decision.evidence.required_to)


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
