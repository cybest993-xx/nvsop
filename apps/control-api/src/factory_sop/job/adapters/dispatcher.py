"""ARQ/Redis 投递适配器；PostgreSQL 任务行是权威 outbox。"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from urllib.parse import urlsplit
from uuid import UUID

from arq import create_pool
from arq.connections import RedisSettings
from sqlalchemy.orm import Session, sessionmaker

from factory_sop.job.adapters.repository import PostgresJobRepository
from factory_sop.job.api import JobDispatcher
from factory_sop.observability import get_logger
from factory_sop.settings import Settings

_logger = get_logger("job")


class ArqJobDispatcher:
    """提交后把 job id 投递到 Redis，并回写 outbox 的投递事实。"""

    def __init__(
        self,
        settings: RedisSettings,
        *,
        session_factory: sessionmaker[Session] | None = None,
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        session_factory: sessionmaker[Session] | None = None,
    ) -> JobDispatcher:
        """从 Redis URL 构造投递器；未配置时只保留待补投 outbox。"""
        if settings.redis_url is None:
            return OutboxOnlyDispatcher()
        try:
            parsed = urlsplit(settings.redis_url.get_secret_value())
            if parsed.scheme not in {"redis", "rediss"} or not parsed.hostname:
                raise ValueError("Redis URL 必须使用 redis(s) scheme")
            database = int(parsed.path.strip("/") or "0")
            return cls(
                RedisSettings(
                    host=parsed.hostname,
                    port=parsed.port or 6379,
                    database=database,
                    username=parsed.username,
                    password=parsed.password,
                    ssl=parsed.scheme == "rediss",
                ),
                session_factory=session_factory,
            )
        except (TypeError, ValueError):
            return OutboxOnlyDispatcher()

    @property
    def redis_settings(self) -> RedisSettings:
        """返回 worker 使用的 Redis 连接配置。"""
        return self._settings

    async def dispatch_async(self, job_id: UUID) -> None:
        """异步投递一个已提交任务；失败时保持 outbox pending。"""
        try:
            await self._dispatch(job_id)
            self._record_success(job_id)
        except Exception as error:
            self._record_failure(job_id, error)
            _logger.warning(
                "job.dispatch.failed",
                job_id=str(job_id),
                error_type=type(error).__name__,
            )

    def dispatch(self, job_id: UUID) -> None:
        """同步提交后投递；在已有事件循环中使用独立线程避免嵌套 asyncio.run。"""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(self.dispatch_async(job_id))
            return
        with ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(lambda: asyncio.run(self.dispatch_async(job_id))).result()

    async def _dispatch(self, job_id: UUID) -> None:
        pool = await create_pool(self._settings)
        try:
            await pool.enqueue_job(
                "validate_dataset_job",
                str(job_id),
                _job_id=str(job_id),
            )
        finally:
            await pool.close()

    def _record_success(self, job_id: UUID) -> None:
        if self._session_factory is None:
            return
        with self._session_factory() as session:
            repository = PostgresJobRepository(session)
            repository.mark_enqueued(job_id=job_id, now=datetime.now(UTC))
            session.commit()

    def _record_failure(self, job_id: UUID, error: Exception) -> None:
        if self._session_factory is None:
            return
        with self._session_factory() as session:
            repository = PostgresJobRepository(session)
            repository.record_dispatch_failure(
                job_id=job_id,
                error=f"{type(error).__name__}: {error}",
                now=datetime.now(UTC),
            )
            session.commit()


class OutboxOnlyDispatcher:
    """Redis 未配置或 URL 无效时的安全降级：不丢弃已提交任务，不伪造执行。"""

    def dispatch(self, job_id: UUID) -> None:
        _logger.warning("job.dispatch.deferred", job_id=str(job_id))
