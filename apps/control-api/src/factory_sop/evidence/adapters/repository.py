"""evidence 中心引用登记的 PostgreSQL 适配器。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import CursorResult, Table, and_, case, func, select, text, update
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from factory_sop.evidence.adapters.tables import (
    EvidenceDeletionClaimRow,
    EvidencePlaybackLeaseRow,
    EvidenceReclipRequestRow,
    EvidenceReferenceRow,
    EvidenceReviewRow,
    EvidenceReviewSessionRow,
)
from factory_sop.evidence.model import (
    EvidenceDeletionClaim,
    EvidenceImpactCount,
    EvidenceInstanceCount,
    EvidenceReclipRequest,
    EvidenceReference,
    EvidenceReferenceLock,
    EvidenceReview,
    EvidenceReviewHold,
    EvidenceReviewUsageCount,
    EvidenceStatus,
    ReclipRequestStatus,
)
from factory_sop.evidence.repository import (
    EvidenceDeletionClaimRepository,
    EvidenceImpactRepository,
    EvidenceReclipRequestRepository,
    EvidenceRepository,
    EvidenceReviewRepository,
)
from nvsop_contracts import EvidenceDeletionClaimStatus

# 权威变更与删除授权共用的事务级互斥锁；固定 key 使 verify 与策略/工位/签发写入串行。
EVIDENCE_DELETION_AUTHORITY_LOCK_KEY = 0x2064656C65


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

    def lock(self, evidence_id: str) -> EvidenceReferenceLock:
        """锁定该引用行并读取未结案删除认领事实；行不存在时引用为 ``None``。

        锁序固定为"先 ``evidence_evidence`` 行，再认领表"，与删除认领的创建同序，因此引用
        写入者与认领创建之间只会串行、不会死锁（§5.19 引用保护）。
        """
        row = self._session.scalar(
            select(EvidenceReferenceRow)
            .where(EvidenceReferenceRow.evidence_id == evidence_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        pending = self._session.scalar(
            select(EvidenceDeletionClaimRow.claim_id).where(_pending_claim(evidence_id)).limit(1)
        )
        return EvidenceReferenceLock(
            reference=None if row is None else row.to_domain(),
            pending_claim=pending is not None,
        )

    def lock_playback(self, evidence_id: str, *, expires_at: datetime) -> EvidenceReferenceLock:
        """锁行并在引用可用且无未结案认领时登记/续期播放占用；否则只返回锁事实。"""
        locked = self.lock(evidence_id)
        reference = locked.reference
        if (
            reference is not None
            and not locked.pending_claim
            and reference.status is EvidenceStatus.AVAILABLE
        ):
            table = cast(Table, EvidencePlaybackLeaseRow.__table__)
            statement = (
                postgres_insert(table)
                .values(evidence_id=evidence_id, expires_at=expires_at)
                .on_conflict_do_update(
                    index_elements=[table.c.evidence_id],
                    set_={"expires_at": expires_at},
                )
            )
            self._session.execute(statement)
        return locked

    def playback_active(self, evidence_id: str, *, now: datetime) -> bool:
        """该引用是否存在未到期的播放占用。"""
        return (
            self._session.scalar(
                select(EvidencePlaybackLeaseRow.evidence_id)
                .where(
                    EvidencePlaybackLeaseRow.evidence_id == evidence_id,
                    EvidencePlaybackLeaseRow.expires_at > now,
                )
                .limit(1)
            )
            is not None
        )

    def mark_media_deleted(self, evidence_id: str) -> bool:
        """把一条可用引用标记为媒体不可用；已处于该状态的重放返回 ``True``。"""
        table = cast(Table, EvidenceReferenceRow.__table__)
        result = cast(
            "CursorResult[Any]",
            self._session.execute(
                update(table)
                .where(
                    table.c.evidence_id == evidence_id,
                    table.c.status == EvidenceStatus.AVAILABLE.value,
                )
                .values(status=EvidenceStatus.DELETED.value)
            ),
        )
        if result.rowcount == 1:
            return True
        # 非可用状态不在此改写：来源机只能确认自己实际删掉的那条已定稿媒体。
        return (
            self._session.scalar(
                select(EvidenceReferenceRow.evidence_id).where(
                    EvidenceReferenceRow.evidence_id == evidence_id,
                    EvidenceReferenceRow.status == EvidenceStatus.DELETED.value,
                )
            )
            is not None
        )

    def mark_media_missing(self, evidence_id: str) -> bool:
        """把一条仍为可用的引用标记为媒体不可用（源文件确证不存在）。

        这用于来源机无法证明本次成功取消链接、但已确证本机不再持有源文件的终局：媒体按不可用
        结案，不写成已删除；已删除的行保持已删除并视为不可用。
        """
        table = cast(Table, EvidenceReferenceRow.__table__)
        result = cast(
            "CursorResult[Any]",
            self._session.execute(
                update(table)
                .where(
                    table.c.evidence_id == evidence_id,
                    table.c.status != EvidenceStatus.DELETED.value,
                )
                .values(
                    status=EvidenceStatus.FAILED.value,
                    failure_reason="source_missing",
                )
            ),
        )
        if result.rowcount == 1:
            return True
        return (
            self._session.scalar(
                select(EvidenceReferenceRow.evidence_id).where(
                    EvidenceReferenceRow.evidence_id == evidence_id,
                    EvidenceReferenceRow.status == EvidenceStatus.DELETED.value,
                )
            )
            is not None
        )

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
    row: EvidenceReferenceRow
    | EvidenceReviewRow
    | EvidenceReviewSessionRow
    | EvidenceDeletionClaimRow
    | EvidenceReclipRequestRow,
) -> dict[str, object]:
    return {column.name: getattr(row, column.key) for column in row.__table__.columns}


class PostgresEvidenceReviewRepository(EvidenceReviewRepository):
    """人工复核结论的 PostgreSQL 只追加仓储；无更新或删除路径。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    def begin(self, value: EvidenceReviewHold) -> bool:
        """登记一位复核人的在审占用；同一 ``review_id`` 重放不覆盖已存在记录。"""
        row = EvidenceReviewSessionRow(
            review_id=value.review_id,
            evidence_id=value.evidence_id,
            generation=value.generation,
            reviewer_id=value.reviewer_id,
            started_at=value.started_at,
            ended_at=None,
        )
        table = cast(Table, EvidenceReviewSessionRow.__table__)
        statement = (
            postgres_insert(table)
            .values(**_columns(row))
            .on_conflict_do_nothing(index_elements=[table.c.review_id])
            .returning(table.c.review_id)
        )
        return self._session.execute(statement).scalar_one_or_none() is not None

    def find_hold(self, review_id: UUID) -> EvidenceReviewHold | None:
        """按 review_id 读取在审 hold；不存在时返回 ``None``。"""
        row = self._session.scalar(
            select(EvidenceReviewSessionRow)
            .where(EvidenceReviewSessionRow.review_id == review_id)
            .execution_options(populate_existing=True)
        )
        return None if row is None else row.to_domain()

    def cancel(self, *, review_id: UUID, reviewer_id: UUID, at: datetime) -> bool:
        """仅结束指定 reviewer 自己的在审占用；不存在、已结束或非本人返回 ``False``。"""
        table = cast(Table, EvidenceReviewSessionRow.__table__)
        result = cast(
            "CursorResult[Any]",
            self._session.execute(
                update(table)
                .where(
                    table.c.review_id == review_id,
                    table.c.reviewer_id == reviewer_id,
                    table.c.ended_at.is_(None),
                )
                .values(ended_at=at)
            ),
        )
        return result.rowcount == 1

    def conclude_own(self, *, evidence_id: str, reviewer_id: UUID, at: datetime) -> int:
        """结案该 reviewer 在这条证据上的全部在审占用；不触碰其他 reviewer 的占用。"""
        table = cast(Table, EvidenceReviewSessionRow.__table__)
        result = cast(
            "CursorResult[Any]",
            self._session.execute(
                update(table)
                .where(
                    table.c.evidence_id == evidence_id,
                    table.c.reviewer_id == reviewer_id,
                    table.c.ended_at.is_(None),
                )
                .values(ended_at=at)
            ),
        )
        return int(result.rowcount)

    def active_reviewers(self, *, evidence_id: str) -> frozenset[UUID] | None:
        """返回活动 reviewer；历史复核状态未知返回 ``None``，不能当作空集合。"""
        known = self._session.scalar(
            select(EvidenceReferenceRow.review_state_known).where(
                EvidenceReferenceRow.evidence_id == evidence_id
            )
        )
        if known is not True:
            return None
        return frozenset(
            self._session.scalars(
                select(EvidenceReviewSessionRow.reviewer_id).where(
                    EvidenceReviewSessionRow.evidence_id == evidence_id,
                    EvidenceReviewSessionRow.ended_at.is_(None),
                )
            ).all()
        )

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


class PostgresEvidenceDeletionClaimRepository(EvidenceDeletionClaimRepository):
    """删除认领的 PostgreSQL 仓储；未结案唯一性由部分唯一索引与插入冲突共同保证。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    def find(self, claim_id: UUID) -> EvidenceDeletionClaim | None:
        row = self._session.scalar(
            select(EvidenceDeletionClaimRow)
            .where(EvidenceDeletionClaimRow.claim_id == claim_id)
            .execution_options(populate_existing=True)
        )
        return None if row is None else row.to_domain()

    def find_pending(self, evidence_id: str) -> EvidenceDeletionClaim | None:
        """该证据当前未结案的删除认领；没有时返回 ``None``。"""
        row = self._session.scalar(
            select(EvidenceDeletionClaimRow)
            .where(_pending_claim(evidence_id))
            .limit(1)
            .execution_options(populate_existing=True)
        )
        return None if row is None else row.to_domain()

    def insert_if_no_pending(self, value: EvidenceDeletionClaim) -> bool:
        """未结案认领按 ``evidence_id`` 唯一；已有未结案认领时插入被数据库拒绝。"""
        row = EvidenceDeletionClaimRow.from_domain(value)
        table = cast(Table, EvidenceDeletionClaimRow.__table__)
        statement = (
            postgres_insert(table)
            .values(**_columns(row))
            .on_conflict_do_nothing()
            .returning(table.c.claim_id)
        )
        return self._session.execute(statement).scalar_one_or_none() is not None

    def lock_authority(self) -> None:
        """取事务级权威互斥锁；释放由本事务提交/回滚决定，与授权同生共死。"""
        self._session.execute(
            text("SELECT pg_advisory_xact_lock(:key)"),
            {"key": EVIDENCE_DELETION_AUTHORITY_LOCK_KEY},
        )

    def has_authorized_pending(self) -> bool:
        """是否存在已授权未结案删除；权威变更在持锁后调用它做检查。"""
        return (
            self._session.scalar(
                select(EvidenceDeletionClaimRow.claim_id)
                .where(
                    EvidenceDeletionClaimRow.status == EvidenceDeletionClaimStatus.PENDING.value,
                    EvidenceDeletionClaimRow.authorized_at.is_not(None),
                )
                .limit(1)
            )
            is not None
        )

    def authorize(self, claim_id: UUID, *, at: datetime) -> bool:
        """仅在仍为未结案时写入首次授权时刻；幂等重放保留原时刻。"""
        table = cast(Table, EvidenceDeletionClaimRow.__table__)
        result = cast(
            "CursorResult[Any]",
            self._session.execute(
                update(table)
                .where(
                    table.c.claim_id == claim_id,
                    table.c.status == EvidenceDeletionClaimStatus.PENDING.value,
                )
                .values(authorized_at=func.coalesce(table.c.authorized_at, at))
            ),
        )
        return result.rowcount == 1

    def refresh_deadline(self, claim_id: UUID, *, expires_at: datetime) -> bool:
        """仅在仍为未结案时刷新票据截止；已结案/不存在返回 ``False``。"""
        table = cast(Table, EvidenceDeletionClaimRow.__table__)
        result = cast(
            "CursorResult[Any]",
            self._session.execute(
                update(table)
                .where(
                    table.c.claim_id == claim_id,
                    table.c.status == EvidenceDeletionClaimStatus.PENDING.value,
                )
                .values(expires_at=expires_at)
            ),
        )
        return result.rowcount == 1

    def settle(
        self,
        claim_id: UUID,
        *,
        status: EvidenceDeletionClaimStatus,
        reason_code: str,
        detail: str | None,
        settled_at: datetime,
    ) -> bool:
        """仅在仍为 ``PENDING`` 时结案；CAS 失败返回 ``False``，不覆盖既有终局。"""
        table = cast(Table, EvidenceDeletionClaimRow.__table__)
        result = cast(
            "CursorResult[Any]",
            self._session.execute(
                update(table)
                .where(
                    table.c.claim_id == claim_id,
                    table.c.status == EvidenceDeletionClaimStatus.PENDING.value,
                )
                .values(
                    status=status.value,
                    reason_code=reason_code,
                    detail=detail,
                    settled_at=settled_at,
                )
            ),
        )
        return result.rowcount == 1


class PostgresEvidenceImpactRepository(EvidenceImpactRepository):
    """保留影响估算的只读计数；按实例聚合引用行，不读取媒体字节（ADR-0012）。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    def review_usage(
        self,
        *,
        station_id: str | None,
        exposed_from: datetime | None,
        exposed_to: datetime,
    ) -> tuple[EvidenceReviewUsageCount, ...]:
        """按实例聚合窗口内引用的复核状态；复核占用表只在 evidence 内部连接。"""
        table = cast(Table, EvidenceReferenceRow.__table__)
        sessions = cast(Table, EvidenceReviewSessionRow.__table__)
        conditions = [table.c.received_at <= exposed_to]
        if exposed_from is not None:
            conditions.append(table.c.received_at > exposed_from)
        if station_id is not None:
            conditions.append(table.c.station_id == station_id)
        statement = (
            select(
                table.c.host_id,
                table.c.station_id,
                table.c.instance_id,
                func.bool_and(table.c.review_state_known).label("known"),
                func.bool_or(sessions.c.review_id.is_not(None)).label("active"),
            )
            .select_from(
                table.outerjoin(
                    sessions,
                    and_(
                        sessions.c.evidence_id == table.c.evidence_id,
                        sessions.c.ended_at.is_(None),
                    ),
                )
            )
            .where(*conditions)
            .group_by(table.c.host_id, table.c.station_id, table.c.instance_id)
        )
        return tuple(
            EvidenceReviewUsageCount(
                host_id=_as_uuid(str(row.host_id)),
                station_id=_as_uuid(str(row.station_id)),
                instance_id=int(row.instance_id),
                known=bool(row.known),
                active=bool(row.active),
            )
            for row in self._session.execute(statement).all()
        )

    def evidence_usage(
        self,
        *,
        station_id: str | None,
        exposed_from: datetime | None,
        exposed_to: datetime,
    ) -> tuple[EvidenceInstanceCount, ...]:
        table = cast(Table, EvidenceReferenceRow.__table__)
        linked = table.c.violation_id.is_not(None)
        conditions = [table.c.received_at <= exposed_to]
        if exposed_from is not None:
            conditions.append(table.c.received_at > exposed_from)
        if station_id is not None:
            conditions.append(table.c.station_id == station_id)
        instance_id = table.c.instance_id
        statement = (
            select(
                table.c.host_id.label("host_id"),
                table.c.station_id.label("station_id"),
                instance_id.label("instance_id"),
                linked.label("linked"),
                func.count().label("objects"),
                func.coalesce(func.sum(table.c.size), 0).label("known_bytes"),
                func.coalesce(func.sum(case((table.c.size.is_(None), 1), else_=0)), 0).label(
                    "unknown_size_objects"
                ),
                func.min(table.c.received_at).label("earliest"),
                func.max(table.c.received_at).label("latest"),
            )
            .where(*conditions)
            .group_by(table.c.host_id, table.c.station_id, instance_id, linked)
        )
        grouped: dict[tuple[str, str, int], dict[bool, EvidenceImpactCount]] = {}
        for row in self._session.execute(statement).all():
            aggregate = EvidenceImpactCount(
                objects=int(row.objects),
                known_bytes=int(row.known_bytes),
                unknown_size_objects=int(row.unknown_size_objects),
                earliest=cast("datetime | None", row.earliest),
                latest=cast("datetime | None", row.latest),
            )
            grouped.setdefault((str(row.host_id), str(row.station_id), int(row.instance_id)), {})[
                bool(row.linked)
            ] = aggregate
        empty = EvidenceImpactCount(0, 0, 0, None, None)
        return tuple(
            EvidenceInstanceCount(
                host_id=_as_uuid(host),
                station_id=_as_uuid(station),
                instance_id=instance,
                unlinked=split.get(False, empty),
                linked=split.get(True, empty),
            )
            for (host, station, instance), split in grouped.items()
        )


def _pending_claim(evidence_id: str) -> ColumnElement[bool]:
    """未结案删除认领的判定条件；引用行锁与认领仓储共用同一条规则。"""
    table = cast(Table, EvidenceDeletionClaimRow.__table__)
    return and_(
        table.c.evidence_id == evidence_id,
        table.c.status == EvidenceDeletionClaimStatus.PENDING.value,
    )


def _as_uuid(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError as error:
        raise ValueError("evidence reference host/station id is not a valid identity") from error


__all__ = [
    "PostgresEvidenceDeletionClaimRepository",
    "PostgresEvidenceImpactRepository",
    "PostgresEvidenceReclipRequestRepository",
    "PostgresEvidenceRepository",
    "PostgresEvidenceReviewRepository",
]
