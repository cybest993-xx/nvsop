"""推理机工位实时输入和本地配置解析。"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from queue import Empty, Full, Queue
from threading import Event, Thread, current_thread
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
from edge_runtime.stream_health import StreamFact, StreamHealthEvent, decode
from edge_runtime.supervisor.inputs import ActionRecognized, StreamHealthObserved, SupervisorInput


@dataclass(frozen=True, slots=True)
class StationRuntimeConfiguration:
    """一条工位实时判定流及其最后确认的本地配置。"""

    station_id: str
    inference_url: str
    request_body: dict[str, object]
    template: Template
    parameters: RuntimeParameters
    margins: EvidenceMargins


@dataclass(frozen=True, slots=True)
class InputWaitExpired:
    """输入源等待到期, 主循环应触发 supervisor 的核心计时器。"""


class StationInputSource(Protocol):
    """实时判定循环所需的最小输入接缝。"""

    @property
    def ended(self) -> bool: ...

    def next_input(self, *, timeout: float | None) -> SupervisorInput | InputWaitExpired | None: ...

    def close(self) -> None: ...


class _SseResponse(Protocol):
    """SSE 响应在本运行时使用的最小接口。"""

    def readline(self) -> bytes: ...

    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class _Disconnected:
    """一条 SSE 连接结束, 主循环应记录失联并稍后重连。"""

    fact: StreamFact


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
        self._response: _SseResponse | None = None
        self._closed = False
        self._ended = False
        self._next_retry_at = 0.0
        self._retry_delay = min(max(timeout, 0.1), 5.0)
        self._retry_wakeup = Event()
        self._timeout_reported = False

    @property
    def ended(self) -> bool:
        return self._ended

    def next_input(self, *, timeout: float | None) -> SupervisorInput | InputWaitExpired | None:
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
        response = self._response
        if response is not None:
            with suppress(OSError, AttributeError):
                response.close()
            self._response = None
        reader = self._reader
        if reader is not None and reader is not current_thread():
            reader.join(timeout=self._timeout + 0.1)

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
                return
            except OSError:
                return
            self._response = response
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
            if self._response is response:
                self._response = None
            if not self._closed:
                self._enqueue_disconnect(disconnect_fact)

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

    def _enqueue_disconnect(self, fact: StreamFact) -> None:
        try:
            self._events.put_nowait(_Disconnected(fact))
        except Full:
            self._discard_events()
            self._events.put_nowait(_Disconnected(fact))

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


def station_configuration(value: object) -> StationRuntimeConfiguration:
    """解析一条工位实时判定配置并拒绝未知字段。"""
    config = _object(value, "station configuration")
    _require_keys(
        config,
        required={"station_id", "inference_url", "request", "template", "parameters", "margins"},
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
    return StationRuntimeConfiguration(
        station_id=_non_empty_string(config["station_id"], "station_id"),
        inference_url=safe_url(config["inference_url"], "inference_url", schemes={"http", "https"}),
        request_body=request_body,
        template=template,
        parameters=resolved_parameters,
        margins=evidence_margins,
    )


__all__ = [
    "InputWaitExpired",
    "SseStationInputSource",
    "StationInputSource",
    "StationRuntimeConfiguration",
    "station_configuration",
]
