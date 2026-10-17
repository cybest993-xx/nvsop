"""本机录像压缩控制的中心传输与确认; 不拥有编码任务或素材。"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from contextlib import suppress
from time import monotonic, sleep
from typing import Protocol, assert_never

from nvsop_contracts.compression import (
    CompressionClaim,
    CompressionResult,
    compression_action_confirmed,
    compression_claim_from_wire,
)

from edge_runtime.center_client import CenterClient, CenterUnreachableError
from edge_runtime.recording_compression import (
    CompressionStatus,
    RecordingCompressionError,
)
from edge_runtime.supervisor.delegated_transport import CommandTransportError

_PATH = "/api/v1/device-commands/compression"
# 控制动作只改变本机进程状态; 确认等待用本机单调时钟, 不与中心 epoch 取差值。
_CONTROL_CONFIRM_SECONDS = 30.0
_logger = logging.getLogger("edge_runtime")


class CompressionControl(Protocol):
    """控制轮需要的最小本机面; 不要求它拥有编码任务或素材。"""

    @property
    def readrate(self) -> float | None: ...

    def status(self) -> CompressionStatus: ...

    def pause(self) -> CompressionStatus: ...

    def resume(self) -> CompressionStatus: ...

    def abort(self) -> CompressionStatus: ...


class CompressionCommandLoop:
    """专用命令类型只控制已有 worker; 回报成功前保留同一观测, 不领取后继命令。"""

    def __init__(
        self,
        *,
        client: CenterClient,
        worker: CompressionControl | None,
        interval: float,
    ) -> None:
        self._client = client
        self._worker = worker
        self._interval = interval
        self._pending: tuple[CompressionClaim, CompressionResult] | None = None

    def run_once(self, *, should_stop: Callable[[], bool] = lambda: False) -> bool:
        try:
            if self._pending is None:
                response = self._client.get(f"{_PATH}/next")
                if response.status == 204:
                    return False
                if response.status != 200:
                    raise CommandTransportError("压缩命令领取失败", status=response.status)
                try:
                    claim = compression_claim_from_wire(json.loads(response.body))
                except (ValueError, TypeError) as error:
                    raise CommandTransportError("压缩命令契约非法") from error
                if should_stop():
                    return False
                result = self._execute(claim, should_stop)
                self._pending = claim, result
            claim, result = self._pending
            response = self._client.post(
                f"{_PATH}/{claim.command_id}/result",
                result.to_wire(),
                headers={"X-Command-Claim-Token": claim.claim_token},
            )
            if response.status != 200:
                if response.status < 500:
                    # 中心是租约权威: 4xx 说明这次领取或观测已不适用, 交回队列重新领取,
                    # 不用本机时钟另判过期, 也不把陈旧结果写成当前观测。
                    self._pending = None
                    _logger.info(
                        "edge.recording_compression.report_rejected command_id=%s status=%s",
                        claim.command_id,
                        response.status,
                    )
                raise CommandTransportError("压缩结果回报失败", status=response.status)
            self._pending = None
            return True
        except CenterUnreachableError as error:
            raise CommandTransportError("中心压缩接口暂不可达") from error

    def _execute(
        self, claim: CompressionClaim, should_stop: Callable[[], bool]
    ) -> CompressionResult:
        worker = self._worker
        if worker is None:
            return CompressionResult(
                "unavailable",
                "unavailable",
                None,
                None,
                None,
                "compression_unavailable",
                "本机未启用录像压缩或未配置证据切片",
            )
        try:
            match claim.action:
                case "pause":
                    worker.pause()
                case "resume":
                    worker.resume()
                case "abort":
                    worker.abort()
                case "inspect":
                    pass
                case _:
                    assert_never(claim.action)
            deadline = monotonic() + _CONTROL_CONFIRM_SECONDS
            while worker.status().state in {"pausing", "resuming", "aborting"}:
                if should_stop() or monotonic() >= deadline:
                    return self._snapshot("control_unconfirmed", "进程状态尚未确认, 请重新刷新")
                sleep(0.05)
            if not compression_action_confirmed(claim.action, worker.status().state):
                return self._snapshot(
                    "control_unconfirmed", "任务仍被占用或无法确认, 请检查本机状态"
                )
            return self._snapshot()
        except (OSError, RecordingCompressionError) as error:
            return self._snapshot("compression_control_failed", str(error))

    def _snapshot(self, failure: str | None = None, detail: str | None = None) -> CompressionResult:
        assert self._worker is not None
        observed = self._worker.status()
        return CompressionResult(
            observed.state,
            observed.requested,
            observed.segment,
            observed.pid,
            self._worker.readrate,
            failure or ("control_state_invalid" if observed.state == "unavailable" else None),
            (detail or observed.detail or "")[:255] or None,
        )

    def run_forever(self, *, should_stop: Callable[[], bool]) -> None:
        while not should_stop():
            with suppress(CommandTransportError):
                self.run_once(should_stop=should_stop)
            deadline = monotonic() + self._interval
            while not should_stop() and monotonic() < deadline:
                sleep(0.05)
