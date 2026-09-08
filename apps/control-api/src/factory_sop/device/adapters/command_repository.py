"""`device_pending_command` 的 PostgreSQL 适配器。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import CursorResult, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import DatabaseError
from sqlalchemy.orm import Session as DatabaseSession

from factory_sop.device.adapters.tables import PendingCommandRow
from factory_sop.device.errors import DeviceRefusalCode, DeviceRefusedError
from factory_sop.device.model import (
    PendingCommand,
    PendingCommandCompletion,
    PendingCommandStatus,
)


class PostgresPendingCommandRepository:
    """通过请求级事务保存、领取和完成委托命令。"""

    def __init__(self, session: DatabaseSession) -> None:
        self._session = session

    def by_id(self, command_id: UUID) -> PendingCommand | None:
        row = self._session.get(PendingCommandRow, command_id)
        return row.to_domain() if row is not None else None

    def by_idempotency_key(self, key: str) -> PendingCommand | None:
        row = self._session.scalar(
            select(PendingCommandRow).where(PendingCommandRow.idempotency_key == key)
        )
        return row.to_domain() if row is not None else None

    def add(self, command: PendingCommand) -> None:
        self._session.add(PendingCommandRow.from_domain(command))
        try:
            self._session.flush()
        except DatabaseError as error:
            constraint = getattr(getattr(error.orig, "diag", None), "constraint_name", None)
            if constraint == "uq_device_pending_command_idempotency_key":
                raise DeviceRefusedError(DeviceRefusalCode.COMMAND_IDEMPOTENCY_CONFLICT) from error
            raise

    def add_or_get(self, command: PendingCommand) -> PendingCommand:
        """用 PostgreSQL 冲突忽略原子插入，并返回唯一已持久化命令。"""
        row = PendingCommandRow.from_domain(command)
        values = {
            "id": row.id,
            "host_id": row.host_id,
            "command_type": row.command_type,
            "target_id": row.target_id,
            "target_revision": row.target_revision,
            "idempotency_key": row.idempotency_key,
            "status": row.status,
            "attempt": row.attempt,
            "claim_token": row.claim_token,
            "claimed_at": row.claimed_at,
            "lease_expires_at": row.lease_expires_at,
            "result": row.result,
            "result_detail": row.result_detail,
            "failure_code": row.failure_code,
            "completed_at": row.completed_at,
            "created_by": row.created_by,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }
        self._session.execute(
            insert(PendingCommandRow)
            .values(values)
            .on_conflict_do_nothing(index_elements=[PendingCommandRow.idempotency_key])
        )
        stored = self._session.scalar(
            select(PendingCommandRow).where(
                PendingCommandRow.idempotency_key == command.idempotency_key
            )
        )
        if stored is None:
            raise RuntimeError("idempotent command insert was not readable")
        return stored.to_domain()

    def claim_next(
        self,
        *,
        host_id: UUID,
        claim_token: str,
        claimed_at: datetime,
        lease_expires_at: datetime,
    ) -> PendingCommand | None:
        """在一个事务中回收过期领取并锁定该主机的最早待命令。"""
        self._session.execute(
            update(PendingCommandRow)
            .where(
                PendingCommandRow.status == PendingCommandStatus.CLAIMED,
                PendingCommandRow.lease_expires_at.is_not(None),
                PendingCommandRow.lease_expires_at <= claimed_at,
            )
            .values(
                status=PendingCommandStatus.PENDING,
                claim_token=None,
                claimed_at=None,
                lease_expires_at=None,
                updated_at=claimed_at,
            )
        )
        row = self._session.scalar(
            select(PendingCommandRow)
            .where(
                PendingCommandRow.host_id == host_id,
                PendingCommandRow.status == PendingCommandStatus.PENDING,
            )
            .order_by(PendingCommandRow.created_at, PendingCommandRow.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if row is None:
            return None
        row.status = PendingCommandStatus.CLAIMED
        row.attempt += 1
        row.claim_token = claim_token
        row.claimed_at = claimed_at
        row.lease_expires_at = lease_expires_at
        row.updated_at = claimed_at
        self._session.flush()
        return row.to_domain()

    def complete(self, completion: PendingCommandCompletion) -> PendingCommand:
        """用条件更新保护主机身份、令牌和领取状态。"""
        row = self._session.get(PendingCommandRow, completion.command_id)
        if row is None:
            raise DeviceRefusedError(DeviceRefusalCode.COMMAND_NOT_FOUND)
        if row.host_id != completion.host_id:
            raise DeviceRefusedError(DeviceRefusalCode.COMMAND_HOST_MISMATCH)
        if row.status in {
            PendingCommandStatus.SUCCEEDED,
            PendingCommandStatus.FAILED,
            PendingCommandStatus.REJECTED,
        }:
            if (
                row.claim_token == completion.claim_token
                and row.result == completion.result
                and row.result_detail == completion.result_detail
                and row.failure_code == completion.failure_code
            ):
                return row.to_domain()
            raise DeviceRefusedError(DeviceRefusalCode.COMMAND_ALREADY_COMPLETED)
        if row.status is not PendingCommandStatus.CLAIMED:
            raise DeviceRefusedError(DeviceRefusalCode.COMMAND_CLAIM_REQUIRED)
        if row.claim_token != completion.claim_token:
            raise DeviceRefusedError(DeviceRefusalCode.COMMAND_CLAIM_TOKEN_INVALID)
        if row.lease_expires_at is None or completion.completed_at >= row.lease_expires_at:
            raise DeviceRefusedError(DeviceRefusalCode.COMMAND_CLAIM_EXPIRED)

        changed = cast(
            "CursorResult[Any]",
            self._session.execute(
                update(PendingCommandRow)
                .where(
                    PendingCommandRow.id == completion.command_id,
                    PendingCommandRow.host_id == completion.host_id,
                    PendingCommandRow.status == PendingCommandStatus.CLAIMED,
                    PendingCommandRow.claim_token == completion.claim_token,
                    PendingCommandRow.lease_expires_at > completion.completed_at,
                )
                .values(
                    status=completion.status,
                    result=completion.result,
                    result_detail=completion.result_detail,
                    failure_code=completion.failure_code,
                    completed_at=completion.completed_at,
                    updated_at=completion.completed_at,
                )
            ),
        )
        if changed.rowcount != 1:
            raise DeviceRefusedError(DeviceRefusalCode.COMMAND_CLAIM_EXPIRED)
        loaded = self._session.get(PendingCommandRow, completion.command_id)
        if loaded is None:
            raise DeviceRefusedError(DeviceRefusalCode.COMMAND_NOT_FOUND)
        return loaded.to_domain()
