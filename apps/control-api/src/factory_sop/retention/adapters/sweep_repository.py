"""`retention_sweep` 账本的 PostgreSQL 适配器；方法都不提交事务。"""

from __future__ import annotations

from contextlib import AbstractContextManager
from datetime import datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import CursorResult, update
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.orm import Session

from factory_sop.retention.adapters.tables import RetentionSweepRow
from factory_sop.retention.sweep import RetentionSweepResult, RetentionSweepStatus
from factory_sop.retention.usecases import (
    RetentionSweepBegin,
    RetentionSweepLedger,
)


class PostgresRetentionSweepRepository(RetentionSweepLedger):
    """周期身份为主键；未提交事务整体回滚，已提交周期不重复执行。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    def phase(self) -> AbstractContextManager[object]:
        return self._session.begin_nested()

    def begin(self, value: RetentionSweepBegin) -> bool:
        """登记一个周期任务；已有同周期任务时返回 ``False``，不重复执行。"""
        table = cast(Any, RetentionSweepRow.__table__)
        statement = (
            postgres_insert(table)
            .values(
                cycle_id=value.cycle_id,
                status=RetentionSweepStatus.RUNNING.value,
                scope=value.scope,
                result=None,
                failure_code=None,
                started_at=value.started_at,
                finished_at=None,
            )
            .on_conflict_do_nothing(index_elements=[table.c.cycle_id])
            .returning(table.c.cycle_id)
        )
        return self._session.execute(statement).scalar_one_or_none() is not None

    def finish(
        self,
        *,
        cycle_id: UUID,
        result: RetentionSweepResult,
        finished_at: datetime,
        failure_code: str | None,
    ) -> None:
        """以本轮真实结果结案；未完成（部分成功）必须保留 ``failure_code`` 可诊断。"""
        status = (
            RetentionSweepStatus.COMPLETED
            if failure_code is None and result.completed
            else RetentionSweepStatus.FAILED
        )
        statement = cast(
            "CursorResult[Any]",
            self._session.execute(
                update(RetentionSweepRow)
                .where(RetentionSweepRow.cycle_id == cycle_id)
                .values(
                    status=status,
                    result=result.to_wire(),
                    failure_code=failure_code,
                    finished_at=finished_at,
                )
            ),
        )
        if statement.rowcount != 1:
            raise RuntimeError("retention sweep ledger row disappeared before finish")


__all__ = ["PostgresRetentionSweepRepository"]
