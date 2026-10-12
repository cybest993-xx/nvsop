"""evidence 中心引用登记的 PostgreSQL 适配器。"""

from __future__ import annotations

from typing import Any, cast
from uuid import UUID

from sqlalchemy import CursorResult, Table, func, select, update
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.orm import Session

from factory_sop.evidence.adapters.tables import (
    EvidenceReclipRequestRow,
    EvidenceReferenceRow,
    EvidenceReviewRow,
)
from factory_sop.evidence.model import (
    EvidenceReclipRequest,
    EvidenceReference,
    EvidenceReview,
    EvidenceStatus,
    ReclipRequestStatus,
)
from factory_sop.evidence.repository import (
    EvidenceReclipRequestRepository,
    EvidenceRepository,
    EvidenceReviewRepository,
)


class PostgresEvidenceRepository(EvidenceRepository):
    def __init__(self, session: Session) -> None:
        self._session = session

    def find(self, evidence_id: str) -> EvidenceReference | None:
        row = self._session.scalar(
            select(EvidenceReferenceRow)
            .where(EvidenceReferenceRow.evidence_id == evidence_id)
            .execution_options(populate_existing=True)
        )
        return None if row is None else row.to_domain()

    def insert(self, value: EvidenceReference) -> bool:
        row = EvidenceReferenceRow.from_domain(value)
        table = cast(Table, EvidenceReferenceRow.__table__)
        statement = (
            postgres_insert(table)
            .values(**_columns(row))
            .on_conflict_do_nothing(index_elements=[table.c.evidence_id])
            .returning(table.c.evidence_id)
        )
        return self._session.execute(statement).scalar_one_or_none() is not None

    def replace_if_current(self, *, expected: EvidenceReference, value: EvidenceReference) -> bool:
        """只更新仍属于调用方读取 generation 的行；证据身份列从不参与覆盖。"""
        table = cast(Table, EvidenceReferenceRow.__table__)
        expected_registration = expected.registration
        registration = value.registration
        statement = (
            update(table)
            .where(
                table.c.evidence_id == expected.evidence_id,
                table.c.status == expected.status.value,
                (
                    table.c.sha256.is_(None)
                    if expected_registration.sha256 is None
                    else table.c.sha256 == expected_registration.sha256
                ),
                (
                    table.c.size.is_(None)
                    if expected_registration.size is None
                    else table.c.size == expected_registration.size
                ),
                (
                    table.c.reference.is_(None)
                    if expected_registration.reference is None
                    else table.c.reference == expected_registration.reference
                ),
                (
                    table.c.wall_offset.is_(None)
                    if expected_registration.wall_offset is None
                    else table.c.wall_offset == expected_registration.wall_offset
                ),
                (
                    table.c.required_from.is_(None)
                    if expected_registration.required_from is None
                    else table.c.required_from == expected_registration.required_from
                ),
                (
                    table.c.required_to.is_(None)
                    if expected_registration.required_to is None
                    else table.c.required_to == expected_registration.required_to
                ),
                (
                    table.c.failure_reason.is_(None)
                    if expected.failure_reason is None
                    else table.c.failure_reason == expected.failure_reason
                ),
                table.c.received_at == expected.received_at,
            )
            .values(
                sha256=registration.sha256,
                size=registration.size,
                reference=registration.reference,
                # 冻结三元数据不是身份，只随登记状态提升（PENDING/FAILED→AVAILABLE）落库。
                wall_offset=registration.wall_offset,
                required_from=registration.required_from,
                required_to=registration.required_to,
                status=value.status.value,
                failure_reason=value.failure_reason,
                received_at=value.received_at,
            )
        )
        result = cast("CursorResult[Any]", self._session.execute(statement))
        return result.rowcount == 1

    def page(
        self,
        *,
        page: int,
        page_size: int,
        status: EvidenceStatus | None = None,
        station_id: str | None = None,
        instance_id: int | None = None,
    ) -> tuple[tuple[EvidenceReference, ...], int]:
        statement = select(EvidenceReferenceRow)
        if status is not None:
            statement = statement.where(EvidenceReferenceRow.status == status.value)
        if station_id is not None:
            statement = statement.where(EvidenceReferenceRow.station_id == station_id)
        if instance_id is not None:
            statement = statement.where(EvidenceReferenceRow.instance_id == instance_id)
        rows = self._session.scalars(
            statement.order_by(
                EvidenceReferenceRow.received_at.desc(),
                EvidenceReferenceRow.evidence_id.desc(),
            )
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
        total = self._session.scalar(select(func.count()).select_from(statement.subquery()))
        return tuple(row.to_domain() for row in rows), int(total or 0)


def _columns(
    row: EvidenceReferenceRow | EvidenceReviewRow | EvidenceReclipRequestRow,
) -> dict[str, object]:
    return {column.name: getattr(row, column.key) for column in row.__table__.columns}


class PostgresEvidenceReviewRepository(EvidenceReviewRepository):
    """人工复核结论的 PostgreSQL 只追加仓储；无更新或删除路径。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    def insert(self, value: EvidenceReview) -> bool:
        row = EvidenceReviewRow.from_domain(value)
        table = cast(Table, EvidenceReviewRow.__table__)
        statement = (
            postgres_insert(table)
            .values(**_columns(row))
            .on_conflict_do_nothing(index_elements=[table.c.review_id])
            .returning(table.c.review_id)
        )
        return self._session.execute(statement).scalar_one_or_none() is not None

    def find(self, review_id: UUID) -> EvidenceReview | None:
        row = self._session.scalar(
            select(EvidenceReviewRow)
            .where(EvidenceReviewRow.review_id == review_id)
            .execution_options(populate_existing=True)
        )
        return None if row is None else row.to_domain()

    def list_for_evidence(self, evidence_id: str) -> tuple[EvidenceReview, ...]:
        rows = self._session.scalars(
            select(EvidenceReviewRow)
            .where(EvidenceReviewRow.evidence_id == evidence_id)
            .order_by(EvidenceReviewRow.reviewed_at.asc(), EvidenceReviewRow.review_id.asc())
        ).all()
        return tuple(row.to_domain() for row in rows)


class PostgresEvidenceReclipRequestRepository(EvidenceReclipRequestRepository):
    """再切片请求的 PostgreSQL 只追加仓储；命令由 device 队列在同一事务内登记。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    def insert(self, value: EvidenceReclipRequest) -> bool:
        row = EvidenceReclipRequestRow.from_domain(value)
        table = cast(Table, EvidenceReclipRequestRow.__table__)
        statement = (
            postgres_insert(table)
            .values(**_columns(row))
            .on_conflict_do_nothing(index_elements=[table.c.request_id])
            .returning(table.c.request_id)
        )
        return self._session.execute(statement).scalar_one_or_none() is not None

    def find(self, request_id: UUID) -> EvidenceReclipRequest | None:
        row = self._session.scalar(
            select(EvidenceReclipRequestRow)
            .where(EvidenceReclipRequestRow.request_id == request_id)
            .execution_options(populate_existing=True)
        )
        return None if row is None else row.to_domain()

    def find_by_command_id(self, command_id: UUID) -> EvidenceReclipRequest | None:
        row = self._session.scalar(
            select(EvidenceReclipRequestRow)
            .where(EvidenceReclipRequestRow.command_id == command_id)
            .execution_options(populate_existing=True)
        )
        return None if row is None else row.to_domain()

    def has_for(
        self,
        *,
        host_id: UUID,
        station_id: str,
        instance_id: int,
        anchor: float,
        window_start: float,
        window_end: float,
    ) -> bool:
        """经原证据登记连接，验证存在对应来源主机/工位/实例/锚点/窗口的真实请求。"""
        statement = (
            select(EvidenceReclipRequestRow.request_id)
            .join(
                EvidenceReferenceRow,
                EvidenceReferenceRow.evidence_id == EvidenceReclipRequestRow.evidence_id,
            )
            .where(
                EvidenceReclipRequestRow.host_id == host_id,
                EvidenceReferenceRow.station_id == station_id,
                EvidenceReferenceRow.instance_id == instance_id,
                EvidenceReclipRequestRow.anchor == anchor,
                func.abs(EvidenceReclipRequestRow.window_start - window_start) <= 1e-6,
                func.abs(EvidenceReclipRequestRow.window_end - window_end) <= 1e-6,
            )
            .limit(1)
        )
        return self._session.scalar(statement) is not None

    def mark_succeeded(self, request_id: UUID, *, result_evidence_id: str) -> bool:
        table = cast(Table, EvidenceReclipRequestRow.__table__)
        result = cast(
            "CursorResult[Any]",
            self._session.execute(
                update(table)
                .where(
                    table.c.request_id == request_id,
                    table.c.status == ReclipRequestStatus.PENDING.value,
                )
                .values(
                    status=ReclipRequestStatus.SUCCEEDED.value,
                    result_evidence_id=result_evidence_id,
                    failure_reason=None,
                )
            ),
        )
        return result.rowcount == 1

    def mark_failed(self, request_id: UUID, *, failure_reason: str) -> bool:
        """仅在 ``PENDING`` 时原子转为 ``FAILED``；不覆盖已成功/已失败终态。"""
        table = cast(Table, EvidenceReclipRequestRow.__table__)
        result = cast(
            "CursorResult[Any]",
            self._session.execute(
                update(table)
                .where(
                    table.c.request_id == request_id,
                    table.c.status == ReclipRequestStatus.PENDING.value,
                )
                .values(
                    status=ReclipRequestStatus.FAILED.value,
                    failure_reason=failure_reason,
                    result_evidence_id=None,
                )
            ),
        )
        return result.rowcount == 1

    def list_for_evidence(self, evidence_id: str) -> tuple[EvidenceReclipRequest, ...]:
        rows = self._session.scalars(
            select(EvidenceReclipRequestRow)
            .where(EvidenceReclipRequestRow.evidence_id == evidence_id)
            .order_by(
                EvidenceReclipRequestRow.requested_at.asc(),
                EvidenceReclipRequestRow.request_id.asc(),
            )
        ).all()
        return tuple(row.to_domain() for row in rows)


__all__ = [
    "PostgresEvidenceReclipRequestRepository",
    "PostgresEvidenceRepository",
    "PostgresEvidenceReviewRepository",
]
