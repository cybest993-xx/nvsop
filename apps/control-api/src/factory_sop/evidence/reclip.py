"""再切片请求：锚点与来源主机冻结，命令交付走 device 队列，本模块只拥有请求与状态（§5.20）。

本票只登记请求、计算可再切片截止时刻并做权限/窗口校验；真正的 Edge 切片执行、失败结案与
新证据追加属后续 S038/S039，命令因此不可领取，绝不下发给旧严格命令消费者。
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from factory_sop.auth.api import Caller, Permission, authorize
from factory_sop.evidence.model import (
    EvidenceOrigin,
    EvidenceReclipRequest,
    EvidenceRegistration,
    EvidenceStatus,
    ReclipRequestStatus,
)
from factory_sop.evidence.repository import EvidenceReclipRequestRepository, EvidenceRepository

if TYPE_CHECKING:
    from factory_sop.device.api import DeviceEvidenceGateway


class EvidenceReclipRefusedError(ValueError):
    """再切片请求被拒绝；被拒绝的请求不产生任何持久化事实。"""

    def __init__(self, status: int, code: str) -> None:
        super().__init__(code)
        self.status = status
        self.code = code


def reclip_availability(
    registration: EvidenceRegistration,
    *,
    status: EvidenceStatus,
    recording_window_seconds: int | None,
    now: float,
) -> tuple[float | None, bool]:
    """按来源机冻结的墙钟映射与中心保存的滚动窗口给出理论截止时刻及是否可再切片。

    截止取核心必需跨度的最早时刻老化出滚动录像的时刻；旧登记缺冻结元数据、证据尚未对账
    或来源机窗口未知时返回不可再切片。窗口值是中心保存的配置，不代表来源机已实际应用。
    """
    if (
        status is not EvidenceStatus.AVAILABLE
        or recording_window_seconds is None
        or registration.wall_offset is None
        or registration.required_from is None
    ):
        return None, False
    deadline = registration.required_from + registration.wall_offset + recording_window_seconds
    return deadline, now < deadline


def create_reclip_request(
    caller: Caller,
    *,
    request_id: UUID,
    evidence_id: str,
    generation: str,
    before_seconds: float,
    after_seconds: float,
    now: datetime,
    evidence: EvidenceRepository,
    requests: EvidenceReclipRequestRepository,
    gateway: DeviceEvidenceGateway,
) -> EvidenceReclipRequest:
    """登记一次再切片请求；按 ``request_id`` 幂等，锚点/来源主机由证据事实派生。

    客户端只给证据身份、代次、新余量与稳定请求 ID；锚点、来源主机与命令目标不接受覆写。
    新窗口是核心必需跨度与请求余量的并集，核心跨度不可被余量削掉（§5.20）。
    """
    authorize(caller, Permission.EVIDENCE_VIEW)
    authorize(caller, Permission.EVIDENCE_REVIEW_EDIT)
    if (
        not math.isfinite(before_seconds)
        or not math.isfinite(after_seconds)
        or before_seconds < 0
        or after_seconds < 0
    ):
        raise EvidenceReclipRefusedError(422, "reclip_margins_invalid")
    existing = requests.find(request_id)
    if existing is not None:
        if existing.matches(
            evidence_id=evidence_id,
            generation=generation,
            before_seconds=before_seconds,
            after_seconds=after_seconds,
            requested_by=caller.user.id,
        ):
            return existing
        raise EvidenceReclipRefusedError(409, "reclip_request_conflict")
    reference = evidence.find(evidence_id)
    if reference is None:
        raise EvidenceReclipRefusedError(404, "evidence_unregistered")
    registration = reference.registration
    if registration.generation != generation:
        raise EvidenceReclipRefusedError(409, "evidence_generation_mismatch")
    if reference.status is not EvidenceStatus.AVAILABLE:
        raise EvidenceReclipRefusedError(410, "evidence_unavailable")
    if (
        registration.wall_offset is None
        or registration.required_from is None
        or registration.required_to is None
    ):
        raise EvidenceReclipRefusedError(409, "reclip_metadata_unavailable")
    host_id = UUID(registration.host_id)
    recording_window = gateway.recording_window_seconds(host_id=host_id)
    if recording_window is None:
        raise EvidenceReclipRefusedError(409, "reclip_source_host_unavailable")
    window_start = min(registration.required_from, registration.anchor - before_seconds)
    window_end = max(registration.required_to, registration.anchor + after_seconds)
    if window_end - window_start > recording_window:
        raise EvidenceReclipRefusedError(422, "reclip_window_too_long")
    if now.timestamp() >= window_start + registration.wall_offset + recording_window:
        raise EvidenceReclipRefusedError(410, "reclip_window_expired")
    command_id = gateway.enqueue_reclip_command(
        host_id=host_id,
        request_id=request_id,
        idempotency_key=f"evidence-reclip:{request_id}",
        actor_id=caller.user.id,
        now=now,
    )
    request = EvidenceReclipRequest(
        request_id=request_id,
        evidence_id=evidence_id,
        generation=generation,
        host_id=host_id,
        anchor=registration.anchor,
        before_seconds=before_seconds,
        after_seconds=after_seconds,
        window_start=window_start,
        window_end=window_end,
        requested_by=caller.user.id,
        requested_at=now,
        command_id=command_id,
        status=ReclipRequestStatus.PENDING,
    )
    if requests.insert(request):
        return request
    concurrent = requests.find(request_id)
    if concurrent is None:
        raise RuntimeError("reclip request insert conflicted without a visible row")
    if concurrent.matches(
        evidence_id=evidence_id,
        generation=generation,
        before_seconds=before_seconds,
        after_seconds=after_seconds,
        requested_by=caller.user.id,
    ):
        return concurrent
    raise EvidenceReclipRefusedError(409, "reclip_request_conflict")


def list_reclip_requests(
    caller: Caller, *, evidence_id: str, requests: EvidenceReclipRequestRepository
) -> tuple[EvidenceReclipRequest, ...]:
    """返回该证据的全部再切片请求；查询复用查看权限，不隐含编辑。"""
    authorize(caller, Permission.EVIDENCE_VIEW)
    return requests.list_for_evidence(evidence_id)


def settle_reclip_success(
    *,
    command_id: UUID,
    evidence_id: str,
    host_id: UUID,
    evidence: EvidenceRepository,
    requests: EvidenceReclipRequestRepository,
) -> EvidenceReclipRequest:
    """把一条已领取再切片命令的成功结果结案为请求 ``SUCCEEDED`` 并记录新引用。

    新引用必须已登记、来源为 reclip、且属于同一主机与锚点，否则拒绝，不追加伪造代次。
    """
    request = requests.find_by_command_id(command_id)
    if request is None:
        raise EvidenceReclipRefusedError(404, "reclip_request_unregistered")
    if request.host_id != host_id:
        raise EvidenceReclipRefusedError(409, "reclip_request_host_mismatch")
    reference = evidence.find(evidence_id)
    if reference is None or reference.registration.origin is not EvidenceOrigin.RECLIP:
        raise EvidenceReclipRefusedError(409, "reclip_result_unregistered")
    if UUID(reference.registration.host_id) != host_id:
        raise EvidenceReclipRefusedError(409, "reclip_result_host_mismatch")
    if reference.registration.anchor != request.anchor:
        raise EvidenceReclipRefusedError(409, "reclip_result_anchor_mismatch")
    if request.status is ReclipRequestStatus.SUCCEEDED:
        if request.result_evidence_id == evidence_id:
            return request
        raise EvidenceReclipRefusedError(409, "reclip_request_conflict")
    if request.status is ReclipRequestStatus.FAILED:
        raise EvidenceReclipRefusedError(409, "reclip_request_conflict")
    if not requests.mark_succeeded(request.request_id, result_evidence_id=evidence_id):
        current = requests.find(request.request_id)
        if (
            current is not None
            and current.status is ReclipRequestStatus.SUCCEEDED
            and current.result_evidence_id == evidence_id
        ):
            return current
        raise EvidenceReclipRefusedError(409, "reclip_request_conflict")
    updated = requests.find(request.request_id)
    if updated is None:
        raise RuntimeError("reclip request disappeared after success settlement")
    return updated


__all__ = [
    "EvidenceReclipRefusedError",
    "create_reclip_request",
    "list_reclip_requests",
    "reclip_availability",
    "settle_reclip_success",
]
