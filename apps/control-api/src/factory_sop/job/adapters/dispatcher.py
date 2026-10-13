"""ARQ/Redis 投递适配器；PostgreSQL 任务行是权威 outbox。"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from uuid import UUID

from arq import create_pool
from arq.connections import RedisSettings
from sqlalchemy.orm import Session, sessionmaker

from factory_sop.job.adapters.repository import PostgresJobRepository
from factory_sop.job.api import JobType
from factory_sop.observability import get_logger
from factory_sop.settings import ConfigurationError, Settings, parse_redis_url

_logger = get_logger("job")


def _delivery_id(job_id: UUID, updated_at: datetime) -> str:
    """用 PostgreSQL generation 派生 ARQ 去重身份，避免旧 in-progress key 阻塞恢复。"""
    return f"{job_id}:{updated_at.isoformat(timespec='microseconds')}"


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
    ) -> ArqJobDispatcher:
        """从 Settings 中的 Redis URL 构造投递器；配置无效时显式抛出 `ConfigurationError`。

        直接构造的 Settings 未经过 `from_environment`，所以这里复用同一解析规则，只消费
        完整解析出的连接事实，不假设配置已被校验。
        """
        if settings.redis_url is None:
            raise ConfigurationError("部署必须配置 Redis 任务队列")
        connection = parse_redis_url(settings.redis_url.get_secret_value())
        return cls(
            RedisSettings(
                host=connection.host,
                port=connection.port,
                database=connection.database,
                username=connection.username,
                password=connection.password,
                ssl=connection.ssl,
            ),
            session_factory=session_factory,
        )

    @property
    def redis_settings(self) -> RedisSettings:
        """返回 worker 使用的 Redis 连接配置。

        `arq.RedisSettings.__repr__` 会打印 password，因此本属性不得进入日志或错误信息。
        """
        return self._settings

    async def dispatch_async(self, job_id: UUID) -> None:
        """异步投递一个已提交任务；失败时保持本次 pending generation 可补投。"""
        expected_updated_at: datetime | None = None
        try:
            target = self._worker_target(job_id)
            if target is None:
                # 已知延后类型（DDM 训练命令）：S073 安全派发就绪前不向 ARQ 投递，
                # 也不把它误报为投递失败。
                return
            function, expected_updated_at = target
            accepted = await self._dispatch(
                job_id,
                function=function,
                delivery_id=_delivery_id(job_id, expected_updated_at),
            )
            if accepted:
                self._record_success(job_id, expected_updated_at=expected_updated_at)
        except Exception as error:
            if expected_updated_at is not None:
                self._record_failure(
                    job_id,
                    error,
                    expected_updated_at=expected_updated_at,
                )
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

    async def _dispatch(self, job_id: UUID, *, function: str, delivery_id: str) -> bool:
        pool = await create_pool(self._settings)
        try:
            job = await pool.enqueue_job(function, str(job_id), _job_id=delivery_id)
            return job is not None
        finally:
            await pool.close()

    def _worker_target(self, job_id: UUID) -> tuple[str, datetime] | None:
        """读取投递入口及其 pending generation 的 CAS 时间戳；延后类型返回 ``None``。"""
        if self._session_factory is None:
            raise RuntimeError("任务 dispatcher 未配置数据库 session factory")
        with self._session_factory() as session:
            job = PostgresJobRepository(session).by_id(job_id)
        if job is None:
            raise ValueError(f"任务不存在：{job_id}")
        if job.job_type is JobType.DATASET_DDM_TRAINING:
            # S073/S074 安全派发与结果登记就绪前，DDM 训练命令只保留 outbox 事实，
            # 不注册、不投递可实际 start 的函数；命令仍为 pending 可被现有扫描器接管。
            return None
        if job.job_type is JobType.DATASET_ANNOTATION_PREPARATION:
            return "prepare_annotation_context_job", job.updated_at
        if job.job_type is JobType.DATASET_ANNOTATION:
            return "annotate_dataset_job", job.updated_at
        if job.job_type is JobType.DATASET_VALIDATION:
            return "validate_dataset_job", job.updated_at
        if job.job_type is JobType.DATASET_USAGE_CHECK:
            return "check_dataset_usage_job", job.updated_at
        if job.job_type is JobType.DATASET_ARTIFACT:
            return "generate_dataset_artifact_job", job.updated_at
        raise ValueError(f"未知任务类型：{job.job_type}")

    def _record_success(self, job_id: UUID, *, expected_updated_at: datetime) -> None:
        if self._session_factory is None:
            return
        with self._session_factory() as session:
            repository = PostgresJobRepository(session)
            if not repository.mark_enqueued(
                job_id=job_id,
                expected_updated_at=expected_updated_at,
                now=datetime.now(UTC),
            ):
                repository.mark_running_dispatched(job_id=job_id)
            session.commit()

    def _record_failure(
        self,
        job_id: UUID,
        error: Exception,
        *,
        expected_updated_at: datetime,
    ) -> None:
        if self._session_factory is None:
            return
        with self._session_factory() as session:
            repository = PostgresJobRepository(session)
            repository.record_dispatch_failure(
                job_id=job_id,
                expected_updated_at=expected_updated_at,
                error=f"{type(error).__name__}: {error}",
                now=datetime.now(UTC),
            )
            session.commit()
