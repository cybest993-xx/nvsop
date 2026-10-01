"""retention 全局策略的 PostgreSQL 适配器；方法不提交事务。"""

from __future__ import annotations

from typing import Any, cast

from sqlalchemy import CursorResult, Table, select, update
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.orm import Session

from factory_sop.retention.adapters.tables import RetentionPolicyRow
from factory_sop.retention.model import RetentionPolicyState
from factory_sop.retention.usecases import RetentionPolicyRepository


class PostgresRetentionPolicyRepository(RetentionPolicyRepository):
    def __init__(self, session: Session) -> None:
        self._session = session

    def get_global(self) -> RetentionPolicyState | None:
        row = self._session.scalar(
            select(RetentionPolicyRow)
            .where(RetentionPolicyRow.singleton == 1)
            .execution_options(populate_existing=True)
        )
        return None if row is None else row.to_domain()

    def replace_if_current(self, *, expected_revision: int, value: RetentionPolicyState) -> bool:
        """expected_revision==0 时仅首次插入；否则只在 revision 仍相等时更新。"""
        table = cast(Table, RetentionPolicyRow.__table__)
        policy, revision = value.policy.to_wire(), value.revision
        if expected_revision == 0:
            insert_statement = (
                postgres_insert(table)
                .values(singleton=1, policy=policy, revision=revision)
                .on_conflict_do_nothing(index_elements=[table.c.singleton])
                .returning(table.c.singleton)
            )
            return self._session.execute(insert_statement).scalar_one_or_none() is not None
        update_statement = (
            update(table)
            .where(table.c.singleton == 1, table.c.revision == expected_revision)
            .values(policy=policy, revision=revision)
        )
        result = cast("CursorResult[Any]", self._session.execute(update_statement))
        return result.rowcount == 1


__all__ = ["PostgresRetentionPolicyRepository"]
