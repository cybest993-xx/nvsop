"""推理机再切片委托命令的执行接缝 (S038)。

目标推理机领取中心委托的再切片命令后, 复用首次切片能力 (`slice_evidence`), 按固定原锚点与
中心已并集好的新窗口从本机滚动录像原子定稿新媒体代次, 再以 ``origin=reclip`` 登记中心引用。
稳定请求 ID 的意图与产物先持久化到本机 ``local_reclip_request``: 一旦定稿, 该产物即权威,
重启/重复领取/登记确认丢失都直接复用, 不再查询或重切可能已过期/被压缩的滚动素材。

**本模块已接入生产运行时**: ``build_autonomous_runtime_from_file`` 装配本 runner 与领取循环,
生产 ``create_app`` 挂载专用签名端点。终局失败 (过期/素材缺失/切片错误) 由 runner 回报中心结案;
中心暂时不可达保留本机待办等待恢复, 不静默丢弃。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from time import monotonic, time
from typing import Protocol, cast

from nvsop_contracts import ReclipEvidenceClaim, ReclipEvidenceCommand, ReclipEvidenceResult

from edge_runtime.center_client import CenterClient, CenterUnreachableError
from edge_runtime.evidence_media import (
    EvidenceClipSet,
    EvidenceMediaConfiguration,
    EvidenceMediaError,
    evidence_registration_payloads,
    reclip_evidence_identity,
    recover_finalized_clip_set,
    slice_evidence,
)
from edge_runtime.evidence_reporting import register_evidence_payloads
from edge_runtime.judgment.model import HostInstant
from edge_runtime.local_state import LocalState, PendingEvidence, PendingReclip, StationStore


class ReclipEvidenceError(RuntimeError):
    """一次再切片本机执行失败的具体原因; 记录到本机待办, 不冒充成功也不静默丢弃。

    ``terminal`` 区分终局失败与可恢复故障: 过期/素材缺失/切片错误/已定稿媒体损坏都是终局失败,
    由 runner 回报中心结案; 中心暂时不可达或拒绝登记属于恢复等待, 保留待办下一轮重试 (S039/AC1)。
    """

    def __init__(self, reason: str, detail: str, *, terminal: bool = True) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail
        self.terminal = terminal


class ReclipCommandTransport(Protocol):
    """再切片命令的中心传输接缝, 只承载领取信封和成功回报。"""

    def claim_next(self) -> ReclipEvidenceClaim | None: ...

    def report(self, claim: ReclipEvidenceClaim, result: ReclipEvidenceResult) -> None: ...


def _covers_frozen_span(record: PendingReclip) -> bool:
    """命令窗口必须仍覆盖本机冻结的核心必需跨度与锚点, 不信任中心单方面裁剪。"""
    required_from = record.required_from if record.required_from is not None else record.anchor
    required_to = record.required_to if record.required_to is not None else record.anchor
    return record.window_from <= min(required_from, record.anchor) and record.window_to >= max(
        required_to, record.anchor
    )


def _coverage_number(value: object, name: str) -> float:
    """读取再切片产物的覆盖边界; 非数字即视为不可信的部分产物。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EvidenceMediaError(
            "reclip_window_incomplete", f"reclip result has a non-numeric {name}"
        )
    return float(value)


def _require_full_reclip_coverage(record: PendingReclip, clip_set: EvidenceClipSet) -> None:
    """再切片产物必须覆盖全部冻结来源与请求窗口; 部分覆盖是终局失败, 不冒充成功 (AC3)。

    首次切片允许部分覆盖留给后续登记对账; 再切片在登记前显式拒绝, 不改动首次切片语义。
    """
    media = cast("list[dict[str, object]]", json.loads(clip_set.media_json))
    present = {cast(str, item["media_path"]) for item in media}
    missing = [source for source in record.sources if source not in present]
    if missing:
        raise EvidenceMediaError(
            "reclip_source_missing",
            f"reclip result is missing frozen sources: {', '.join(missing)}",
        )
    for item in media:
        actual_from = _coverage_number(item["actual_from"], "actual_from")
        actual_to = _coverage_number(item["actual_to"], "actual_to")
        if actual_from > record.window_from or actual_to < record.window_to:
            raise EvidenceMediaError(
                "reclip_window_incomplete",
                f"source {item['media_path']} does not cover the requested reclip window",
            )


def _pending_evidence(record: PendingReclip) -> PendingEvidence:
    """把本机再切片记录还原为首次切片接缝的输入; identity 已固定, 不再从队列主键派生。"""
    return PendingEvidence(
        queue_id=0,
        instance_id=record.instance_id,
        anchor=HostInstant(record.anchor),
        start=HostInstant(record.window_from),
        end=HostInstant(record.window_to),
        attempts=record.attempts,
        last_error=record.last_error,
        wall_offset=record.wall_offset,
        sources=record.sources,
        media_results=record.media_results,
        covered_from=record.covered_from,
        covered_to=record.covered_to,
        required_from=record.required_from,
        required_to=record.required_to,
    )


class ReclipEvidenceExecutor:
    """在目标推理机上复用首次切片能力, 按原锚点与并集窗口追加新媒体代次。"""

    def __init__(
        self,
        *,
        state: LocalState,
        host_id: str,
        recording_directory: Path,
        ffmpeg_binary: Path,
        configuration: EvidenceMediaConfiguration,
        client: CenterClient,
        clock: Callable[[], float] = monotonic,
        wall_clock: Callable[[], float] = time,
    ) -> None:
        self._state = state
        self._host_id = host_id
        self._recording_directory = recording_directory
        self._ffmpeg_binary = ffmpeg_binary
        self._configuration = configuration
        self._client = client
        self._clock = clock
        self._wall_clock = wall_clock

    def execute(self, command: ReclipEvidenceCommand) -> ReclipEvidenceResult:
        """只从本机入队冻结的原始素材映射切片, 不改锚点, 不覆盖旧片段。"""
        station = self._state.station(command.station_id)
        record = station.reclip_request(command.request_id) or self._begin(station, command)
        now = HostInstant(self._clock())
        if record.terminal_error_code is not None:
            # 终局失败已冻结: 重启/重复领取复用同一原因, 素材后来恢复或不同异常都不重切 (AC1)。
            raise ReclipEvidenceError(
                record.terminal_error_code, record.terminal_error_detail or ""
            )
        if record.wall_offset is None or not record.sources or record.identity is None:
            # 来源事实缺失已在 _begin 持久化; 重复领取保持同一可观察失败, 不重切也不登记。
            raise self._terminal(
                station,
                command.request_id,
                code="reclip_source_missing",
                detail=record.last_error or "no frozen local material mapping",
                now=now,
            )
        if not _covers_frozen_span(record):
            raise self._terminal(
                station,
                command.request_id,
                code="reclip_window_incomplete",
                detail="requested window does not cover the frozen core span and anchor",
                now=now,
            )
        # 已定稿产物是权威: 直接复用, 不再查询或重切滚动素材。
        if record.evidence_id is not None:
            return ReclipEvidenceResult(evidence_id=record.evidence_id)
        if record.media_results is None:
            record = self._slice(station, record, command, now)
        try:
            payloads = self._payloads(record)
        except (ValueError, OSError) as error:
            # 本机已定稿媒体被篡改/删除: 重试无法恢复, 终局失败并回报具体原因。
            raise self._terminal(
                station,
                command.request_id,
                code="reclip_registration_failed",
                detail=f"{type(error).__name__}: {error}",
                now=now,
            ) from error
        try:
            register_evidence_payloads(self._client, payloads)
        except (CenterUnreachableError, ValueError) as error:
            # 中心未确认 (暂时不可达或 HTTP 拒绝): 保留待办等待中心恢复, 不结案成终局失败 (AC1)。
            station.record_reclip_failure(
                command.request_id, at=now, error=f"{type(error).__name__}: {error}"[:255]
            )
            raise ReclipEvidenceError(
                "reclip_registration_failed", str(error), terminal=False
            ) from error
        evidence_id = cast(str, payloads[0]["evidence_id"])
        station.mark_reclip_registered(command.request_id, at=now, evidence_id=evidence_id)
        return ReclipEvidenceResult(evidence_id=evidence_id)

    @staticmethod
    def _terminal(
        station: StationStore,
        request_id: str,
        *,
        code: str,
        detail: str,
        now: HostInstant,
    ) -> ReclipEvidenceError:
        """冻结首次终局失败原因并返回对应错误, 使重试复用同一结案 (S039/AC1)。"""
        station.record_reclip_terminal_failure(request_id, at=now, code=code, detail=detail)
        return ReclipEvidenceError(code, detail)

    def _begin(self, station: StationStore, command: ReclipEvidenceCommand) -> PendingReclip:
        row = station.evidence_at(instance_id=command.instance_id, anchor=command.anchor)
        if row is None:
            return self._source_missing(
                station, command, "no frozen evidence row for the requested anchor"
            )
        if row.wall_offset is None:
            return self._source_missing(
                station, command, "evidence row has no frozen wall clock mapping"
            )
        if not row.sources:
            return self._source_missing(
                station, command, "evidence row has no frozen recording sources"
            )
        identity = reclip_evidence_identity(self._host_id, row.queue_id, command.request_id)
        return station.record_reclip_intent(
            request_id=command.request_id,
            instance_id=command.instance_id,
            anchor=command.anchor,
            window_from=command.window_start,
            window_to=command.window_end,
            wall_offset=row.wall_offset,
            sources=row.sources,
            required_from=row.required_from,
            required_to=row.required_to,
            identity=identity,
        )

    def _source_missing(
        self, station: StationStore, command: ReclipEvidenceCommand, detail: str
    ) -> PendingReclip:
        """来源事实缺失时持久化可查询原因, 不伪造墙钟/相机映射 (S038 AC3)。"""
        return station.record_reclip_intent_failure(
            request_id=command.request_id,
            instance_id=command.instance_id,
            anchor=command.anchor,
            window_from=command.window_start,
            window_to=command.window_end,
            error=f"reclip_source_missing: {detail}",
        )

    def _slice(
        self,
        station: StationStore,
        record: PendingReclip,
        command: ReclipEvidenceCommand,
        now: HostInstant,
    ) -> PendingReclip:
        assert record.identity is not None
        try:
            # SQL 索引丢失时先按稳定身份复用已定稿产物, 不重新查询可能已消失/被压缩的素材。
            recovered = recover_finalized_clip_set(
                _pending_evidence(record),
                host_id=self._host_id,
                directory=self._configuration.evidence_directory,
                identity=record.identity,
            )
            if recovered is not None:
                # 已定稿产物优先于过期判定: 租约/重启后不得因中心截止而丢掉已拥有的产物。
                clip_set = recovered
            else:
                self._reject_if_expired(station, record, command, now)
                clip_set = slice_evidence(
                    _pending_evidence(record),
                    host_id=self._host_id,
                    recording_directory=self._recording_directory,
                    ffmpeg_binary=self._ffmpeg_binary,
                    configuration=self._configuration,
                    identity=record.identity,
                )
            _require_full_reclip_coverage(record, clip_set)
        except ReclipEvidenceError:
            raise
        except EvidenceMediaError as error:
            raise self._terminal(
                station,
                record.request_id,
                code=error.reason,
                detail=error.detail,
                now=now,
            ) from error
        station.record_reclip_slice(
            record.request_id,
            at=now,
            media_results=clip_set.media_json,
            covered_from=clip_set.covered_from,
            covered_to=clip_set.covered_to,
        )
        updated = station.reclip_request(record.request_id)
        if updated is None:
            raise ReclipEvidenceError("reclip_record_missing", "reclip record disappeared")
        return updated

    def _reject_if_expired(
        self,
        station: StationStore,
        record: PendingReclip,
        command: ReclipEvidenceCommand,
        now: HostInstant,
    ) -> None:
        """仅当尚未产出任何产物时才按中心截止报终局归档过期; 已定稿产物已在前面复用。"""
        if self._wall_clock() < command.available_until:
            return
        raise self._terminal(
            station,
            record.request_id,
            code="archive_expired",
            detail="center reclip deadline passed before any artifact was produced",
            now=now,
        )

    def _payloads(self, record: PendingReclip) -> tuple[dict[str, object], ...]:
        return evidence_registration_payloads(
            _pending_evidence(record),
            host_id=self._host_id,
            station_id=record.station_id,
            directory=self._configuration.evidence_directory,
            identity=record.identity,
            origin="reclip",
        )


class ReclipCommandRunner:
    """驱动一次再切片命令的领取、本机真实执行和成功回报。"""

    def __init__(
        self, *, transport: ReclipCommandTransport, executor: ReclipEvidenceExecutor
    ) -> None:
        self._transport = transport
        self._executor = executor

    def run_once(self) -> bool:
        """处理一条命令; 无命令返回 False。终局失败回报具体原因, 中心不可达保留待办 (S039)。"""
        claim = self._transport.claim_next()
        if claim is None:
            return False
        try:
            result = self._executor.execute(claim.command)
        except ReclipEvidenceError as error:
            if not error.terminal:
                return True
            self._transport.report(
                claim,
                ReclipEvidenceResult(
                    failure_code=error.reason,
                    detail=error.detail[:255] if error.detail else None,
                ),
            )
            return True
        self._transport.report(claim, result)
        return True


__all__ = [
    "ReclipCommandRunner",
    "ReclipCommandTransport",
    "ReclipEvidenceError",
    "ReclipEvidenceExecutor",
]
