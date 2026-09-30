"""evidence 登记与查询：登记是对“来源推理机本机持有该证据”的对账，不读写媒体字节（ADR-0012）。"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from factory_sop.evidence.api import HostOwnershipGateway
from factory_sop.evidence.errors import EvidenceRefusal, EvidenceRefusedError
from factory_sop.evidence.model import (
    EvidenceReference,
    EvidenceRegistration,
    EvidenceStatus,
)
from factory_sop.evidence.repository import EvidenceRepository


def register_evidence(
    registration: EvidenceRegistration,
    *,
    received_at: datetime,
    evidence: EvidenceRepository,
    host_gateway: HostOwnershipGateway,
) -> EvidenceReference:
    """登记一条本机证据引用；按稳定证据 ID 幂等，拒绝归属、身份或摘要冲突。

    主机不属于该工位时拒绝；受控引用非法时登记 ``FAILED`` 且不存非法串；冲突不覆盖原引用。
    """
    host_id = _uuid(registration.host_id, "evidence host_id")
    station_id = _uuid(registration.station_id, "evidence station_id")
    if not host_gateway.owns_station(host_id=host_id, station_id=station_id):
        raise EvidenceRefusedError(
            EvidenceRefusal.HOST_NOT_OWNER,
            "evidence reference is outside the authenticated host topology",
        )
    incoming = _classify(registration, received_at=received_at)
    existing = evidence.find(registration.evidence_id)
    if existing is None and not evidence.insert(incoming):
        existing = evidence.find(registration.evidence_id)
        if existing is None:
            raise RuntimeError("evidence insert conflicted without a visible row")
    return incoming if existing is None else _reconcile(existing, incoming, evidence=evidence)


def find_evidence(evidence: EvidenceRepository, evidence_id: str) -> EvidenceReference | None:
    """按稳定证据 ID 返回登记事实，未登记时为 ``None``。"""
    return evidence.find(evidence_id)


def list_evidence(
    evidence: EvidenceRepository,
    *,
    page: int,
    page_size: int,
    status: EvidenceStatus | None = None,
    station_id: str | None = None,
    instance_id: int | None = None,
) -> tuple[tuple[EvidenceReference, ...], int]:
    """返回按登记状态/工位/实例定位的一页引用及总数，供授权查询 seam 复用。"""
    return evidence.page(
        page=page,
        page_size=page_size,
        status=status,
        station_id=station_id,
        instance_id=instance_id,
    )


def is_controlled_reference(reference: str) -> bool:
    """受控本机引用：非空、相对、无上跳、无 scheme、无控制字符；中心只存形态，读取由来源机做。"""
    if not reference or reference != reference.strip():
        return False
    if reference.startswith("/") or "\\" in reference or "://" in reference:
        return False
    if any(ord(character) < 0x20 for character in reference):
        return False
    return all(part not in {"", ".", ".."} for part in reference.split("/"))


def _classify(registration: EvidenceRegistration, *, received_at: datetime) -> EvidenceReference:
    """把登记输入归约为真实状态；非法引用不落入持久化。"""
    if registration.sha256 is None:
        return EvidenceReference(registration, EvidenceStatus.PENDING, None, received_at)
    if not is_controlled_reference(registration.reference or ""):
        return EvidenceReference(
            registration.without_material(),
            EvidenceStatus.FAILED,
            EvidenceRefusal.ILLEGAL_REFERENCE.value,
            received_at,
        )
    return EvidenceReference(registration, EvidenceStatus.AVAILABLE, None, received_at)


def _reconcile(
    existing: EvidenceReference, incoming: EvidenceReference, *, evidence: EvidenceRepository
) -> EvidenceReference:
    """合并重复登记：身份一致时补齐/提升，身份或摘要冲突时拒绝。"""
    if existing.registration.identity() != incoming.registration.identity():
        raise EvidenceRefusedError(
            EvidenceRefusal.IDENTITY_CONFLICT,
            "evidence identity conflicts with the registered reference",
        )
    if existing.status is EvidenceStatus.AVAILABLE:
        if (
            incoming.status is EvidenceStatus.AVAILABLE
            and existing.registration.material() != incoming.registration.material()
        ):
            raise EvidenceRefusedError(
                EvidenceRefusal.IDENTITY_CONFLICT,
                "evidence digest or material conflicts with the registered reference",
            )
        return existing
    # 已失败不等于未登记：身份重报不得把失败记录抹回待登记，只有可用才可超越它。
    if existing.status is EvidenceStatus.FAILED and incoming.status is EvidenceStatus.PENDING:
        return existing
    unchanged = (
        existing.status is incoming.status
        and existing.registration == incoming.registration
        and existing.failure_reason == incoming.failure_reason
    )
    if incoming.status is EvidenceStatus.AVAILABLE or not unchanged:
        if evidence.replace_if_current(expected=existing, value=incoming):
            return incoming
        current = evidence.find(existing.evidence_id)
        if current is None:
            raise RuntimeError("evidence transition conflicted without a visible row")
        if current.registration.identity() != incoming.registration.identity():
            raise EvidenceRefusedError(
                EvidenceRefusal.IDENTITY_CONFLICT,
                "evidence identity conflicts with the registered reference",
            )
        if current.status is EvidenceStatus.AVAILABLE:
            if (
                incoming.status is EvidenceStatus.AVAILABLE
                and current.registration.material() == incoming.registration.material()
            ):
                return current
            raise EvidenceRefusedError(
                EvidenceRefusal.IDENTITY_CONFLICT,
                "evidence digest or material lost a concurrent transition",
            )
        if (
            current.status is incoming.status
            and current.registration == incoming.registration
            and current.failure_reason == incoming.failure_reason
        ):
            return current
        raise EvidenceRefusedError(
            EvidenceRefusal.IDENTITY_CONFLICT,
            "evidence state changed concurrently",
        )
    return existing


def _uuid(value: str, label: str) -> UUID:
    try:
        return UUID(value)
    except ValueError as error:
        raise ValueError(f"{label} is not a valid identity") from error


__all__ = [
    "find_evidence",
    "is_controlled_reference",
    "list_evidence",
    "register_evidence",
]
