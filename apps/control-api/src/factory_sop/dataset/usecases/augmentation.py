"""增强命令的授权、冻结、幂等受理与派发前取消。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime
from uuid import UUID

from factory_sop.auth.api import Caller, Permission, authorize
from factory_sop.dataset.errors import DatasetRefusalCode, DatasetRefusedError
from factory_sop.dataset.model import (
    AnnotationExecutionStatus,
    AugmentationCommand,
    AugmentationStatus,
    MemberStatus,
)
from factory_sop.dataset.repository import AugmentationRepository
from factory_sop.identifiers import new_id
from factory_sop.job.api import AugmentationJobQueue
from factory_sop.observability import get_logger

_logger = get_logger("dataset")


def request_augmentation(
    *,
    dataset_id: UUID,
    execution_id: UUID,
    frame_drop_iterations: int,
    idempotency_key: str,
    caller: Caller,
    now: datetime,
    datasets: AugmentationRepository,
    jobs: AugmentationJobQueue,
) -> AugmentationCommand:
    """只接受本数据集成功标注的服务端身份；受理不等于基座成功或产物登记。"""
    authorize(caller, Permission.DATASET_EDIT)
    if not idempotency_key.strip() or len(idempotency_key) > 255:
        raise DatasetRefusedError(DatasetRefusalCode.IDEMPOTENCY_CONFLICT)
    if isinstance(frame_drop_iterations, bool) or not 0 <= frame_drop_iterations <= 10:
        raise DatasetRefusedError(DatasetRefusalCode.STATE_CONFLICT)
    existing = datasets.augmentation_by_key(dataset_id, idempotency_key)
    if existing is not None:
        return _replay(existing, execution_id, frame_drop_iterations)
    execution = datasets.annotation_execution_by_id(execution_id)
    submission = (
        None if execution is None else datasets.annotation_submission_by_id(execution.submission_id)
    )
    member = None if submission is None else datasets.member_by_id(submission.member_id)
    actions = (
        None
        if submission is None
        else datasets.action_list_by_revision(
            dataset_id=dataset_id, revision=submission.action_list_revision
        )
    )
    if (
        execution is None
        or execution.status is not AnnotationExecutionStatus.SUCCEEDED
        or not execution.upstream_data_id
        or not execution.upstream_video_id
        or not execution.derived_video_sha256
        or not execution.derived_video_size
        or not execution.clips
        or submission is None
        or submission.dataset_id != dataset_id
        or member is None
        or member.status != MemberStatus.REGISTERED
        or member.object_key != submission.source_object_key
        or member.actual_sha256 != submission.source_sha256
        or actions is None
    ):
        raise DatasetRefusedError(
            DatasetRefusalCode.ANNOTATION_STATE_CONFLICT,
            detail="增强输入必须属于本数据集的已成功标注执行，且源媒体身份一致",
        )
    command = AugmentationCommand(
        id=new_id(),
        dataset_id=dataset_id,
        annotation_execution_id=execution_id,
        label_data_id=execution.upstream_data_id,
        idempotency_key=idempotency_key,
        frame_drop_iterations=frame_drop_iterations,
        input_snapshot={
            "member_id": str(member.id),
            "source": member.source,
            "original_filename": member.original_filename,
            "source_object_key": submission.source_object_key,
            "source_sha256": submission.source_sha256,
            "annotation_submission_id": str(submission.id),
            "annotation_execution_id": str(execution.id),
            "annotation_revision": submission.revision,
            "action_list_revision": submission.action_list_revision,
            "actions": list(actions.actions),
            "mode": submission.mode.value,
            "upstream_video_id": execution.upstream_video_id,
            "derived_video_sha256": execution.derived_video_sha256,
            "derived_video_size": execution.derived_video_size,
            "clips": [dict(clip) for clip in execution.clips],
        },
        created_by=caller.user.id,
        created_at=now,
        updated_at=now,
    )
    if not datasets.add_augmentation(command):
        existing = datasets.augmentation_by_key(dataset_id, idempotency_key)
        if existing is None:
            raise DatasetRefusedError(DatasetRefusalCode.STATE_CONFLICT)
        return _replay(existing, execution_id, frame_drop_iterations)
    job = jobs.get_or_create_augmentation(dataset_id=dataset_id, command_id=command.id, now=now)
    saved = replace(command, job_id=job.id, version=1)
    if not datasets.save_augmentation(saved, expected_version=0):
        raise DatasetRefusedError(DatasetRefusalCode.STATE_CONFLICT)
    _logger.info(
        "dataset.augmentation.accepted", command_id=str(saved.id), actor_id=str(caller.user.id)
    )
    return saved


def _replay(
    existing: AugmentationCommand, execution_id: UUID, iterations: int
) -> AugmentationCommand:
    if (
        existing.annotation_execution_id != execution_id
        or existing.frame_drop_iterations != iterations
    ):
        raise DatasetRefusedError(DatasetRefusalCode.IDEMPOTENCY_CONFLICT)
    return existing


def list_augmentations(
    *, dataset_id: UUID, caller: Caller, datasets: AugmentationRepository
) -> Sequence[AugmentationCommand]:
    """按权限读回历史，不以客户端缓存替代持久记录。"""
    authorize(caller, Permission.DATASET_VIEW)
    if datasets.dataset_by_id(dataset_id) is None:
        raise DatasetRefusedError(DatasetRefusalCode.DATASET_NOT_FOUND)
    return datasets.list_augmentations(dataset_id)


def read_augmentation(
    *, dataset_id: UUID, command_id: UUID, caller: Caller, datasets: AugmentationRepository
) -> AugmentationCommand:
    """授权读取持久状态，未知/不可达不解释为已完成。"""
    authorize(caller, Permission.DATASET_VIEW)
    command = datasets.augmentation_by_id(command_id)
    if command is None or command.dataset_id != dataset_id:
        raise DatasetRefusedError(DatasetRefusalCode.RESOURCE_MISMATCH)
    return command


def cancel_augmentation(
    *,
    dataset_id: UUID,
    command_id: UUID,
    caller: Caller,
    now: datetime,
    datasets: AugmentationRepository,
) -> AugmentationCommand:
    """只取消尚未有持久派发意图的命令；不向上游发送伪取消。"""
    authorize(caller, Permission.DATASET_EDIT)
    datasets.cancel_pending_augmentation(
        dataset_id=dataset_id, command_id=command_id, actor_id=caller.user.id, now=now
    )
    command = datasets.augmentation_by_id(command_id)
    if command is None or command.dataset_id != dataset_id:
        raise DatasetRefusedError(DatasetRefusalCode.RESOURCE_MISMATCH)
    if command.status is not AugmentationStatus.CANCELLED:
        raise DatasetRefusedError(
            DatasetRefusalCode.STATE_CONFLICT, detail="已进入派发或已结束；基座没有已派发取消接口"
        )
    return command
