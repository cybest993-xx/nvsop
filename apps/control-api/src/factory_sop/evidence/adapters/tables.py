"""evidence 中心引用登记表的 SQLAlchemy 行模型。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from factory_sop.evidence.model import (
    EvidenceKind,
    EvidenceOrigin,
    EvidenceReference,
    EvidenceRegistration,
    EvidenceStatus,
)
from factory_sop.persistence import Table


class EvidenceReferenceRow(Table):
    """一条中心证据引用：只存身份/元数据，不存媒体字节或对象存储键。"""

    __tablename__ = "evidence_evidence"

    evidence_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    host_id: Mapped[str] = mapped_column(String(128), index=True)
    station_id: Mapped[str] = mapped_column(String(128), index=True)
    instance_id: Mapped[int] = mapped_column(BigInteger(), index=True)
    violation_id: Mapped[str | None] = mapped_column(Text(), nullable=True)
    kind: Mapped[str] = mapped_column(String(32))
    origin: Mapped[str] = mapped_column(String(32))
    anchor: Mapped[float]
    window_start: Mapped[float]
    window_end: Mapped[float]
    generation: Mapped[str] = mapped_column(String(128))
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    size: Mapped[int | None] = mapped_column(BigInteger(), nullable=True)
    reference: Mapped[str | None] = mapped_column(Text(), nullable=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    failure_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

    def to_domain(self) -> EvidenceReference:
        return EvidenceReference(
            registration=EvidenceRegistration(
                evidence_id=self.evidence_id,
                host_id=self.host_id,
                station_id=self.station_id,
                instance_id=self.instance_id,
                violation_id=self.violation_id,
                kind=EvidenceKind(self.kind),
                origin=EvidenceOrigin(self.origin),
                anchor=self.anchor,
                window_start=self.window_start,
                window_end=self.window_end,
                generation=self.generation,
                sha256=self.sha256,
                size=self.size,
                reference=self.reference,
            ),
            status=EvidenceStatus(self.status),
            failure_reason=self.failure_reason,
            received_at=self.received_at,
        )

    @classmethod
    def from_domain(cls, value: EvidenceReference) -> EvidenceReferenceRow:
        registration = value.registration
        return cls(
            evidence_id=registration.evidence_id,
            host_id=registration.host_id,
            station_id=registration.station_id,
            instance_id=registration.instance_id,
            violation_id=registration.violation_id,
            kind=registration.kind.value,
            origin=registration.origin.value,
            anchor=registration.anchor,
            window_start=registration.window_start,
            window_end=registration.window_end,
            generation=registration.generation,
            sha256=registration.sha256,
            size=registration.size,
            reference=registration.reference,
            status=value.status.value,
            failure_reason=value.failure_reason,
            received_at=value.received_at,
        )


__all__ = ["EvidenceReferenceRow"]
