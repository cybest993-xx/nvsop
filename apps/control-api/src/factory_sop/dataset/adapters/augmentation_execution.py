"""dataset 拥有的增强推进器：PG 意图先于 HTTP，终态登记与 job 同事务。"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Callable
from contextlib import suppress
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryFile

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from factory_sop.auth.api import (
    AuthorizationRefusedError,
    CurrentCallerResolver,
    Permission,
    require_current_actor,
)
from factory_sop.dataset.adapters.augmentation import AugmentationVolume, HttpAugmentationBackend
from factory_sop.dataset.adapters.repository import PostgresDatasetRepository
from factory_sop.dataset.api import (
    ArtifactExecutionOutcome,
    ArtifactExecutionResult,
    ArtifactJobFinisher,
    ArtifactTransactionCommitter,
)
from factory_sop.dataset.media import MediaProbe
from factory_sop.dataset.model import AugmentationCommand, AugmentationStatus
from factory_sop.dataset.storage import ObjectStorage
from factory_sop.job.api import ApplicationJob
from factory_sop.observability import get_logger

_logger = get_logger("dataset")
_PENDING = ArtifactExecutionResult(ArtifactExecutionOutcome.RETRY_PENDING)
_TERMINAL = {
    AugmentationStatus.COMPLETED: ArtifactExecutionOutcome.SUCCEEDED,
    AugmentationStatus.FAILED: ArtifactExecutionOutcome.FAILED,
    AugmentationStatus.CANCELLED: ArtifactExecutionOutcome.SUPERSEDED,
}


class PostgresAugmentationExecutor:
    """worker 只负责任务租约；非幂等派发权由持久输入唯一约束独立保护。"""

    def __init__(
        self,
        *,
        factory: sessionmaker[Session],
        backend_url: str,
        timeout_seconds: int,
        volume_root: Path,
        storage_factory: Callable[[], ObjectStorage],
        media_probe: MediaProbe,
    ) -> None:
        self._factory = factory
        self._backend = HttpAugmentationBackend(backend_url, timeout_seconds)
        self._volume = AugmentationVolume(volume_root, media_probe)
        self._storage = storage_factory

    def execute(
        self,
        *,
        job: ApplicationJob,
        finish_job: ArtifactJobFinisher,
        commit_transaction: ArtifactTransactionCommitter,
        resolve_current_caller: CurrentCallerResolver,
    ) -> ArtifactExecutionResult:
        with self._factory() as session:
            command = PostgresDatasetRepository(session).augmentation_by_id(job.attempt_id)
        if command is None or command.job_id != job.id or command.dataset_id != job.dataset_id:
            result = ArtifactExecutionResult(ArtifactExecutionOutcome.FAILED, "RESOURCE_MISMATCH")
            with self._factory() as session:
                if finish_job(session, result):
                    commit_transaction(session)
            return result
        if command.status in _TERMINAL:
            return self._publish(command, command, finish_job, commit_transaction)
        if command.status is AugmentationStatus.PENDING:
            try:
                require_current_actor(
                    actor_id=command.created_by,
                    permissions=(Permission.DATASET_EDIT,),
                    resolve_current_caller=resolve_current_caller,
                )
                self._verify_source(command)
                input_files = self._volume.verify_input(command)
                require_current_actor(
                    actor_id=command.created_by,
                    permissions=(Permission.DATASET_EDIT,),
                    resolve_current_caller=resolve_current_caller,
                )
            except Exception as error:
                code = (
                    "PERMISSION_REVOKED"
                    if isinstance(error, AuthorizationRefusedError)
                    else "AUGMENTATION_INPUT_UNAVAILABLE"
                )
                failed = replace(command, status=AugmentationStatus.FAILED, failure_code=code)
                return self._publish(command, failed, finish_job, commit_transaction)
            with self._factory() as session:
                repository = PostgresDatasetRepository(session)
                begun = repository.begin_augmentation_dispatch(
                    command_id=command.id,
                    backend_target=self._backend.target,
                    now=datetime.now(UTC),
                    input_files=input_files,
                )
                if begun is None or not commit_transaction(session):
                    return _PENDING
            command = begun
            # 从此以后，即使超时/ARQ 恢复/崩溃，也不能重发同一输入的非幂等 POST。
            try:
                identity = self._backend.start(command)
                if not re.fullmatch(re.escape(command.label_data_id) + r"_augmented_\d+", identity):
                    raise ValueError("基座响应身份与冻结输入不匹配")
                bound = replace(
                    command,
                    status=AugmentationStatus.BOUND,
                    base_augmentation_id=identity,
                    result_manifest={"input_files": input_files},
                    failure_code=None,
                )
                # 绑定是已发生的外部事实，独立于可能已过期的 worker 发布权保存。
                self._save_fact(command, bound)
            except Exception as error:
                self._diagnostic(command, "DISPATCH_RESULT_UNVERIFIED", error)
            return _PENDING
        if command.status is AugmentationStatus.RESULT_UNKNOWN:
            # 完成列表缺失不能证明未开始；没有可安全关联的响应身份时持续待核实。
            return _PENDING
        if command.backend_target != self._backend.target:
            self._diagnostic(command, "AUGMENTATION_TARGET_CHANGED")
            return _PENDING
        assert command.base_augmentation_id is not None
        try:
            observation = self._backend.status(command.base_augmentation_id)
            progress = observation.get("progress")
            if (
                observation.get("dataset_id") != command.base_augmentation_id
                or observation.get("status") not in {"pending", "running", "completed", "failed"}
                or isinstance(progress, bool)
                or not isinstance(progress, (int, float))
                or not math.isfinite(progress)
                or not 0 <= progress <= 100
            ):
                raise ValueError("增强观测身份或状态不可信")
        except Exception as error:
            self._diagnostic(command, "AUGMENTATION_OBSERVATION_UNAVAILABLE", error)
            return _PENDING
        observed = self._save_fact(
            command,
            replace(
                command,
                base_observation=dict(observation),
                last_observed_at=datetime.now(UTC),
                failure_code=None,
            ),
        )
        if observed is None:
            return _PENDING
        command = observed
        if observation["status"] == "failed":
            return self._publish(
                command,
                replace(
                    command,
                    status=AugmentationStatus.FAILED,
                    failure_code="AUGMENTATION_BASE_FAILED",
                ),
                finish_job,
                commit_transaction,
            )
        if observation["status"] != "completed":
            return _PENDING
        # completed 只证明基座结束，真实文件与中心登记仍需分别完成。
        try:
            files = self._volume.prepare_result(command)
            if "files" not in command.result_manifest:
                candidate = self._save_fact(
                    command,
                    replace(
                        command,
                        result_manifest={**command.result_manifest, "files": files},
                        result_failure_code=None,
                    ),
                )
                if candidate is None:
                    return _PENDING
                command = candidate
            self._volume.copy_result(command, files, self._storage())
        except Exception as error:
            self._diagnostic(command, "AUGMENTATION_REGISTRATION_FAILED", error, registration=True)
            return _PENDING
        return self._publish(
            command,
            replace(
                command,
                status=AugmentationStatus.COMPLETED,
                result_status="registered",
                result_failure_code=None,
            ),
            finish_job,
            commit_transaction,
        )

    def _verify_source(self, command: AugmentationCommand) -> None:
        storage = self._storage()
        with TemporaryFile() as source:
            storage.download_to(
                object_key=command.input_snapshot["source_object_key"], destination=source
            )
            source.seek(0)
            if (
                hashlib.file_digest(source, "sha256").hexdigest()
                != command.input_snapshot["source_sha256"]
            ):
                raise ValueError("原始来源媒体与冻结身份不一致")

    def _save_fact(
        self, previous: AugmentationCommand, value: AugmentationCommand
    ) -> AugmentationCommand | None:
        value = replace(value, version=previous.version + 1, updated_at=datetime.now(UTC))
        with self._factory.begin() as session:
            saved = PostgresDatasetRepository(session).save_augmentation(
                value, expected_version=previous.version
            )
        return value if saved else None

    def _diagnostic(
        self,
        command: AugmentationCommand,
        code: str,
        error: Exception | None = None,
        *,
        registration: bool = False,
    ) -> None:
        value = (
            replace(command, result_failure_code=code)
            if registration
            else replace(command, failure_code=code)
        )
        # 身份冲突不允许改绑；原先的持久在途占用继续保留。
        with suppress(IntegrityError):
            self._save_fact(command, value)
        _logger.warning(
            "dataset.augmentation.pending_verification",
            command_id=str(command.id),
            failure_code=code,
            error_type=type(error).__name__ if error else None,
        )

    def _publish(
        self,
        previous: AugmentationCommand,
        value: AugmentationCommand,
        finish_job: ArtifactJobFinisher,
        commit_transaction: ArtifactTransactionCommitter,
    ) -> ArtifactExecutionResult:
        result = ArtifactExecutionResult(_TERMINAL[value.status], value.failure_code)
        with self._factory() as session:
            if previous.status not in _TERMINAL:
                value = replace(value, version=previous.version + 1, updated_at=datetime.now(UTC))
                if not PostgresDatasetRepository(session).save_augmentation(
                    value, expected_version=previous.version
                ):
                    return ArtifactExecutionResult(ArtifactExecutionOutcome.LEASE_LOST)
            if not finish_job(session, result) or not commit_transaction(session):
                session.rollback()
                return ArtifactExecutionResult(ArtifactExecutionOutcome.LEASE_LOST)
        _logger.info(
            "dataset.augmentation.finished",
            command_id=str(value.id),
            status=value.status.value,
            base_augmentation_id=value.base_augmentation_id,
            result_status=value.result_status,
        )
        return result
