"""数据集 owner 的制品执行与候选清理。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from typing import Any, cast
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from factory_sop.dataset.adapters.repository import PostgresDatasetRepository
from factory_sop.dataset.api import (
    ArtifactExecutionOutcome,
    ArtifactExecutionResult,
    ArtifactJobFinisher,
    ArtifactTransactionCommitter,
)
from factory_sop.dataset.errors import DatasetRefusalCode, DatasetRefusedError
from factory_sop.dataset.model import ArtifactStatus
from factory_sop.dataset.storage import ObjectStorage, ObjectStorageUnavailableError
from factory_sop.dataset.usecases.usage import (
    ArtifactTarget,
    begin_artifact_generation,
    complete_artifact,
    complete_artifact_candidate_cleanup,
    fail_artifact,
    invalidate_artifact_for_job,
    mark_artifact_cleanup_pending,
    record_artifact_orphan_candidate,
    render_ddm_artifact_with_base,
)
from factory_sop.job.api import ApplicationJob
from factory_sop.observability import get_logger

_logger = get_logger("dataset")


class PostgresDatasetArtifactExecutor:
    """通过单一公共 seam 执行完整的数据集制品生命周期。"""

    def __init__(
        self,
        *,
        factory: sessionmaker[Session],
        storage_factory: Callable[[], ObjectStorage],
        generate: Callable[[Path, str], bytes],
    ) -> None:
        self._factory = factory
        self._storage_factory = storage_factory
        self._generate = generate

    def execute(
        self,
        *,
        job: ApplicationJob,
        finish_job: ArtifactJobFinisher,
        commit_transaction: ArtifactTransactionCommitter,
    ) -> ArtifactExecutionResult:
        """生成制品，并在同一事务内组合数据集与任务终态。"""
        stale_object_keys: tuple[str, ...] = ()
        with self._factory() as session:
            datasets = PostgresDatasetRepository(session)
            target = begin_artifact_generation(job=job, datasets=datasets, now=datetime.now(UTC))
            if target is None:
                stale_artifact = datasets.artifact_by_id(job.attempt_id)
                if (
                    stale_artifact is not None
                    and stale_artifact.job_id == job.id
                    and stale_artifact.status
                    in {ArtifactStatus.PENDING, ArtifactStatus.RUNNING, ArtifactStatus.FAILED}
                ):
                    stale_object_keys = _artifact_candidate_keys(
                        object_key=stale_artifact.object_key,
                        manifest=stale_artifact.manifest,
                    )
                invalidate_artifact_for_job(
                    job=job,
                    datasets=datasets,
                    now=datetime.now(UTC),
                )
            if not commit_transaction(session):
                return ArtifactExecutionResult(ArtifactExecutionOutcome.LEASE_LOST)

        if target is None:
            return self._finish_superseded(
                job=job,
                object_keys=stale_object_keys,
                finish_job=finish_job,
                commit_transaction=commit_transaction,
            )

        object_key = cast(str, target.artifact.object_key)
        try:
            storage = self._storage_factory()
        except Exception as error:
            return self._finish_failure(
                target=target,
                finish_job=finish_job,
                commit_transaction=commit_transaction,
                code="STORAGE_UNAVAILABLE",
                detail="训练素材存储暂时不可用，请稍后重试",
                error=error,
            )

        try:
            for orphan_key in _artifact_candidate_keys(
                object_key=None,
                manifest=target.artifact.manifest,
            ):
                if not self._discard_candidate(storage=storage, object_key=orphan_key):
                    raise DatasetRefusedError(
                        DatasetRefusalCode.STORAGE_UNAVAILABLE,
                        detail="旧制品候选清理失败，请稍后重试",
                    )
            self._discard_candidate(storage=storage, object_key=object_key)
            content, manifest = render_ddm_artifact_with_base(
                target,
                storage=storage,
                generate=self._generate,
            )
            with storage.writing(object_key=object_key) as sink:
                sink.write(content)
            stored = storage.stat(object_key=object_key)
            if stored.size != len(content):
                self._discard_candidate(storage=storage, object_key=object_key)
                raise DatasetRefusedError(
                    DatasetRefusalCode.ARTIFACT_INTEGRITY_FAILURE,
                    detail="制品对象大小与生成内容不一致",
                )
            readback = BytesIO()
            storage.download_to(object_key=object_key, destination=readback)
            if readback.getvalue() != content:
                self._discard_candidate(storage=storage, object_key=object_key)
                raise DatasetRefusedError(
                    DatasetRefusalCode.ARTIFACT_INTEGRITY_FAILURE,
                    detail="制品对象回读摘要与生成内容不一致",
                )
        except Exception as error:  # pragma: no cover - worker 隔离由集成测试覆盖
            if not self._discard_candidate(storage=storage, object_key=object_key):
                return self._mark_cleanup_pending(
                    target=target,
                    object_key=object_key,
                    error=error,
                )
            code = (
                error.code.value
                if isinstance(error, DatasetRefusedError)
                else (
                    "STORAGE_UNAVAILABLE"
                    if isinstance(error, ObjectStorageUnavailableError)
                    else "ARTIFACT_GENERATION_FAILED"
                )
            )
            detail = (
                error.detail
                if isinstance(error, DatasetRefusedError)
                else (
                    "训练素材存储暂时不可用，请稍后重试"
                    if isinstance(error, ObjectStorageUnavailableError)
                    else "DDM annotation 制品生成失败"
                )
            )
            return self._finish_failure(
                target=target,
                finish_job=finish_job,
                commit_transaction=commit_transaction,
                code=code,
                detail=detail,
                error=error,
            )

        return self._publish(
            target=target,
            object_key=object_key,
            manifest=manifest,
            storage=storage,
            finish_job=finish_job,
            commit_transaction=commit_transaction,
        )

    def cleanup_candidates(self) -> None:
        """清理已持久化的未发布候选，不向 job 暴露清理规则。"""
        with self._factory() as session:
            candidates = tuple(
                PostgresDatasetRepository(session).list_artifact_cleanup_candidates(limit=100)
            )
        if not candidates:
            return
        try:
            storage = self._storage_factory()
        except Exception:
            _logger.exception("job.dataset_artifact.cleanup_storage_unavailable")
            return
        for artifact in candidates:
            keys = _artifact_candidate_keys(
                object_key=(
                    artifact.object_key if artifact.status is not ArtifactStatus.AVAILABLE else None
                ),
                manifest=artifact.manifest,
            )
            if not keys:
                continue
            if not all(self._discard_candidate(storage=storage, object_key=key) for key in keys):
                continue
            with self._factory() as session:
                datasets = PostgresDatasetRepository(session)
                cleared = complete_artifact_candidate_cleanup(
                    artifact_id=artifact.id,
                    job_id=None,
                    candidate_keys=keys,
                    expected_updated_at=artifact.updated_at,
                    now=datetime.now(UTC),
                    datasets=datasets,
                )
                if not cleared:
                    session.rollback()
                    continue
                session.commit()
            _logger.info(
                "job.dataset_artifact.candidate_cleanup.finished",
                dataset_id=str(artifact.dataset_id),
                artifact_id=str(artifact.id),
                object_keys=keys,
                result="succeeded",
            )

    def _finish_superseded(
        self,
        *,
        job: ApplicationJob,
        object_keys: tuple[str, ...],
        finish_job: ArtifactJobFinisher,
        commit_transaction: ArtifactTransactionCommitter,
    ) -> ArtifactExecutionResult:
        if object_keys:
            try:
                storage = self._storage_factory()
                cleanup_succeeded = all(
                    self._discard_candidate(storage=storage, object_key=key) for key in object_keys
                )
            except Exception:
                cleanup_succeeded = False
                _logger.exception(
                    "job.dataset_artifact.candidate_cleanup_unavailable",
                    job_id=str(job.id),
                    dataset_id=str(job.dataset_id),
                    artifact_id=str(job.attempt_id),
                    object_keys=object_keys,
                    result="retry_pending",
                )
            if not cleanup_succeeded:
                _logger.warning(
                    "job.dataset_artifact.candidate_cleanup_retry",
                    job_id=str(job.id),
                    dataset_id=str(job.dataset_id),
                    artifact_id=str(job.attempt_id),
                    object_keys=object_keys,
                    result="retry_pending",
                )
                return ArtifactExecutionResult(ArtifactExecutionOutcome.RETRY_PENDING)
            with self._factory() as session:
                datasets = PostgresDatasetRepository(session)
                cleared = complete_artifact_candidate_cleanup(
                    artifact_id=job.attempt_id,
                    job_id=job.id,
                    now=datetime.now(UTC),
                    datasets=datasets,
                )
                if not cleared:
                    session.rollback()
                    _logger.warning(
                        "job.dataset_artifact.candidate_cleanup_lease_lost",
                        job_id=str(job.id),
                        dataset_id=str(job.dataset_id),
                        artifact_id=str(job.attempt_id),
                        object_keys=object_keys,
                        result="retry_pending",
                    )
                    return ArtifactExecutionResult(ArtifactExecutionOutcome.RETRY_PENDING)
                result = ArtifactExecutionResult(ArtifactExecutionOutcome.SUPERSEDED)
                if not finish_job(session, result):
                    session.rollback()
                    return ArtifactExecutionResult(ArtifactExecutionOutcome.LEASE_LOST)
                if not commit_transaction(session):
                    return ArtifactExecutionResult(ArtifactExecutionOutcome.LEASE_LOST)
            _logger.info(
                "job.dataset_artifact.candidate_cleanup.finished",
                job_id=str(job.id),
                dataset_id=str(job.dataset_id),
                artifact_id=str(job.attempt_id),
                object_keys=object_keys,
                result="succeeded",
            )
            return result

        with self._factory() as session:
            result = ArtifactExecutionResult(ArtifactExecutionOutcome.SUPERSEDED)
            if not finish_job(session, result):
                session.rollback()
                return ArtifactExecutionResult(ArtifactExecutionOutcome.LEASE_LOST)
            if not commit_transaction(session):
                return ArtifactExecutionResult(ArtifactExecutionOutcome.LEASE_LOST)
        return result

    def _publish(
        self,
        *,
        target: ArtifactTarget,
        object_key: str,
        manifest: Mapping[str, Any],
        storage: ObjectStorage,
        finish_job: ArtifactJobFinisher,
        commit_transaction: ArtifactTransactionCommitter,
    ) -> ArtifactExecutionResult:
        with self._factory() as session:
            datasets = PostgresDatasetRepository(session)
            try:
                completed = complete_artifact(
                    target=target,
                    object_key=object_key,
                    artifact_sha256=str(manifest["artifact_sha256"]),
                    artifact_size=int(manifest["artifact_size"]),
                    manifest=manifest,
                    now=datetime.now(UTC),
                    datasets=datasets,
                )
                result = ArtifactExecutionResult(ArtifactExecutionOutcome.SUCCEEDED)
                if not finish_job(session, result):
                    session.rollback()
                    self._record_orphan(
                        artifact_id=target.artifact.id,
                        object_key=object_key,
                        error=RuntimeError("artifact job lease lost"),
                    )
                    return ArtifactExecutionResult(ArtifactExecutionOutcome.LEASE_LOST)
                try:
                    if not commit_transaction(session):
                        self._record_orphan(
                            artifact_id=target.artifact.id,
                            object_key=object_key,
                            error=RuntimeError("artifact execution cancelled"),
                        )
                        return ArtifactExecutionResult(ArtifactExecutionOutcome.LEASE_LOST)
                except Exception:  # pragma: no cover - 需要真实数据库故障注入
                    _logger.exception(
                        "job.dataset_artifact.commit_unknown",
                        job_id=str(target.job.id),
                        dataset_id=str(completed.dataset_id),
                        usage_kind=completed.kind.value,
                        artifact_id=str(completed.id),
                        usage_check_id=str(completed.usage_check_id),
                        input_digest=completed.input_digest,
                        actor_id=str(completed.created_by),
                        result="commit_unknown",
                    )
                    return ArtifactExecutionResult(ArtifactExecutionOutcome.COMMIT_UNKNOWN)
            except DatasetRefusedError as error:
                session.rollback()
                if not self._discard_candidate(storage=storage, object_key=object_key):
                    return self._mark_cleanup_pending(
                        target=target,
                        object_key=object_key,
                        error=error,
                    )
                return self._finish_failure(
                    target=target,
                    finish_job=finish_job,
                    commit_transaction=commit_transaction,
                    code=error.code.value,
                    detail=error.detail,
                    error=error,
                )
            except Exception as error:  # pragma: no cover - 真实数据库故障由集成测试覆盖
                session.rollback()
                if not self._discard_candidate(storage=storage, object_key=object_key):
                    return self._mark_cleanup_pending(
                        target=target,
                        object_key=object_key,
                        error=error,
                    )
                return self._finish_failure(
                    target=target,
                    finish_job=finish_job,
                    commit_transaction=commit_transaction,
                    code="ARTIFACT_DATABASE_FAILURE",
                    detail="制品结果登记失败",
                    error=error,
                )
        _logger.info(
            "job.dataset_artifact.finished",
            job_id=str(target.job.id),
            dataset_id=str(completed.dataset_id),
            usage_kind=completed.kind.value,
            artifact_id=str(completed.id),
            usage_check_id=str(completed.usage_check_id),
            input_digest=completed.input_digest,
            actor_id=str(completed.created_by),
            result="succeeded",
        )
        return ArtifactExecutionResult(ArtifactExecutionOutcome.SUCCEEDED)

    def _finish_failure(
        self,
        *,
        target: ArtifactTarget,
        finish_job: ArtifactJobFinisher,
        commit_transaction: ArtifactTransactionCommitter,
        code: str,
        detail: str,
        error: Exception,
    ) -> ArtifactExecutionResult:
        with self._factory() as session:
            datasets = PostgresDatasetRepository(session)
            try:
                failed = fail_artifact(
                    target=target,
                    code=code,
                    detail=detail,
                    now=datetime.now(UTC),
                    datasets=datasets,
                )
            except DatasetRefusedError:
                _logger.warning(
                    "job.dataset_artifact.lease_lost",
                    job_id=str(target.job.id),
                    error_type=type(error).__name__,
                )
                return ArtifactExecutionResult(ArtifactExecutionOutcome.LEASE_LOST)
            result = ArtifactExecutionResult(
                ArtifactExecutionOutcome.FAILED,
                failure_code=code,
            )
            if not finish_job(session, result):
                session.rollback()
                return ArtifactExecutionResult(ArtifactExecutionOutcome.LEASE_LOST)
            if not commit_transaction(session):
                return ArtifactExecutionResult(ArtifactExecutionOutcome.LEASE_LOST)
        _logger.warning(
            "job.dataset_artifact.failed",
            job_id=str(target.job.id),
            dataset_id=str(failed.dataset_id),
            artifact_id=str(failed.id),
            failure_code=code,
            error_type=type(error).__name__,
        )
        return result

    def _mark_cleanup_pending(
        self,
        *,
        target: ArtifactTarget,
        object_key: str,
        error: Exception,
    ) -> ArtifactExecutionResult:
        with self._factory() as session:
            datasets = PostgresDatasetRepository(session)
            if not mark_artifact_cleanup_pending(
                target=target,
                object_key=object_key,
                now=datetime.now(UTC),
                datasets=datasets,
            ):
                session.rollback()
                _logger.warning(
                    "job.dataset_artifact.cleanup_lease_lost",
                    job_id=str(target.job.id),
                    dataset_id=str(target.artifact.dataset_id),
                    artifact_id=str(target.artifact.id),
                    error_type=type(error).__name__,
                    result="retry_pending",
                )
                return ArtifactExecutionResult(ArtifactExecutionOutcome.LEASE_LOST)
            session.commit()
        _logger.warning(
            "job.dataset_artifact.cleanup_retry",
            job_id=str(target.job.id),
            dataset_id=str(target.artifact.dataset_id),
            artifact_id=str(target.artifact.id),
            error_type=type(error).__name__,
            result="retry_pending",
        )
        return ArtifactExecutionResult(ArtifactExecutionOutcome.RETRY_PENDING)

    def _record_orphan(
        self,
        *,
        artifact_id: UUID,
        object_key: str,
        error: Exception,
    ) -> None:
        with self._factory() as session:
            datasets = PostgresDatasetRepository(session)
            if not record_artifact_orphan_candidate(
                artifact_id=artifact_id,
                object_key=object_key,
                datasets=datasets,
            ):
                session.rollback()
                _logger.warning(
                    "job.dataset_artifact.orphan_candidate_record_failed",
                    artifact_id=str(artifact_id),
                    object_key=object_key,
                    error_type=type(error).__name__,
                    result="retry_pending",
                )
                return
            session.commit()
        _logger.warning(
            "job.dataset_artifact.orphan_candidate_recorded",
            artifact_id=str(artifact_id),
            object_key=object_key,
            error_type=type(error).__name__,
            result="retry_pending",
        )

    @staticmethod
    def _discard_candidate(*, storage: ObjectStorage, object_key: str) -> bool:
        try:
            storage.delete(object_key=object_key)
        except Exception:
            _logger.exception(
                "job.dataset_artifact.orphan_candidate",
                object_key=object_key,
            )
            return False
        return True


def _artifact_candidate_keys(
    *,
    object_key: str | None,
    manifest: Mapping[str, Any],
) -> tuple[str, ...]:
    keys = [object_key] if object_key else []
    keys.extend(
        key for key in manifest.get("orphan_candidate_keys", []) if isinstance(key, str) and key
    )
    return tuple(sorted(set(keys)))
