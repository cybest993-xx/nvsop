"""retention 全局策略表的 SQLAlchemy 行模型；单行存储，无历史数据。"""

from __future__ import annotations

from sqlalchemy import Integer, SmallInteger
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from factory_sop.persistence import Table
from factory_sop.retention.model import RetentionPolicy, RetentionPolicyState


class RetentionPolicyRow(Table):
    __tablename__ = "retention_policy"

    singleton: Mapped[int] = mapped_column(SmallInteger(), primary_key=True)
    policy: Mapped[dict[str, object]] = mapped_column(JSONB())
    revision: Mapped[int] = mapped_column(Integer())

    def to_domain(self) -> RetentionPolicyState:
        return RetentionPolicyState(
            policy=RetentionPolicy.from_wire(self.policy), revision=self.revision
        )

    @classmethod
    def from_domain(cls, value: RetentionPolicyState) -> RetentionPolicyRow:
        return cls(singleton=1, policy=value.policy.to_wire(), revision=value.revision)


__all__ = ["RetentionPolicyRow"]
