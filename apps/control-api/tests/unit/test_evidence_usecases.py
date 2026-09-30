"""evidence 登记用例：幂等、归属校验、受控引用与状态查询。"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest

from factory_sop.evidence.errors import EvidenceRefusal, EvidenceRefusedError
from factory_sop.evidence.model import (
    EvidenceKind,
    EvidenceOrigin,
    EvidenceReference,
    EvidenceRegistration,
    EvidenceStatus,
)
from factory_sop.evidence.usecases import find_evidence, list_evidence, register_evidence

HOST_ID = UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f301")
STATION_ID = UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f302")
OTHER_HOST_ID = UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f303")
RECEIVED_AT = datetime(2026, 10, 1, tzinfo=UTC)
SHA256 = "a" * 64


class FakeHostGateway:
    def __init__(self, owner: UUID) -> None:
        self._owner = owner

    def owns_station(self, *, host_id: UUID, station_id: UUID) -> bool:
        return host_id == self._owner and station_id == STATION_ID


class MemoryEvidence:
    def __init__(self) -> None:
        self.rows: dict[str, EvidenceReference] = {}

    def find(self, evidence_id: str) -> EvidenceReference | None:
        return self.rows.get(evidence_id)

    def insert(self, value: EvidenceReference) -> bool:
        if value.evidence_id in self.rows:
            return False
        self.rows[value.evidence_id] = value
        return True

    def replace(self, value: EvidenceReference) -> None:
        self.rows[value.evidence_id] = value

    def page(
        self,
        *,
        page: int,
        page_size: int,
        status: EvidenceStatus | None = None,
        station_id: str | None = None,
        instance_id: int | None = None,
    ) -> tuple[tuple[EvidenceReference, ...], int]:
        values = [
            value
            for value in self.rows.values()
            if (status is None or value.status is status)
            and (station_id is None or value.registration.station_id == station_id)
            and (instance_id is None or value.registration.instance_id == instance_id)
        ]
        start = (page - 1) * page_size
        return tuple(values[start : start + page_size]), len(values)


def _registration(
    *,
    evidence_id: str = "ev-1",
    instance_id: int = 1,
    anchor: float = 100.0,
    sha256: str | None = SHA256,
    size: int | None = 1024,
    reference: str | None = "evidence/ev-1.mp4",
) -> EvidenceRegistration:
    return EvidenceRegistration(
        evidence_id=evidence_id,
        host_id=str(HOST_ID),
        station_id=str(STATION_ID),
        instance_id=instance_id,
        violation_id="decision-1#0",
        kind=EvidenceKind.CLIP,
        origin=EvidenceOrigin.AUTOMATIC,
        anchor=anchor,
        window_start=95.0,
        window_end=105.0,
        generation="original",
        sha256=sha256,
        size=size,
        reference=reference,
    )


def _register(
    evidence: MemoryEvidence, registration: EvidenceRegistration, *, host: UUID = HOST_ID
) -> EvidenceReference:
    return register_evidence(
        registration,
        received_at=RECEIVED_AT,
        evidence=evidence,
        host_gateway=FakeHostGateway(host),
    )


def test_register_complete_reference_is_available_and_idempotent() -> None:
    evidence = MemoryEvidence()
    first = _register(evidence, _registration())
    second = _register(evidence, _registration())
    assert first.status is EvidenceStatus.AVAILABLE
    assert second == first
    assert list(evidence.rows) == ["ev-1"]


def test_register_without_material_is_pending_then_promoted() -> None:
    evidence = MemoryEvidence()
    pending = _register(evidence, _registration(sha256=None, size=None, reference=None))
    assert pending.status is EvidenceStatus.PENDING
    promoted = _register(evidence, _registration())
    assert promoted.status is EvidenceStatus.AVAILABLE
    stored = find_evidence(evidence, "ev-1")
    assert stored is not None
    assert stored.status is EvidenceStatus.AVAILABLE


def test_unknown_host_is_refused_and_nothing_persisted() -> None:
    evidence = MemoryEvidence()
    with pytest.raises(EvidenceRefusedError) as refused:
        _register(evidence, _registration(), host=OTHER_HOST_ID)
    assert refused.value.code is EvidenceRefusal.HOST_NOT_OWNER
    assert evidence.rows == {}


def test_illegal_reference_is_failed_without_storing_the_reference() -> None:
    evidence = MemoryEvidence()
    failed = _register(evidence, _registration(reference="../escape.mp4"))
    assert failed.status is EvidenceStatus.FAILED
    assert failed.failure_reason == EvidenceRefusal.ILLEGAL_REFERENCE.value
    assert failed.registration.sha256 is None
    assert failed.registration.reference is None
    # 已失败身份重报身份元数据时，失败记录不被抹回待登记。
    stored = _register(evidence, _registration(sha256=None, size=None, reference=None))
    assert stored.status is EvidenceStatus.FAILED


def test_conflicting_digest_or_identity_is_refused_and_original_kept() -> None:
    evidence = MemoryEvidence()
    original = _register(evidence, _registration())
    for conflicting in (
        _registration(anchor=200.0),
        _registration(sha256="b" * 64, reference="evidence/other.mp4"),
    ):
        with pytest.raises(EvidenceRefusedError) as refused:
            _register(evidence, conflicting)
        assert refused.value.code is EvidenceRefusal.IDENTITY_CONFLICT
        assert find_evidence(evidence, "ev-1") == original


def test_list_filters_by_status_and_scope() -> None:
    evidence = MemoryEvidence()
    _register(evidence, _registration(evidence_id="ev-1"))
    _register(evidence, _registration(evidence_id="ev-2", sha256=None, size=None, reference=None))
    _register(evidence, _registration(evidence_id="ev-3", instance_id=2))
    available, available_total = list_evidence(
        evidence, page=1, page_size=10, status=EvidenceStatus.AVAILABLE
    )
    assert available_total == 2
    assert {value.evidence_id for value in available} == {"ev-1", "ev-3"}
    scoped, scoped_total = list_evidence(
        evidence, page=1, page_size=10, station_id=str(STATION_ID), instance_id=2
    )
    assert scoped_total == 1
    assert [value.evidence_id for value in scoped] == ["ev-3"]


def test_partial_material_is_rejected_at_construction() -> None:
    with pytest.raises(ValueError, match="together"):
        _registration(sha256=SHA256, size=None, reference=None)
