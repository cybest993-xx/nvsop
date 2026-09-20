"""推理机工位实时输入和本地配置解析。"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from queue import Empty, Full, Queue
from threading import Event, Lock, Thread, current_thread
from time import monotonic
from typing import Protocol, cast

from edge_runtime.configuration_values import (
    _array,
    _is_number,
    _non_empty_string,
    _non_negative_number,
    _object,
    _positive_number,
    _require_keys,
    safe_url,
)
from edge_runtime.judgment.evidence import EvidenceMargins
from edge_runtime.judgment.model import HostInstant, Ordering, RuntimeParameters, Template
from edge_runtime.judgment.reasons import ReasonCode
from edge_runtime.local_state.queues import BackendReportContext
from edge_runtime.stream_health import StreamFact, StreamHealthEvent, decode
from edge_runtime.supervisor.inputs import (
    ActionRecognized,
    StreamHealthObserved,
    SupervisorInput,
    Validity,
    ValidityChanged,
)


@dataclass(frozen=True, slots=True)
class StationRuntimeConfiguration:
    """一条工位实时判定流及其最后确认的本地配置。"""

    station_id: str
    inference_url: str
    request_body: dict[str, object]
    template: Template
    parameters: RuntimeParameters
    margins: EvidenceMargins
    backend_id: str | None = None
    template_version_id: str | None = None
    template_sha256: str | None = None
    model_ids: tuple[str, ...] = ()
    disposition_policy: str | None = None


@dataclass(frozen=True, slots=True)
class InputWaitExpired:
    """输入源等待到期, 主循环应触发 supervisor 的核心计时器。"""


@dataclass(frozen=True, slots=True)
class ProvenancedSupervisorInput:
    """仅供运行时/持久化层携带的 backend 来源; judgment core 不消费它。"""

    arriving: SupervisorInput
    provenance: BackendReportContext


class StationInputSource(Protocol):
    """实时判定循环所需的最小输入接缝。"""

    @property
    def ended(self) -> bool: ...

    def next_input(
        self, *, timeout: float | None
    ) -> SupervisorInput | ProvenancedSupervisorInput | InputWaitExpired | None: ...

    def close(self) -> None: ...


class _SseResponse(Protocol):
    """SSE 响应在本运行时使用的最小接口。"""

    def readline(self) -> bytes: ...

    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class _Disconnected:
    """一条 SSE 连接结束, 主循环应记录失联并稍后重连。"""

    fact: StreamFact
    backend_unreachable: bool = False


class SseStationInputSource(StationInputSource):
    """从基座聊天流读取动作和合成健康事件, 并在断开后重连。"""

    def __init__(
        self,
        *,
        inference_url: str,
        request_body: Mapping[str, object],
        timeout: float,
        queue_size: int = 64,
    ) -> None:
        if timeout <= 0:
            raise ValueError("station stream timeout must be positive")
        if queue_size <= 0:
            raise ValueError("station input queue size must be positive")
        self._inference_url = inference_url
        self._request_body = dict(request_body)
        self._timeout = timeout
        self._events: Queue[SupervisorInput | _Disconnected] = Queue(maxsize=queue_size)
        self._reader: Thread | None = None
        self._closed = False
        self._ended = False
        self._next_retry_at = 0.0
        self._retry_delay = min(max(timeout, 0.1), 5.0)
        self._retry_wakeup = Event()
        self._timeout_reported = False
        self._backend_unreachable = False

    @property
    def ended(self) -> bool:
        return self._ended

    def next_input(
        self, *, timeout: float | None
    ) -> SupervisorInput | ProvenancedSupervisorInput | InputWaitExpired | None:
        """在核心 deadline 到达时返回 None, 而不是阻塞在网络读取上。"""
        if self._closed:
            return None
        if self._reader is None:
            if not self._wait_for_retry(timeout):
                return None if self._closed else InputWaitExpired()
            self._start_reader()
        try:
            arriving = self._events.get(timeout=None if timeout is None else max(0.0, timeout))
        except Empty:
            if self._reader is not None:
                if not self._timeout_reported:
                    self._timeout_reported = True
                    return self._source_error(StreamFact.INFERENCE_TIMEOUT)
                return InputWaitExpired()
            return InputWaitExpired()
        if isinstance(arriving, _Disconnected):
            if not self._closed:
                self._reader = None
            self._timeout_reported = False
            if self._closed:
                return None
            self._next_retry_at = monotonic() + self._retry_delay
            if arriving.backend_unreachable:
                if self._backend_unreachable:
                    return InputWaitExpired()
                self._backend_unreachable = True
                return ValidityChanged(
                    reason=ReasonCode.INFERENCE_BACKEND_UNREACHABLE,
                    now=Validity.IMPAIRED,
                )
            return self._source_error(arriving.fact)
        self._timeout_reported = False
        return arriving

    def close(self) -> None:
        """关闭当前连接并禁止后续重连。"""
        self._closed = True
        self._ended = True
        self._retry_wakeup.set()
        self._timeout_reported = False
        try:
            self._events.put_nowait(_Disconnected(StreamFact.STREAM_ENDED))
        except Full:
            self._discard_events()
            self._events.put_nowait(_Disconnected(StreamFact.STREAM_ENDED))
        reader = self._reader
        if reader is not None and reader is not current_thread():
            reader.join(timeout=self._timeout + 0.1)
            if reader.is_alive():
                raise RuntimeError("station SSE reader did not stop after close")
            self._reader = None

    def _wait_for_retry(self, timeout: float | None) -> bool:
        delay = max(0.0, self._next_retry_at - monotonic())
        if delay == 0:
            return not self._closed
        if timeout is not None and timeout <= delay:
            self._retry_wakeup.wait(timeout)
            return False
        self._retry_wakeup.wait(delay)
        return not self._closed

    def _start_reader(self) -> None:
        reader = Thread(target=self._read_stream, name="edge-sse-reader", daemon=True)
        self._reader = reader
        reader.start()

    def _read_stream(self) -> None:
        response: _SseResponse | None = None
        disconnect_fact = StreamFact.SOURCE_ERROR
        backend_unreachable = False
        try:
            request = urllib.request.Request(
                self._inference_url,
                data=json.dumps(
                    self._request_body, ensure_ascii=False, separators=(",", ":")
                ).encode("utf-8"),
                headers={"Accept": "text/event-stream", "Content-Type": "application/json"},
                method="POST",
            )
            try:
                response = cast(
                    _SseResponse,
                    urllib.request.urlopen(request, timeout=self._timeout),
                )
            except TimeoutError:
                disconnect_fact = StreamFact.INFERENCE_TIMEOUT
                return
            except urllib.error.URLError as error:
                if isinstance(error.reason, TimeoutError):
                    disconnect_fact = StreamFact.INFERENCE_TIMEOUT
                else:
                    backend_unreachable = True
                return
            except OSError:
                backend_unreachable = True
                return
            if self._backend_unreachable and not self._closed:
                if not self._enqueue(
                    ValidityChanged(
                        reason=ReasonCode.INFERENCE_BACKEND_UNREACHABLE,
                        now=Validity.RESTORED,
                    )
                ):
                    disconnect_fact = StreamFact.CHUNK_BACKLOG_EXCEEDED
                    return
                self._backend_unreachable = False
            data_lines: list[str] = []
            while not self._closed:
                try:
                    line = response.readline()
                except TimeoutError:
                    disconnect_fact = StreamFact.INFERENCE_TIMEOUT
                    break
                except (OSError, AttributeError):
                    # Python HTTPResponse 在关闭竞态中可能抛出 AttributeError; 停机时按断流处理。
                    break
                if not line:
                    break
                decoded = line.decode("utf-8", errors="replace").rstrip("\r\n")
                if decoded.startswith("data:"):
                    data_lines.append(decoded[5:].lstrip())
                if decoded or not data_lines:
                    continue
                data = "\n".join(data_lines)
                data_lines.clear()
                if data == "[DONE]":
                    disconnect_fact = StreamFact.STREAM_ENDED
                    break
                try:
                    document = json.loads(data)
                except json.JSONDecodeError:
                    arriving: SupervisorInput = self._source_error()
                else:
                    arriving = self._input_from_document(document)
                if not self._enqueue(arriving):
                    disconnect_fact = StreamFact.CHUNK_BACKLOG_EXCEEDED
                    break
        finally:
            if response is not None:
                with suppress(OSError, AttributeError):
                    response.close()
            if not self._closed:
                self._enqueue_disconnect(
                    disconnect_fact,
                    backend_unreachable=backend_unreachable,
                )

    def _enqueue(self, arriving: SupervisorInput) -> bool:
        try:
            self._events.put_nowait(arriving)
        except Full:
            self._discard_events()
            self._events.put_nowait(self._source_error(StreamFact.CHUNK_BACKLOG_EXCEEDED))
            return False
        return True

    def _discard_events(self) -> None:
        while True:
            try:
                self._events.get_nowait()
            except Empty:
                return

    def _enqueue_disconnect(self, fact: StreamFact, *, backend_unreachable: bool = False) -> None:
        disconnected = _Disconnected(fact, backend_unreachable=backend_unreachable)
        try:
            self._events.put_nowait(disconnected)
        except Full:
            self._discard_events()
            self._events.put_nowait(disconnected)

    def _source_error(self, fact: StreamFact = StreamFact.SOURCE_ERROR) -> StreamHealthObserved:
        return StreamHealthObserved(event=StreamHealthEvent(fact=fact, at_monotonic=monotonic()))

    def _input_from_document(self, value: object) -> SupervisorInput:
        if not isinstance(value, Mapping):
            return self._source_error()
        choices = value.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], Mapping):
            return self._source_error()
        choice = choices[0]
        metadata = choice.get("chunk_metadata")
        if not isinstance(metadata, Mapping):
            metadata = {}
        health = decode(metadata)
        if health is not None:
            return StreamHealthObserved(event=health)
        delta = choice.get("delta")
        signal = metadata.get("response")
        if signal is None and isinstance(delta, Mapping):
            signal = delta.get("content")
        source_time = metadata.get("start_time", metadata.get("end_time"))
        source_anchor = metadata.get("first_timestamp")
        if (
            not isinstance(signal, str)
            or not signal.strip()
            or not _is_number(source_time)
            or not _positive_number(source_anchor, "source_anchor")
        ):
            return self._source_error()
        return ActionRecognized(
            signal=signal.strip(),
            at=HostInstant(monotonic()),
            source_time=float(cast(int | float, source_time)),
            source_anchor=float(cast(int | float, source_anchor)),
        )


class ProvenancedStationInputSource(StationInputSource):
    """给一个 backend 的所有推理输入附加固定的事件时来源。"""

    def __init__(self, *, source: StationInputSource, provenance: BackendReportContext) -> None:
        self._source = source
        self._provenance = provenance

    @property
    def ended(self) -> bool:
        return self._source.ended

    def next_input(
        self, *, timeout: float | None
    ) -> ProvenancedSupervisorInput | InputWaitExpired | None:
        arriving = self._source.next_input(timeout=timeout)
        if arriving is None or isinstance(arriving, InputWaitExpired):
            return arriving
        if isinstance(arriving, ProvenancedSupervisorInput):
            raise ValueError("station input already has backend provenance")
        return ProvenancedSupervisorInput(arriving=arriving, provenance=self._provenance)

    def close(self) -> None:
        self._source.close()


class MultiplexedStationInputSource(StationInputSource):
    """把同一工位多个后端的输入合并到一个 supervisor 队列。"""

    def __init__(self, *, sources: tuple[StationInputSource, ...], queue_size: int = 128) -> None:
        if not sources:
            raise ValueError("multiplexed station input needs at least one source")
        if queue_size <= 0:
            raise ValueError("multiplexed station input queue size must be positive")
        self._sources = sources
        self._events: Queue[SupervisorInput | ProvenancedSupervisorInput] = Queue(
            maxsize=queue_size
        )
        self._wake = Event()
        self._stopping = Event()
        self._state_lock = Lock()
        self._closed = False
        self._ended = False
        self._remaining = len(sources)
        self._error: BaseException | None = None
        self._workers: tuple[Thread, ...] = ()
        self._started = False

    @property
    def ended(self) -> bool:
        with self._state_lock:
            return self._ended

    def next_input(
        self, *, timeout: float | None
    ) -> SupervisorInput | ProvenancedSupervisorInput | InputWaitExpired | None:
        deadline = None if timeout is None else monotonic() + timeout
        with self._state_lock:
            if not self._started and not self._closed:
                self._workers = tuple(
                    Thread(
                        target=self._pump,
                        args=(source,),
                        name=f"edge-station-source-{index}",
                        daemon=True,
                    )
                    for index, source in enumerate(self._sources)
                )
                self._started = True
                for worker in self._workers:
                    worker.start()
        while True:
            with self._state_lock:
                error = self._error
                closed = self._closed
                ended = self._ended
            if error is not None:
                raise error
            try:
                return self._events.get_nowait()
            except Empty:
                if (closed or ended) and self._events.empty():
                    return None
            if deadline is not None:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    return InputWaitExpired()
                wait_for = min(remaining, 0.5)
            else:
                wait_for = 0.5
            self._wake.wait(wait_for)
            self._wake.clear()

    def close(self) -> None:
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
        self._stopping.set()
        self._wake.set()
        errors: list[BaseException | None] = [None] * len(self._sources)

        def close_source(index: int, source: StationInputSource) -> None:
            try:
                source.close()
            except BaseException as error:
                errors[index] = error

        closers = tuple(
            Thread(target=close_source, args=(index, source), name=f"edge-station-close-{index}")
            for index, source in enumerate(self._sources)
        )
        for closer in closers:
            closer.start()
        for closer in closers:
            closer.join()
        for worker in self._workers:
            if worker is not current_thread():
                worker.join(timeout=1.0)
        with self._state_lock:
            self._ended = True
        for error in errors:
            if error is not None:
                raise error

    def _pump(self, source: StationInputSource) -> None:
        try:
            while not self._stopping.is_set():
                arriving = source.next_input(timeout=0.5)
                if isinstance(arriving, InputWaitExpired):
                    continue
                if arriving is None:
                    if source.ended:
                        break
                    continue
                while not self._stopping.is_set():
                    try:
                        self._events.put(arriving, timeout=0.2)
                        self._wake.set()
                        break
                    except Full:
                        continue
        except BaseException as error:
            with self._state_lock:
                self._error = error
            self._wake.set()
        finally:
            with self._state_lock:
                self._remaining -= 1
                if self._remaining == 0:
                    self._ended = True
            self._wake.set()


def station_configuration(value: object) -> StationRuntimeConfiguration:
    """解析一条工位实时判定配置并拒绝未知字段。"""
    config = _object(value, "station configuration")
    _require_keys(
        config,
        required={"station_id", "inference_url", "request", "template", "parameters", "margins"},
        optional={
            "backend_id",
            "template_version_id",
            "template_sha256",
            "model_ids",
            "disposition_policy",
        },
    )
    request_body = _object(config["request"], "station request")
    if request_body.get("stream") is not True:
        raise ValueError("station request must enable streaming")
    try:
        json.dumps(request_body, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError) as error:
        raise ValueError("station request must be JSON") from error

    template_config = _object(config["template"], "station template")
    _require_keys(
        template_config,
        required={"steps", "ordering", "start_signal", "end_signals"},
    )
    steps = tuple(
        _non_empty_string(item, "template step")
        for item in _array(template_config["steps"], "template steps")
    )
    try:
        ordering = Ordering(_non_empty_string(template_config["ordering"], "template ordering"))
    except ValueError as error:
        raise ValueError("template ordering is unsupported") from error
    end_signals = tuple(
        _non_empty_string(item, "template end signal")
        for item in _array(template_config["end_signals"], "template end signals")
    )
    template = Template(
        steps=steps,
        ordering=ordering,
        start_signal=_non_empty_string(template_config["start_signal"], "template start signal"),
        end_signals=end_signals,
    )

    parameters = _object(config["parameters"], "station parameters")
    _require_keys(parameters, required={"idle_timeout", "step_deadline"})
    resolved_parameters = RuntimeParameters(
        idle_timeout=_positive_number(parameters["idle_timeout"], "idle_timeout"),
        step_deadline=_positive_number(parameters["step_deadline"], "step_deadline"),
    )
    margins = _object(config["margins"], "station evidence margins")
    _require_keys(margins, required={"leading", "trailing"})
    evidence_margins = EvidenceMargins(
        leading=_non_negative_number(margins["leading"], "evidence leading margin"),
        trailing=_non_negative_number(margins["trailing"], "evidence trailing margin"),
    )
    model_ids: tuple[str, ...] = ()
    if "model_ids" in config:
        model_ids = tuple(
            _non_empty_string(item, "model id") for item in _array(config["model_ids"], "model_ids")
        )
    return StationRuntimeConfiguration(
        station_id=_non_empty_string(config["station_id"], "station_id"),
        inference_url=safe_url(config["inference_url"], "inference_url", schemes={"http", "https"}),
        request_body=request_body,
        template=template,
        parameters=resolved_parameters,
        margins=evidence_margins,
        backend_id=(
            None
            if config.get("backend_id") is None
            else _non_empty_string(config["backend_id"], "backend_id")
        ),
        template_version_id=(
            None
            if config.get("template_version_id") is None
            else _non_empty_string(config["template_version_id"], "template_version_id")
        ),
        template_sha256=(
            None
            if config.get("template_sha256") is None
            else _non_empty_string(config["template_sha256"], "template_sha256")
        ),
        model_ids=model_ids,
        disposition_policy=(
            None
            if config.get("disposition_policy") is None
            else _non_empty_string(config["disposition_policy"], "disposition_policy")
        ),
    )


__all__ = [
    "InputWaitExpired",
    "MultiplexedStationInputSource",
    "ProvenancedStationInputSource",
    "ProvenancedSupervisorInput",
    "SseStationInputSource",
    "StationInputSource",
    "StationRuntimeConfiguration",
    "station_configuration",
]
