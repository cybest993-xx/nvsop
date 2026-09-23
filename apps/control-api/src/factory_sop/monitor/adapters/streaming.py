"""monitor SSE 的短读与 PostgreSQL 提交后唤醒适配器。"""

from __future__ import annotations

from typing import Any, cast

from psycopg import Connection as PsycopgConnection
from sqlalchemy import Connection, Engine
from sqlalchemy.orm import Session, sessionmaker

from factory_sop.monitor.adapters.repository import (
    MONITOR_STREAM_CHANNEL,
    PostgresMonitorRepository,
)
from factory_sop.monitor.model import MirroredDecision, MirroredHealth
from factory_sop.monitor.repository import MonitorStreamSource


class PostgresMonitorStreamSource(MonitorStreamSource):
    """LISTEN 连接只负责提示；事实始终从短生命周期 ORM Session 重放。"""

    def __init__(self, factory: sessionmaker[Session], engine: Engine) -> None:
        self._factory = factory
        self._engine = engine
        self._listener: Connection | None = None

    def read_after_sequences(
        self,
        *,
        decision_sequence: int,
        health_sequence: int,
        limit: int,
    ) -> tuple[tuple[MirroredDecision, ...], tuple[MirroredHealth, ...]]:
        # 先 LISTEN 再查 durable cursor，消除“查询结束到开始等待”之间的丢唤醒窗口。
        self._ensure_listener()
        with self._factory() as session:
            repository = PostgresMonitorRepository(session)
            decisions = repository.decisions_after_sequence(
                after_sequence=decision_sequence,
                limit=limit,
            )
            health = repository.health_after_sequence(
                after_sequence=health_sequence,
                limit=limit,
            )
        return decisions, health

    def wait_for_wakeup(self, *, timeout: float) -> bool:
        listener = self._ensure_listener()
        driver = cast(PsycopgConnection[Any], listener.connection.driver_connection)
        if next(driver.notifies(timeout=timeout, stop_after=1), None) is None:
            return False
        # 通知只是边沿提示；一次被唤醒后合并当前已排队提示，下一轮只重放 durable cursor。
        for _ in driver.notifies(timeout=0):
            pass
        return True

    def close(self) -> None:
        listener = self._listener
        if listener is None:
            return
        self._listener = None
        try:
            listener.exec_driver_sql(f"UNLISTEN {MONITOR_STREAM_CHANNEL}")
        finally:
            listener.close()

    def _ensure_listener(self) -> Connection:
        if self._listener is None:
            listener = self._engine.connect().execution_options(isolation_level="AUTOCOMMIT")
            try:
                listener.exec_driver_sql(f"LISTEN {MONITOR_STREAM_CHANNEL}")
            except BaseException:
                listener.close()
                raise
            self._listener = listener
        return self._listener


__all__ = ["PostgresMonitorStreamSource"]
