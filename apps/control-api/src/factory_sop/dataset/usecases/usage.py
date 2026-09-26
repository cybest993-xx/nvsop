"""训练数据 DDM/VLM 用途检查与不可变制品用例。

持久化的 `input_snapshot` / `media` 是冻结且参与摘要计算的输入事实，其中仍然沿用历史
键名 `object_version_id` / `source_object_version_id`；这些键保存的是定稿文件键，不是
对象存储代次。重命名它们会改变已存快照的读取格式与摘要，故只在领域/公开契约层改名。
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import shutil
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from typing import Any, assert_never
from uuid import UUID

from factory_sop.auth.api import Caller, Permission, authorize
from factory_sop.dataset.annotation import (
    AnnotationDataVolume,
    AnnotationDataVolumeInputError,
    AnnotationDataVolumeReadError,
    AnnotationDataVolumeUnavailableError,
)
from factory_sop.dataset.errors import DatasetRefusalCode, DatasetRefusedError
from factory_sop.dataset.media import (
    InvalidMediaError,
    MediaMetadata,
    MediaProbe,
    MediaProbeUnavailableError,
)
from factory_sop.dataset.model import (
    AnnotationExecution,
    AnnotationExecutionStatus,
    AnnotationMode,
    ArtifactStatus,
    DatasetArtifact,
    DdmVideoInput,
    MemberStatus,
    UsageCheck,
    UsageCheckStatus,
    UsageKind,
    VlmCandidate,
    VlmCandidateKind,
    VlmMediaReference,
)
from factory_sop.dataset.repository import UsageDatasetRepository as DatasetRepository
from factory_sop.dataset.storage import (
    ObjectNotFoundError,
    ObjectStorage,
    ObjectStorageUnavailableError,
)
from factory_sop.dataset.usage import (
    DdmReaderInputError,
    DdmReaderUnavailableError,
    DdmSampleClassEmptyError,
    UsageIssue,
    UsageValidationResult,
    VlmReaderInputError,
    VlmReaderUnavailableError,
    canonical_json,
    safe_media_key,
    validate_ddm_input,
    validate_vlm_input,
)
from factory_sop.identifiers import new_id
from factory_sop.job.api import ApplicationJob, UsageJobQueue
from factory_sop.observability import get_logger

BASE_COMMIT = "69352021c2aae0ba071acd2629f5cff224d14ca6"  # pragma: allowlist secret
DDM_CONTRACT_VERSION = "ddm-v1"
VLM_CONTRACT_VERSION = "vlm-v1"
DDM_CONSUMER_PARAMETERS: Mapping[str, object] = {
    "mode": "train",
    "num_classes": 2,
    "frames_per_side": 5,
    "downsample": 1,
    "min_change_dur": 0.3,
    "video_backend": "pyav",
}
_EPSILON = 1e-6
_logger = get_logger("dataset")


def _contract_version(kind: UsageKind) -> str:
    """返回用途对应的输入契约版本；新增用途必须显式接入。"""
    match kind:
        case UsageKind.DDM:
            return DDM_CONTRACT_VERSION
        case UsageKind.VLM:
            return VLM_CONTRACT_VERSION
        case _:
            assert_never(kind)


def _log_artifact_requested(*, artifact: DatasetArtifact, actor_id: UUID, result: str) -> None:
    _logger.info(
        "dataset.artifact.requested",
        dataset_id=str(artifact.dataset_id),
        usage_kind=artifact.kind.value,
        artifact_id=str(artifact.id),
        usage_check_id=str(artifact.usage_check_id),
        input_digest=artifact.input_digest,
        actor_id=str(actor_id),
        result=result,
    )


def _candidate_id_for_kind(kind: UsageKind, candidate_id: UUID | None) -> UUID | None:
    """只为 VLM 检查保留候选身份。"""
    match kind:
        case UsageKind.DDM:
            return None
        case UsageKind.VLM:
            return candidate_id
        case _:
            assert_never(kind)


@dataclass(frozen=True, slots=True)
class UsageCheckRequestResult:
    """发起用途检查返回的检查记录和异步任务。"""

    check: UsageCheck
    job: ApplicationJob


@dataclass(frozen=True, slots=True)
class ArtifactRequestResult:
    """发起制品生成返回的制品记录和异步任务。"""

    artifact: DatasetArtifact
    job: ApplicationJob


@dataclass(frozen=True, slots=True)
class UsageCheckTarget:
    """worker 领取的用途检查冻结输入。"""

    job: ApplicationJob
    check: UsageCheck


@dataclass(frozen=True, slots=True)
class ArtifactTarget:
    """worker 领取的制品生成冻结输入。"""

    job: ApplicationJob
    artifact: DatasetArtifact
    check: UsageCheck


def register_vlm_candidate(
    *,
    dataset_id: UUID,
    kind: VlmCandidateKind,
    action_list_revision: int,
    records: Sequence[Mapping[str, Any]],
    media: Sequence[VlmMediaReference],
    expected_revision: int,
    caller: Caller,
    now: datetime,
    datasets: DatasetRepository,
) -> VlmCandidate:
    """追加 VLM 候选修订；媒体身份只接受已登记资源。"""
    authorize(caller, Permission.DATASET_EDIT)
    _require_dataset(dataset_id, datasets)
    if expected_revision < 0 or isinstance(expected_revision, bool):
        raise DatasetRefusedError(
            DatasetRefusalCode.STALE_REVISION,
            detail="VLM 候选修订号无效",
        )
    action_list = datasets.action_list_by_revision(
        dataset_id=dataset_id,
        revision=action_list_revision,
    )
    if action_list is None:
        raise DatasetRefusedError(DatasetRefusalCode.ACTION_LIST_NOT_FOUND)
    if any(not isinstance(item, Mapping) for item in records):
        raise DatasetRefusedError(
            DatasetRefusalCode.VLM_CANDIDATE_INVALID,
            detail="VLM 候选记录必须是 JSON 对象",
        )
    try:
        canonical_json(records)
    except (TypeError, ValueError) as error:
        raise DatasetRefusedError(
            DatasetRefusalCode.VLM_CANDIDATE_INVALID,
            detail="VLM 候选记录必须是有限 JSON 值",
        ) from error
    members = {member.id: member for member in datasets.list_members(dataset_id=dataset_id)}
    media_basenames: set[str] = set()
    for index, item in enumerate(media):
        if not safe_media_key(item.key):
            raise DatasetRefusedError(
                DatasetRefusalCode.VLM_CANDIDATE_INVALID,
                detail=f"媒体映射 {index} 不是安全的相对键",
            )
        basename = PurePosixPath(item.key).name.casefold()
        if basename in media_basenames:
            raise DatasetRefusedError(
                DatasetRefusalCode.VLM_CANDIDATE_INVALID,
                detail="媒体映射不能共享同一 basename",
            )
        media_basenames.add(basename)
        if item.member_id not in members:
            raise DatasetRefusedError(
                DatasetRefusalCode.RESOURCE_MISMATCH,
                detail="VLM 媒体必须属于当前数据集",
            )
        member = members[item.member_id]
        if any(
            isinstance(action_index, bool)
            or not isinstance(action_index, int)
            or not 1 <= action_index <= len(action_list.actions)
            for action_index in item.action_indices
        ):
            raise DatasetRefusedError(
                DatasetRefusalCode.VLM_CANDIDATE_INVALID,
                detail="VLM 媒体动作编号必须属于指定动作列表修订",
            )
        if (
            member.status != MemberStatus.REGISTERED
            or member.object_key != item.source_object_key
            or member.actual_sha256 != item.source_sha256
        ):
            raise DatasetRefusedError(
                DatasetRefusalCode.VLM_CANDIDATE_INVALID,
                detail="VLM 媒体必须绑定当前已确认的视频对象",
            )
        submission = (
            datasets.annotation_submission_by_id(item.annotation_submission_id)
            if item.annotation_submission_id is not None
            else None
        )
        if item.annotation_execution_id is not None and item.annotation_submission_id is None:
            raise DatasetRefusedError(
                DatasetRefusalCode.VLM_CANDIDATE_INVALID,
                detail="VLM 标注切片必须同时绑定标注提交和成功执行",
            )
        latest_submission = (
            datasets.latest_annotation_submission(member_id=item.member_id)
            if submission is not None
            else None
        )
        if item.annotation_submission_id is not None and (
            submission is None
            or latest_submission is None
            or latest_submission.id != submission.id
            or submission.dataset_id != dataset_id
            or submission.member_id != item.member_id
            or submission.source_object_key != item.source_object_key
            or submission.source_sha256 != item.source_sha256
            or submission.action_list_revision != action_list_revision
        ):
            raise DatasetRefusedError(
                DatasetRefusalCode.VLM_CANDIDATE_INVALID,
                detail="VLM 媒体引用的标注提交不属于当前视频",
            )
        execution = (
            datasets.annotation_execution_by_id(item.annotation_execution_id)
            if item.annotation_execution_id is not None
            else None
        )
        execution_submission = (
            datasets.annotation_submission_by_id(execution.submission_id)
            if execution is not None
            else None
        )
        latest_execution = (
            datasets.latest_annotation_execution(execution_submission.id)
            if execution_submission is not None
            else None
        )
        if item.annotation_execution_id is not None and (
            latest_submission is None
            or execution is None
            or execution_submission is None
            or latest_execution is None
            or latest_execution.id != execution.id
            or execution_submission.dataset_id != dataset_id
            or execution_submission.member_id != item.member_id
            or execution_submission.source_object_key != item.source_object_key
            or execution_submission.source_sha256 != item.source_sha256
            or execution_submission.action_list_revision != action_list_revision
            or execution.status is not AnnotationExecutionStatus.SUCCEEDED
            or (submission is not None and execution.submission_id != submission.id)
            or item.clip_index is None
            or isinstance(item.clip_index, bool)
            or item.clip_index < 0
            or item.clip_index >= len(execution.clips)
        ):
            raise DatasetRefusedError(
                DatasetRefusalCode.VLM_CANDIDATE_INVALID,
                detail="VLM 媒体引用的标注执行代次或片段不存在",
            )
        if item.clip_index is not None and item.annotation_execution_id is None:
            raise DatasetRefusedError(
                DatasetRefusalCode.VLM_CANDIDATE_INVALID,
                detail="VLM 片段索引必须绑定标注执行代次",
            )
        if item.annotation_execution_id is not None and item.action_indices:
            if execution is None or item.clip_index is None:
                raise DatasetRefusedError(
                    DatasetRefusalCode.VLM_CANDIDATE_INVALID,
                    detail="VLM 片段执行身份不完整",
                )
            clip = execution.clips[item.clip_index]
            if not isinstance(clip, dict):
                raise DatasetRefusedError(
                    DatasetRefusalCode.VLM_CANDIDATE_INVALID,
                    detail="VLM 片段动作输出不是对象",
                )
            raw_indices = clip.get("action_indices")
            if raw_indices is None and clip.get("action_index") is not None:
                raw_indices = [clip["action_index"]]
            if not isinstance(raw_indices, list) or {
                int(index) + 1
                for index in raw_indices
                if isinstance(index, int) and not isinstance(index, bool)
            } != set(item.action_indices):
                raise DatasetRefusedError(
                    DatasetRefusalCode.VLM_CANDIDATE_INVALID,
                    detail="VLM 片段动作编号与成功标注输出不一致",
                )
            raw_descriptions = clip.get("action_descriptions")
            if raw_descriptions is None and clip.get("action_description") is not None:
                raw_descriptions = [clip["action_description"]]
            if raw_descriptions is not None:
                if not isinstance(raw_descriptions, list) or any(
                    not isinstance(description, str) for description in raw_descriptions
                ):
                    raise DatasetRefusedError(
                        DatasetRefusalCode.VLM_CANDIDATE_INVALID,
                        detail="VLM 片段动作描述输出无效",
                    )
                expected_descriptions = [
                    action_list.actions[action_index - 1] for action_index in item.action_indices
                ]
                if tuple(raw_descriptions) != tuple(expected_descriptions):
                    raise DatasetRefusedError(
                        DatasetRefusalCode.VLM_CANDIDATE_INVALID,
                        detail="VLM 片段动作描述与指定动作列表修订不一致",
                    )
    if len({item.key for item in media}) != len(media):
        raise DatasetRefusedError(
            DatasetRefusalCode.VLM_CANDIDATE_INVALID,
            detail="VLM 媒体映射键不能重复",
        )
    latest = datasets.latest_vlm_candidate(dataset_id)
    current_revision = latest.revision if latest is not None else 0
    if expected_revision != current_revision:
        raise DatasetRefusedError(
            DatasetRefusalCode.STALE_REVISION,
            detail="VLM 候选已被其他用户修改，请重新读取后再提交",
        )
    candidate = VlmCandidate(
        id=new_id(),
        dataset_id=dataset_id,
        revision=current_revision + 1,
        kind=kind,
        action_list_revision=action_list_revision,
        records=tuple(dict(item) for item in records),
        media=tuple(media),
        created_by=caller.user.id,
        created_at=now,
    )
    datasets.add_vlm_candidate(candidate)
    stored = datasets.latest_vlm_candidate(dataset_id)
    if stored is None or stored.id != candidate.id:
        raise DatasetRefusedError(
            DatasetRefusalCode.STALE_REVISION,
            detail="VLM 候选已被其他用户修改，请重新读取后再提交",
        )
    return candidate


def list_vlm_candidates(
    *, dataset_id: UUID, caller: Caller, datasets: DatasetRepository
) -> Sequence[VlmCandidate]:
    """读取 VLM 候选历史。"""
    authorize(caller, Permission.DATASET_VIEW)
    _require_dataset(dataset_id, datasets)
    return datasets.list_vlm_candidates(dataset_id)


def read_vlm_candidate(
    *, dataset_id: UUID, candidate_id: UUID, caller: Caller, datasets: DatasetRepository
) -> VlmCandidate:
    """读取并核对 VLM 候选归属。"""
    authorize(caller, Permission.DATASET_VIEW)
    _require_dataset(dataset_id, datasets)
    candidate = datasets.vlm_candidate_by_id(candidate_id)
    if candidate is None:
        raise DatasetRefusedError(DatasetRefusalCode.VLM_CANDIDATE_NOT_FOUND)
    if candidate.dataset_id != dataset_id:
        raise DatasetRefusedError(DatasetRefusalCode.RESOURCE_MISMATCH)
    return candidate


def request_usage_check(
    *,
    dataset_id: UUID,
    kind: UsageKind,
    candidate_id: UUID | None,
    caller: Caller,
    now: datetime,
    datasets: DatasetRepository,
    jobs: UsageJobQueue,
) -> UsageCheckRequestResult:
    """冻结当前用途输入并创建异步检查任务。"""
    authorize(caller, Permission.DATASET_EDIT)
    _require_dataset(dataset_id, datasets)
    match kind:
        case UsageKind.DDM:
            validation, snapshot = _freeze_ddm(dataset_id=dataset_id, datasets=datasets)
        case UsageKind.VLM:
            validation, snapshot = _freeze_vlm(
                dataset_id=dataset_id,
                datasets=datasets,
                candidate_id=candidate_id,
            )
        case _:
            assert_never(kind)
    check = UsageCheck(
        id=new_id(),
        dataset_id=dataset_id,
        kind=kind,
        status=UsageCheckStatus.PENDING,
        input_digest=_digest_snapshot(snapshot),
        input_snapshot=snapshot,
        summary=validation.summary,
        issues=(),
        base_commit=BASE_COMMIT,
        contract_version=_contract_version(kind),
        candidate_id=_candidate_id_for_kind(kind, candidate_id),
        job_id=None,
        created_by=caller.user.id,
        created_at=now,
        updated_at=now,
    )
    datasets.add_usage_check(check)
    job = jobs.get_or_create_usage_check(dataset_id=dataset_id, check_id=check.id, now=now)
    check = replace(check, job_id=job.id)
    if not datasets.save_usage_check(check, expected_updated_at=now):
        raise DatasetRefusedError(
            DatasetRefusalCode.USAGE_STATE_CONFLICT,
            detail="用途检查任务保存失败",
        )
    _logger.info(
        "dataset.usage_check.requested",
        dataset_id=str(dataset_id),
        usage_kind=kind.value,
        check_id=str(check.id),
        candidate_id=str(check.candidate_id) if check.candidate_id is not None else None,
        actor_id=str(caller.user.id),
        input_digest=check.input_digest,
        result=check.status.value,
    )
    return UsageCheckRequestResult(check=check, job=job)


def _freeze_vlm(
    *, dataset_id: UUID, datasets: DatasetRepository, candidate_id: UUID | None
) -> tuple[UsageValidationResult, dict[str, Any]]:
    """冻结 VLM 候选、动作列表和每条媒体的当前事实。"""
    candidate = _candidate_for_check(dataset_id, candidate_id, datasets)
    actions = datasets.action_list_by_revision(
        dataset_id=dataset_id,
        revision=candidate.action_list_revision,
    )
    if actions is None:
        raise DatasetRefusedError(DatasetRefusalCode.ACTION_LIST_NOT_FOUND)
    registered = {member.id for member in datasets.list_members(dataset_id=dataset_id)}
    validation = validate_vlm_input(
        candidate,
        actions=actions.actions,
        registered_member_ids=registered,
    )
    snapshot = dict(validation.input_snapshot)
    snapshot["dataset_id"] = str(dataset_id)
    snapshot["candidate_id"] = str(candidate.id)
    snapshot["candidate_kind"] = candidate.kind.value
    snapshot["actions"] = list(actions.actions)
    snapshot["base_commit"] = BASE_COMMIT
    snapshot["contract_version"] = VLM_CONTRACT_VERSION
    scope: list[dict[str, Any]] = []
    for item in candidate.media:
        member = datasets.member_by_id(item.member_id)
        execution = (
            datasets.annotation_execution_by_id(item.annotation_execution_id)
            if item.annotation_execution_id is not None
            else None
        )
        clip = (
            execution.clips[item.clip_index]
            if execution is not None
            and item.clip_index is not None
            and 0 <= item.clip_index < len(execution.clips)
            else None
        )
        scope.append(
            {
                "key": item.key,
                "member_id": str(item.member_id),
                "status": MemberStatus.REGISTERED.value,
                "object_key": member.object_key if member is not None else None,
                "object_version_id": item.source_object_key,
                "source_sha256": item.source_sha256,
                "actual_size": member.actual_size if member is not None else None,
                "duration_seconds": member.duration_seconds if member is not None else None,
                "codec": member.codec if member is not None else None,
                "container": member.container if member is not None else None,
                "annotation_submission_id": (
                    str(item.annotation_submission_id)
                    if item.annotation_submission_id is not None
                    else None
                ),
                "annotation_execution_id": (
                    str(item.annotation_execution_id)
                    if item.annotation_execution_id is not None
                    else None
                ),
                "clip_index": item.clip_index,
                "upstream_data_id": execution.upstream_data_id if execution else None,
                "upstream_video_id": execution.upstream_video_id if execution else None,
                "derived_video_size": execution.derived_video_size if execution else None,
                "derived_video_sha256": execution.derived_video_sha256 if execution else None,
                "derived_video_duration_seconds": (
                    execution.derived_video_duration_seconds if execution else None
                ),
                "clip_filename": (
                    str(clip["filename"])
                    if isinstance(clip, dict) and isinstance(clip.get("filename"), str)
                    else None
                ),
                "clip_start_time": (clip.get("start_time") if isinstance(clip, dict) else None),
                "clip_end_time": (clip.get("end_time") if isinstance(clip, dict) else None),
            }
        )
    for raw_media in snapshot.get("media", []):
        if not isinstance(raw_media, dict):
            continue
        matched = next(
            (item for item in scope if item.get("key") == raw_media.get("key")),
            None,
        )
        if matched is not None:
            for field in (
                "upstream_data_id",
                "upstream_video_id",
                "derived_video_size",
                "derived_video_sha256",
                "derived_video_duration_seconds",
                "clip_filename",
                "clip_start_time",
                "clip_end_time",
            ):
                raw_media[field] = matched.get(field)
    snapshot["scope"] = scope
    return validation, snapshot


def read_usage_check(
    *, dataset_id: UUID, check_id: UUID, caller: Caller, datasets: DatasetRepository
) -> UsageCheck:
    """读取并核对用途检查归属。"""
    authorize(caller, Permission.DATASET_VIEW)
    _require_dataset(dataset_id, datasets)
    check = datasets.usage_check_by_id(check_id)
    if check is None:
        raise DatasetRefusedError(DatasetRefusalCode.USAGE_CHECK_NOT_FOUND)
    if check.dataset_id != dataset_id:
        raise DatasetRefusedError(DatasetRefusalCode.RESOURCE_MISMATCH)
    return check


def list_usage_checks(
    *, dataset_id: UUID, caller: Caller, datasets: DatasetRepository
) -> Sequence[UsageCheck]:
    """读取用途检查历史。"""
    authorize(caller, Permission.DATASET_VIEW)
    _require_dataset(dataset_id, datasets)
    return datasets.list_usage_checks(dataset_id)


def request_artifact(
    *,
    dataset_id: UUID,
    check_id: UUID,
    caller: Caller,
    now: datetime,
    datasets: DatasetRepository,
    jobs: UsageJobQueue,
) -> ArtifactRequestResult:
    """从当前通过的 DDM 检查创建不可覆盖制品任务。"""
    authorize(caller, Permission.DATASET_EDIT)
    _require_dataset(dataset_id, datasets)
    check = datasets.usage_check_by_id(check_id)
    if check is None:
        raise DatasetRefusedError(DatasetRefusalCode.USAGE_CHECK_NOT_FOUND)
    if check.dataset_id != dataset_id:
        raise DatasetRefusedError(DatasetRefusalCode.RESOURCE_MISMATCH)
    match check.kind:
        case UsageKind.DDM:
            pass
        case UsageKind.VLM:
            raise DatasetRefusedError(
                DatasetRefusalCode.USAGE_STATE_CONFLICT,
                detail="只有当前输入仍适用的 DDM 检查可以生成 annotation 制品",
            )
        case _:
            assert_never(check.kind)
    if check.status is not UsageCheckStatus.PASSED or not usage_check_is_current(
        check=check, datasets=datasets
    ):
        raise DatasetRefusedError(
            DatasetRefusalCode.USAGE_STATE_CONFLICT,
            detail="只有当前输入仍适用的 DDM 检查可以生成 annotation 制品",
        )
    existing = datasets.artifact_by_input(
        dataset_id=dataset_id,
        input_digest=check.input_digest,
    )
    if existing is not None:
        job = jobs.get_or_create_artifact(
            dataset_id=dataset_id,
            artifact_id=existing.id,
            now=now,
        )
        match existing.status:
            case ArtifactStatus.AVAILABLE:
                _log_artifact_requested(
                    artifact=existing, actor_id=caller.user.id, result=existing.status.value
                )
                return ArtifactRequestResult(existing, job)
            case ArtifactStatus.PENDING | ArtifactStatus.RUNNING:
                if existing.job_id is None:
                    raise DatasetRefusedError(DatasetRefusalCode.USAGE_STATE_CONFLICT)
                _log_artifact_requested(
                    artifact=existing, actor_id=caller.user.id, result=existing.status.value
                )
                return ArtifactRequestResult(existing, job)
            case ArtifactStatus.FAILED | ArtifactStatus.NOT_GENERATED:
                pending = replace(
                    existing,
                    status=ArtifactStatus.PENDING,
                    failure_code=None,
                    failure_detail=None,
                    retryable=False,
                    recovery_action=None,
                    job_id=job.id,
                    updated_at=now,
                )
                if not datasets.save_artifact(pending, expected_updated_at=existing.updated_at):
                    raise DatasetRefusedError(DatasetRefusalCode.USAGE_STATE_CONFLICT)
                _log_artifact_requested(
                    artifact=pending, actor_id=caller.user.id, result=pending.status.value
                )
                return ArtifactRequestResult(pending, job)
            case _:
                assert_never(existing.status)
    artifact = DatasetArtifact(
        id=new_id(),
        dataset_id=dataset_id,
        usage_check_id=check.id,
        kind=UsageKind.DDM,
        status=ArtifactStatus.PENDING,
        input_digest=check.input_digest,
        object_key=None,
        artifact_sha256=None,
        artifact_size=None,
        manifest={
            "input_digest": check.input_digest,
            "base_commit": check.base_commit,
            "contract_version": check.contract_version,
        },
        failure_code=None,
        failure_detail=None,
        job_id=None,
        created_by=caller.user.id,
        created_at=now,
        updated_at=now,
    )
    datasets.add_artifact(artifact)
    stored = datasets.artifact_by_input(dataset_id=dataset_id, input_digest=check.input_digest)
    if stored is not None and stored.id != artifact.id:
        job = jobs.get_or_create_artifact(dataset_id=dataset_id, artifact_id=stored.id, now=now)
        _log_artifact_requested(
            artifact=stored, actor_id=caller.user.id, result=stored.status.value
        )
        return ArtifactRequestResult(stored, job)
    job = jobs.get_or_create_artifact(dataset_id=dataset_id, artifact_id=artifact.id, now=now)
    artifact = replace(artifact, job_id=job.id)
    if not datasets.save_artifact(artifact, expected_updated_at=now):
        raise DatasetRefusedError(DatasetRefusalCode.USAGE_STATE_CONFLICT)
    _log_artifact_requested(
        artifact=artifact, actor_id=caller.user.id, result=artifact.status.value
    )
    return ArtifactRequestResult(artifact=artifact, job=job)


def read_artifact(
    *, dataset_id: UUID, artifact_id: UUID, caller: Caller, datasets: DatasetRepository
) -> DatasetArtifact:
    """读取并核对制品归属。"""
    authorize(caller, Permission.DATASET_VIEW)
    _require_dataset(dataset_id, datasets)
    artifact = datasets.artifact_by_id(artifact_id)
    if artifact is None:
        raise DatasetRefusedError(DatasetRefusalCode.ARTIFACT_NOT_FOUND)
    if artifact.dataset_id != dataset_id:
        raise DatasetRefusedError(DatasetRefusalCode.RESOURCE_MISMATCH)
    return artifact


def list_artifacts(
    *, dataset_id: UUID, caller: Caller, datasets: DatasetRepository
) -> Sequence[DatasetArtifact]:
    """读取制品历史。"""
    authorize(caller, Permission.DATASET_VIEW)
    _require_dataset(dataset_id, datasets)
    return datasets.list_artifacts(dataset_id)


def begin_usage_check(
    *, job: ApplicationJob, datasets: DatasetRepository, now: datetime
) -> UsageCheckTarget | None:
    """领取用途检查执行租约。"""
    check = datasets.usage_check_by_id(job.attempt_id)
    if (
        check is None
        or check.dataset_id != (job.dataset_id or job.member_id)
        or check.job_id != job.id
        or check.status not in {UsageCheckStatus.PENDING, UsageCheckStatus.RUNNING}
    ):
        return None
    running = replace(check, status=UsageCheckStatus.RUNNING, updated_at=now)
    if not datasets.save_usage_check(running, expected_updated_at=check.updated_at):
        return None
    return UsageCheckTarget(job=job, check=running)


def run_usage_check(
    *,
    target: UsageCheckTarget,
    storage: ObjectStorage,
    media_probe: MediaProbe,
    annotation_volume: AnnotationDataVolume | None,
    ddm_reader: object | None = None,
    vlm_reader: object | None = None,
) -> UsageValidationResult:
    """在事务外复核冻结输入和当前源对象事实。"""
    snapshot = target.check.input_snapshot
    match target.check.kind:
        case UsageKind.DDM:
            validation = _validate_ddm_snapshot(snapshot)
            return _merge_storage_facts(
                kind=target.check.kind,
                validation=validation,
                snapshot=snapshot,
                storage=storage,
                media_probe=media_probe,
                annotation_volume=annotation_volume,
                ddm_reader=ddm_reader,
                vlm_reader=vlm_reader,
            )
        case UsageKind.VLM:
            pass
        case _:
            assert_never(target.check.kind)
    candidate = _candidate_from_snapshot(snapshot, target.check)
    raw_actions = snapshot.get("actions")
    actions = (
        tuple(str(item) for item in raw_actions)
        if isinstance(raw_actions, list) and all(isinstance(item, str) for item in raw_actions)
        else None
    )
    if actions is None:
        return _failed_result(
            snapshot=snapshot,
            summary={"record_count": len(candidate.records), "media_count": len(candidate.media)},
            issue=UsageIssue("VLM_ACTION_LIST_NOT_FOUND", "冻结输入没有动作列表", "candidate"),
        )
    registered_member_ids: set[UUID] = set()
    for item in snapshot.get("media", []):
        if not isinstance(item, dict) or item.get("member_id") is None:
            continue
        try:
            registered_member_ids.add(UUID(str(item["member_id"])))
        except (TypeError, ValueError):
            continue
    result = validate_vlm_input(
        candidate,
        actions=actions,
        registered_member_ids=registered_member_ids,
    )
    return _merge_storage_facts(
        kind=target.check.kind,
        validation=result,
        snapshot=snapshot,
        storage=storage,
        media_probe=media_probe,
        annotation_volume=annotation_volume,
        ddm_reader=ddm_reader,
        vlm_reader=vlm_reader,
    )


def complete_usage_check(
    *,
    target: UsageCheckTarget,
    validation: UsageValidationResult,
    now: datetime,
    datasets: DatasetRepository,
) -> UsageCheck:
    """按 worker 租约记录完整用途结果。"""
    issues = tuple(_issue_dict(item) for item in validation.issues)
    value = replace(
        target.check,
        status=UsageCheckStatus.PASSED if validation.passed else UsageCheckStatus.FAILED,
        input_digest=validation.input_digest,
        input_snapshot=dict(validation.input_snapshot),
        summary=validation.summary,
        issues=issues,
        updated_at=now,
    )
    if not datasets.save_usage_check(value, expected_updated_at=target.check.updated_at):
        raise DatasetRefusedError(
            DatasetRefusalCode.USAGE_STATE_CONFLICT,
            detail="用途检查执行租约已失效",
        )
    _logger.info(
        "dataset.usage_check.completed",
        dataset_id=str(value.dataset_id),
        usage_kind=value.kind.value,
        check_id=str(value.id),
        input_digest=value.input_digest,
        actor_id=str(value.created_by),
        result=value.status.value,
    )
    return value


def fail_usage_check(
    *, target: UsageCheckTarget, code: str, detail: str, now: datetime, datasets: DatasetRepository
) -> UsageCheck:
    """把基础设施失败与输入不通过分开记录。"""
    retryable = code in {
        "USAGE_CHECK_DATABASE_FAILURE",
        "USAGE_STORAGE_UNAVAILABLE",
        "USAGE_ANNOTATION_VOLUME_UNAVAILABLE",
        "USAGE_MEDIA_PROBE_UNAVAILABLE",
    }
    recovery_action = (
        "retry_usage_check"
        if retryable
        else None
        if code == "USAGE_CHECK_EXECUTION_FAILED"
        else "fix_input"
    )
    value = replace(
        target.check,
        status=UsageCheckStatus.FAILED,
        issues=(
            {
                "code": code,
                "detail": detail,
                "location": "worker",
                "retryable": retryable,
                "recovery_action": recovery_action,
            },
        ),
        updated_at=now,
    )
    if not datasets.save_usage_check(value, expected_updated_at=target.check.updated_at):
        raise DatasetRefusedError(DatasetRefusalCode.USAGE_STATE_CONFLICT)
    _logger.warning(
        "dataset.usage_check.failed",
        dataset_id=str(value.dataset_id),
        usage_kind=value.kind.value,
        check_id=str(value.id),
        input_digest=value.input_digest,
        actor_id=str(value.created_by),
        result=value.status.value,
        failure_code=code,
    )
    return value


def apply_usage_check_currentness(
    *, check: UsageCheck, validation: UsageValidationResult, datasets: DatasetRepository
) -> UsageValidationResult:
    """在外部媒体读取完成后的短事务中追加当前性结论。"""
    if usage_check_is_current(check=check, datasets=datasets):
        return validation
    return _with_snapshot(
        validation,
        dict(validation.input_snapshot),
        (UsageIssue("USAGE_INPUT_CHANGED", "受检输入已变化，请重新检查", "input"),),
    )


def usage_check_is_current(*, check: UsageCheck, datasets: DatasetRepository) -> bool:
    """判断已通过检查是否仍对应数据集当前输入。"""
    expected_contract = _contract_version(check.kind)
    if check.base_commit != BASE_COMMIT or check.contract_version != expected_contract:
        return False
    return _snapshot_is_current(
        kind=check.kind,
        snapshot=check.input_snapshot,
        datasets=datasets,
    )


def _snapshot_is_current(
    *, kind: UsageKind, snapshot: Mapping[str, Any], datasets: DatasetRepository
) -> bool:
    expected_contract = _contract_version(kind)
    if (
        snapshot.get("base_commit") != BASE_COMMIT
        or snapshot.get("contract_version") != expected_contract
    ):
        return False
    match kind:
        case UsageKind.DDM | UsageKind.VLM:
            pass
        case _:
            assert_never(kind)
    if kind is UsageKind.DDM:
        if snapshot.get("consumer_parameters") != dict(DDM_CONSUMER_PARAMETERS):
            return False
        raw_videos = snapshot.get("videos")
        scope = snapshot.get("scope")
        dataset_id = snapshot.get("dataset_id")
        if not isinstance(raw_videos, list) or not isinstance(scope, list):
            return False
        if dataset_id is None:
            return False
        try:
            dataset_uuid = UUID(str(dataset_id))
        except (TypeError, ValueError):
            return False
        current_members = {
            str(member.id): member for member in datasets.list_members(dataset_id=dataset_uuid)
        }
        frozen_ids: set[str] = set()
        for item in scope:
            if not isinstance(item, dict) or item.get("member_id") is None:
                return False
            member_key = str(item["member_id"])
            frozen_ids.add(member_key)
            current = current_members.get(member_key)
            if (
                current is None
                or current.status != item.get("status")
                or current.object_key != item.get("object_version_id")
                or current.actual_sha256 != item.get("source_sha256")
                or current.actual_size != item.get("actual_size")
            ):
                return False
            submission = datasets.latest_annotation_submission(member_id=current.id)
            execution = (
                datasets.latest_annotation_execution(submission.id)
                if submission is not None
                else None
            )
            if (
                (str(submission.id) if submission is not None else None)
                != item.get("annotation_submission_id")
                or (submission.revision if submission is not None else None)
                != item.get("annotation_revision")
                or (submission.action_list_revision if submission is not None else None)
                != item.get("annotation_action_list_revision")
                or (str(execution.id) if execution is not None else None)
                != item.get("annotation_execution_id")
                or (execution.status.value if execution is not None else None)
                != item.get("annotation_execution_status")
            ):
                return False
        if frozen_ids != set(current_members):
            return False
        latest_actions = datasets.latest_action_list(dataset_uuid)
        if latest_actions is None:
            return False
        for raw in raw_videos:
            if not isinstance(raw, dict):
                return False
            try:
                member_id = UUID(str(raw["member_id"]))
                annotation_revision = int(raw["annotation_revision"])
                action_list_revision = int(raw["action_list_revision"])
            except (KeyError, TypeError, ValueError):
                return False
            if not _member_source_is_current(
                dataset_id=dataset_uuid,
                member_id=member_id,
                object_key=raw.get("object_version_id"),
                source_sha256=raw.get("source_sha256"),
                datasets=datasets,
            ):
                return False
            submission = datasets.latest_annotation_submission(member_id=member_id)
            execution = (
                datasets.latest_annotation_execution(submission.id)
                if submission is not None
                else None
            )
            if (
                submission is None
                or execution is None
                or execution.status is not AnnotationExecutionStatus.SUCCEEDED
                or str(execution.id) != str(raw.get("annotation_execution_id"))
                or submission.revision != annotation_revision
                or submission.action_list_revision != action_list_revision
                or submission.source_object_key != raw.get("object_version_id")
                or submission.source_sha256 != raw.get("source_sha256")
                or execution.upstream_data_id != raw.get("upstream_data_id")
                or execution.upstream_video_id != raw.get("upstream_video_id")
                or latest_actions.revision != action_list_revision
                or (
                    "annotation_clips" in raw
                    and [_stable_annotation_clip(clip) for clip in execution.clips]
                    != raw.get("annotation_clips")
                )
                or execution.derived_video_size != raw.get("derived_video_size")
                or execution.derived_video_sha256 != raw.get("derived_video_sha256")
                or execution.derived_video_duration_seconds
                != raw.get("derived_video_duration_seconds")
            ):
                return False
        return True
    match kind:
        case UsageKind.VLM:
            pass
        case _:
            assert_never(kind)
    candidate_id = snapshot.get("candidate_id")
    dataset_id = snapshot.get("dataset_id")
    raw_action_list_revision = snapshot.get("action_list_revision")
    if candidate_id is None or dataset_id is None or raw_action_list_revision is None:
        return False
    try:
        dataset_uuid = UUID(str(dataset_id))
        candidate_uuid = UUID(str(candidate_id))
        revision = int(raw_action_list_revision)
    except (TypeError, ValueError):
        return False
    raw_media = snapshot.get("media")
    if not isinstance(raw_media, list):
        return False
    for media in raw_media:
        if not isinstance(media, dict):
            return False
        try:
            media_member_id = UUID(str(media["member_id"]))
        except (KeyError, TypeError, ValueError):
            return False
        if not _member_source_is_current(
            dataset_id=dataset_uuid,
            member_id=media_member_id,
            object_key=media.get("source_object_version_id"),
            source_sha256=media.get("source_sha256"),
            datasets=datasets,
        ):
            return False
        submission_id = media.get("annotation_submission_id")
        execution_id = media.get("annotation_execution_id")
        if submission_id is None and execution_id is not None:
            return False
        submission = None
        if submission_id is not None:
            try:
                submission = datasets.annotation_submission_by_id(UUID(str(submission_id)))
            except (TypeError, ValueError):
                return False
            latest_submission = datasets.latest_annotation_submission(member_id=media_member_id)
            if (
                submission is None
                or latest_submission is None
                or latest_submission.id != submission.id
                or submission.dataset_id != dataset_uuid
                or submission.member_id != media_member_id
                or submission.source_object_key != media.get("source_object_version_id")
                or submission.source_sha256 != media.get("source_sha256")
                or submission.action_list_revision != revision
            ):
                return False
        if execution_id is not None:
            try:
                execution = datasets.annotation_execution_by_id(UUID(str(execution_id)))
            except (TypeError, ValueError):
                return False
            if execution is None or execution.status is not AnnotationExecutionStatus.SUCCEEDED:
                return False
            latest_execution = (
                datasets.latest_annotation_execution(submission.id) if submission else None
            )
            if (
                submission is None
                or latest_execution is None
                or latest_execution.id != execution.id
                or str(execution.submission_id) != str(submission.id)
                or str(execution.submission_id) != str(submission_id)
            ):
                return False
            clip_index = media.get("clip_index")
            if (
                isinstance(clip_index, bool)
                or not isinstance(clip_index, int)
                or clip_index < 0
                or clip_index >= len(execution.clips)
            ):
                return False
            clip = execution.clips[clip_index]
            if (
                execution.upstream_data_id != media.get("upstream_data_id")
                or execution.upstream_video_id != media.get("upstream_video_id")
                or not isinstance(clip, dict)
                or clip.get("filename") != media.get("clip_filename")
                or clip.get("start_time") != media.get("clip_start_time")
                or clip.get("end_time") != media.get("clip_end_time")
                or execution.derived_video_size != media.get("derived_video_size")
                or execution.derived_video_sha256 != media.get("derived_video_sha256")
                or execution.derived_video_duration_seconds
                != media.get("derived_video_duration_seconds")
            ):
                return False
    latest_candidate = datasets.latest_vlm_candidate(dataset_uuid)
    latest_actions = datasets.latest_action_list(dataset_uuid)
    return (
        latest_candidate is not None
        and latest_candidate.id == candidate_uuid
        and latest_actions is not None
        and latest_actions.revision == revision
    )


def _member_source_is_current(
    *,
    dataset_id: UUID,
    member_id: UUID,
    object_key: object,
    source_sha256: object,
    datasets: DatasetRepository,
) -> bool:
    member = datasets.member_by_id(member_id)
    return (
        member is not None
        and member.dataset_id == dataset_id
        and member.status == MemberStatus.REGISTERED
        and member.object_key == object_key
        and member.actual_sha256 == source_sha256
    )


def invalidate_artifact_for_job(
    *, job: ApplicationJob, datasets: DatasetRepository, now: datetime
) -> bool:
    """把输入已变化的待生成制品和任务安全结案。"""
    artifact = datasets.artifact_by_id(job.attempt_id)
    if (
        artifact is None
        or artifact.job_id != job.id
        or artifact.status not in {ArtifactStatus.PENDING, ArtifactStatus.RUNNING}
    ):
        return False
    has_candidate = bool(
        artifact.object_key
        or any(
            isinstance(key, str) and key
            for key in artifact.manifest.get("orphan_candidate_keys", [])
        )
    )
    value = replace(
        artifact,
        status=ArtifactStatus.FAILED,
        failure_code="USAGE_INPUT_CHANGED",
        failure_detail="用途检查输入已变化，请重新检查后再生成制品",
        retryable=has_candidate,
        recovery_action="retry_artifact_cleanup" if has_candidate else "fix_input",
        updated_at=now,
    )
    return datasets.save_artifact(value, expected_updated_at=artifact.updated_at)


def mark_artifact_cleanup_pending(
    *,
    target: ArtifactTarget,
    object_key: str,
    now: datetime,
    datasets: DatasetRepository,
) -> bool:
    """对象候选清理失败时保留可恢复状态，交给租约恢复再次处理。"""
    manifest = dict(target.artifact.manifest)
    keys = [
        key for key in manifest.get("orphan_candidate_keys", []) if isinstance(key, str) and key
    ]
    if object_key not in keys:
        keys.append(object_key)
    manifest["orphan_candidate_keys"] = sorted(set(keys))
    value = replace(
        target.artifact,
        status=ArtifactStatus.FAILED,
        object_key=None,
        manifest=manifest,
        failure_code="STORAGE_UNAVAILABLE",
        failure_detail="候选制品清理失败，请稍后重试",
        retryable=True,
        recovery_action="retry_artifact_cleanup",
        updated_at=now,
    )
    return datasets.save_artifact(value, expected_updated_at=target.artifact.updated_at)


def record_artifact_orphan_candidate(
    *, artifact_id: UUID, object_key: str, datasets: DatasetRepository
) -> bool:
    """把租约丢失后的候选键持久化，交给周期清理重试。"""
    artifact = datasets.artifact_by_id(artifact_id)
    if artifact is None or (
        artifact.status is ArtifactStatus.AVAILABLE and artifact.object_key == object_key
    ):
        return artifact is not None
    manifest = dict(artifact.manifest)
    keys = [
        key for key in manifest.get("orphan_candidate_keys", []) if isinstance(key, str) and key
    ]
    if object_key not in keys:
        keys.append(object_key)
    if keys:
        manifest["orphan_candidate_keys"] = sorted(set(keys))
    value = replace(artifact, manifest=manifest, updated_at=artifact.updated_at)
    return datasets.save_artifact(value, expected_updated_at=artifact.updated_at)


def complete_artifact_candidate_cleanup(
    *,
    artifact_id: UUID,
    job_id: UUID | None,
    now: datetime,
    datasets: DatasetRepository,
    candidate_keys: Sequence[str] | None = None,
    expected_updated_at: datetime | None = None,
) -> bool:
    """在候选对象清理成功后按键移除持久化清理标记。"""
    artifact = datasets.artifact_by_id(artifact_id)
    if artifact is None:
        return False
    if job_id is not None and (
        artifact.job_id != job_id or artifact.status is not ArtifactStatus.FAILED
    ):
        return False
    manifest = dict(artifact.manifest)
    raw_keys = manifest.get("orphan_candidate_keys", [])
    existing_keys = {key for key in raw_keys if isinstance(key, str) and key}
    removed_keys = set(candidate_keys) if candidate_keys is not None else existing_keys
    remaining_keys = sorted(existing_keys - removed_keys)
    if remaining_keys:
        manifest["orphan_candidate_keys"] = remaining_keys
    else:
        manifest.pop("orphan_candidate_keys", None)
    value = replace(
        artifact,
        object_key=(
            None
            if job_id is not None or artifact.status is not ArtifactStatus.AVAILABLE
            else artifact.object_key
        ),
        manifest=manifest,
        retryable=False if job_id is not None else artifact.retryable,
        recovery_action="fix_input" if job_id is not None else artifact.recovery_action,
        updated_at=now,
    )
    return datasets.save_artifact(
        value,
        expected_updated_at=expected_updated_at or artifact.updated_at,
    )


def begin_artifact_generation(
    *, job: ApplicationJob, datasets: DatasetRepository, now: datetime
) -> ArtifactTarget | None:
    """领取制品生成租约，并重新核对来源检查。"""
    artifact = datasets.artifact_by_id(job.attempt_id)
    if (
        artifact is None
        or artifact.dataset_id != (job.dataset_id or job.member_id)
        or artifact.job_id != job.id
        or artifact.status not in {ArtifactStatus.PENDING, ArtifactStatus.RUNNING}
    ):
        return None
    check = datasets.usage_check_by_id(artifact.usage_check_id)
    if (
        check is None
        or check.status is not UsageCheckStatus.PASSED
        or check.input_digest != artifact.input_digest
        or not usage_check_is_current(check=check, datasets=datasets)
    ):
        return None
    candidate_key = (
        f"training-datasets/{artifact.dataset_id}/artifacts/{artifact.id}/"
        f"{new_id()}/annotation.json"
    )
    manifest = dict(artifact.manifest)
    orphan_keys = [
        key for key in manifest.get("orphan_candidate_keys", []) if isinstance(key, str) and key
    ]
    if artifact.object_key is not None:
        orphan_keys.append(artifact.object_key)
    if orphan_keys:
        manifest["orphan_candidate_keys"] = sorted(set(orphan_keys))
    else:
        manifest.pop("orphan_candidate_keys", None)
    running = replace(
        artifact,
        status=ArtifactStatus.RUNNING,
        object_key=candidate_key,
        manifest=manifest,
        updated_at=now,
    )
    if not datasets.save_artifact(running, expected_updated_at=artifact.updated_at):
        return None
    return ArtifactTarget(job=job, artifact=running, check=check)


def render_ddm_artifact_with_base(
    target: ArtifactTarget,
    *,
    storage: ObjectStorage,
    generate: Callable[[Path, str], bytes],
) -> tuple[bytes, dict[str, Any]]:
    """下载冻结源对象并通过 NVIDIA 基座聚合函数生成制品。"""
    annotation = _ddm_annotation(target)
    raw_scope = target.check.input_snapshot.get("scope")
    if not isinstance(raw_scope, list):
        raise DatasetRefusedError(DatasetRefusalCode.ARTIFACT_UNAVAILABLE)
    scope = {
        str(item["member_id"]): item
        for item in raw_scope
        if isinstance(item, dict) and item.get("member_id") is not None
    }
    raw_sources = target.check.input_snapshot.get("annotation_sources")
    if not isinstance(raw_sources, dict):
        raise DatasetRefusedError(
            DatasetRefusalCode.ARTIFACT_UNAVAILABLE,
            detail="用途检查没有保存不可变标注副本",
        )
    annotation_source_digests: dict[str, str] = {}
    with TemporaryDirectory(prefix="nvsop-ddm-") as temporary:
        workspace = Path(temporary)
        for member_id in annotation:
            source = scope.get(member_id)
            if source is None or not isinstance(source.get("object_key"), str):
                raise DatasetRefusedError(
                    DatasetRefusalCode.ARTIFACT_UNAVAILABLE,
                    detail="冻结范围缺少完整源对象映射",
                )
            video_path = workspace / f"{member_id}.mp4"
            try:
                with video_path.open("wb") as destination:
                    storage.download_to(
                        object_key=source["object_key"],
                        destination=destination,
                    )
            except ObjectNotFoundError as error:
                raise DatasetRefusedError(
                    DatasetRefusalCode.OBJECT_NOT_FOUND,
                    detail="冻结的源视频对象不存在",
                ) from error
            except ObjectStorageUnavailableError as error:
                raise DatasetRefusedError(
                    DatasetRefusalCode.STORAGE_UNAVAILABLE,
                    detail="训练素材存储暂时不可用，请稍后重试",
                ) from error
            _verify_downloaded_source(video_path, source)
            copy = raw_sources.get(member_id)
            if not isinstance(copy, dict):
                raise DatasetRefusedError(
                    DatasetRefusalCode.ARTIFACT_UNAVAILABLE,
                    detail="用途检查没有保存该视频的不可变标注副本",
                )
            encoded = copy.get("content_base64")
            expected_annotation_sha256 = copy.get("sha256")
            if not isinstance(encoded, str) or not isinstance(expected_annotation_sha256, str):
                raise DatasetRefusedError(
                    DatasetRefusalCode.ARTIFACT_UNAVAILABLE,
                    detail="不可变标注副本清单无效",
                )
            try:
                source_annotation = base64.b64decode(encoded, validate=True)
                json.loads(source_annotation)
            except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError) as error:
                raise DatasetRefusedError(
                    DatasetRefusalCode.ARTIFACT_UNAVAILABLE,
                    detail="不可变标注副本不是有效 JSON",
                ) from error
            if hashlib.sha256(source_annotation).hexdigest() != expected_annotation_sha256:
                raise DatasetRefusedError(
                    DatasetRefusalCode.ARTIFACT_UNAVAILABLE,
                    detail="不可变标注副本摘要不一致",
                )
            annotation_source_digests[member_id] = expected_annotation_sha256
            annotation_directory = workspace / member_id
            annotation_directory.mkdir()
            (annotation_directory / f"{member_id}_annotation.json").write_bytes(source_annotation)
        generated = generate(workspace, "annotation.json")
        try:
            actual = json.loads(generated)
        except (TypeError, json.JSONDecodeError):
            raise DatasetRefusedError(
                DatasetRefusalCode.ARTIFACT_UNAVAILABLE,
                detail="NVIDIA DDM 聚合结果不是 JSON",
            ) from None
        normalized = _canonicalize_base_annotation(actual, target.check.input_snapshot)
        if set(normalized) != set(annotation):
            raise DatasetRefusedError(
                DatasetRefusalCode.ARTIFACT_UNAVAILABLE,
                detail="NVIDIA DDM 聚合结果缺少冻结视频",
            )
        for member_id, expected_entries in annotation.items():
            if not _clip_coverage_matches(
                _annotation_entries_as_clips(
                    normalized[member_id],
                    actions=_actions_for_member(target.check.input_snapshot, member_id),
                ),
                _annotation_entries_as_clips(
                    expected_entries,
                    actions=_actions_for_member(target.check.input_snapshot, member_id),
                ),
            ):
                raise DatasetRefusedError(
                    DatasetRefusalCode.ARTIFACT_UNAVAILABLE,
                    detail="NVIDIA DDM 聚合结果与冻结标注覆盖不一致",
                )
    return _ddm_content_and_manifest(
        normalized,
        target,
        annotation_source_digests=annotation_source_digests,
    )


def _canonicalize_base_annotation(
    value: object, snapshot: Mapping[str, Any]
) -> dict[str, list[dict[str, Any]]]:
    if not isinstance(value, dict):
        raise DatasetRefusedError(
            DatasetRefusalCode.ARTIFACT_UNAVAILABLE,
            detail="NVIDIA DDM 聚合结果结构无效",
        )
    actions_by_member = {
        str(video["member_id"]): tuple(video.get("actions", ()))
        for video in snapshot.get("videos", ())
        if isinstance(video, dict) and video.get("member_id") is not None
    }
    result: dict[str, list[dict[str, Any]]] = {}
    for member_id, raw_entries in value.items():
        actions = actions_by_member.get(str(member_id))
        if actions is None or not isinstance(raw_entries, list):
            raise DatasetRefusedError(
                DatasetRefusalCode.ARTIFACT_UNAVAILABLE,
                detail="NVIDIA DDM 聚合结果视频身份无效",
            )
        entries: list[dict[str, Any]] = []
        for index, entry in enumerate(raw_entries):
            normalized = _normalize_annotation_entry(
                entry, actions=actions, location=f"{member_id}[{index}]"
            )
            if isinstance(normalized, UsageIssue):
                raise DatasetRefusedError(
                    DatasetRefusalCode.ARTIFACT_UNAVAILABLE,
                    detail=normalized.detail,
                )
            indices = normalized["action_indices"]
            descriptions = normalized["action_descriptions"]
            if len(indices) > 1 or entry.get("is_concurrent") is True:
                entries.append(
                    {
                        "actions": [item + 1 for item in indices],
                        "descriptions": descriptions,
                        "is_concurrent": len(indices) > 1,
                        "start_timestamp": normalized["start_time"],
                        "end_timestamp": normalized["end_time"],
                    }
                )
            else:
                entries.append(
                    {
                        "description": descriptions[0],
                        "start_timestamp": normalized["start_time"],
                        "end_timestamp": normalized["end_time"],
                    }
                )
        result[str(member_id)] = sorted(
            entries,
            key=lambda item: (
                item["start_timestamp"],
                item["end_timestamp"],
                tuple(item.get("actions", ())),
                tuple(item.get("descriptions", (item.get("description", ""),))),
            ),
        )
    return result


def _actions_for_member(snapshot: Mapping[str, Any], member_id: str) -> tuple[str, ...]:
    for video in snapshot.get("videos", ()):
        if isinstance(video, dict) and str(video.get("member_id")) == member_id:
            return tuple(str(item) for item in video.get("actions", ()))
    return ()


def _annotation_entries_as_clips(
    entries: Sequence[Mapping[str, Any]], *, actions: Sequence[str]
) -> list[dict[str, Any]]:
    clips: list[dict[str, Any]] = []
    for entry in entries:
        indices = entry.get("actions")
        descriptions = entry.get("descriptions")
        if indices is None:
            descriptions = [entry["description"]]
            try:
                indices = [actions.index(str(descriptions[0])) + 1]
            except ValueError as error:
                raise DatasetRefusedError(
                    DatasetRefusalCode.ARTIFACT_UNAVAILABLE,
                    detail="DDM annotation 动作描述无法映射",
                ) from error
        if not isinstance(indices, list) or not isinstance(descriptions, list):
            raise DatasetRefusedError(
                DatasetRefusalCode.ARTIFACT_UNAVAILABLE,
                detail="DDM annotation 动作字段无效",
            )
        clips.append(
            {
                "start_time": entry["start_timestamp"],
                "end_time": entry["end_timestamp"],
                "action_indices": [int(item) - 1 for item in indices],
                "action_descriptions": list(descriptions),
            }
        )
    return clips


def _ddm_annotation(target: ArtifactTarget) -> dict[str, list[dict[str, Any]]]:
    """把基座已经产出的 clip 语义转换为训练读取器的稳定格式。"""
    videos = target.check.input_snapshot.get("videos")
    if not isinstance(videos, list):
        raise DatasetRefusedError(DatasetRefusalCode.ARTIFACT_UNAVAILABLE)
    annotation: dict[str, list[dict[str, Any]]] = {}
    for video in videos:
        if not isinstance(video, dict):
            raise DatasetRefusedError(DatasetRefusalCode.ARTIFACT_UNAVAILABLE)
        member_id = str(video.get("member_id", ""))
        clips = video.get("annotation_clips")
        if not member_id or not isinstance(clips, list) or not clips:
            raise DatasetRefusedError(
                DatasetRefusalCode.ARTIFACT_UNAVAILABLE,
                detail="冻结输入缺少基座已确认的标注输出",
            )
        entries: list[dict[str, Any]] = []
        for clip in clips:
            if not isinstance(clip, dict):
                raise DatasetRefusedError(
                    DatasetRefusalCode.ARTIFACT_UNAVAILABLE,
                    detail="基座标注输出结构无效",
                )
            try:
                start = float(clip["start_time"])
                end = float(clip["end_time"])
                concurrent = bool(clip.get("is_concurrent", False))
                raw_indices = clip.get("action_indices")
                raw_descriptions = clip.get("action_descriptions")
                if raw_indices is None:
                    raw_indices = [clip["action_index"]]
                if raw_descriptions is None:
                    raw_descriptions = [clip["action_description"]]
                indices = [int(item) for item in raw_indices]
                descriptions = [str(item) for item in raw_descriptions]
                if not indices or len(indices) != len(descriptions):
                    raise ValueError("actions")
                if not all(math.isfinite(value) for value in (start, end)) or end <= start:
                    raise ValueError("range")
                if concurrent or len(indices) > 1:
                    entries.append(
                        {
                            "actions": [item + 1 for item in indices],
                            "descriptions": descriptions,
                            "is_concurrent": len(indices) > 1 or concurrent,
                            "start_timestamp": start,
                            "end_timestamp": end,
                        }
                    )
                else:
                    entries.append(
                        {
                            "description": descriptions[0],
                            "start_timestamp": start,
                            "end_timestamp": end,
                        }
                    )
            except (KeyError, TypeError, ValueError):
                raise DatasetRefusedError(
                    DatasetRefusalCode.ARTIFACT_UNAVAILABLE,
                    detail="冻结的 DDM 基座输出无法生成制品",
                ) from None
        annotation[member_id] = entries
    return annotation


def _stable_annotation_clip(value: Mapping[str, Any]) -> dict[str, Any]:
    """只保留基座 clip 的稳定业务字段，排除生成时刻和临时路径。"""
    allowed = (
        "id",
        "filename",
        "start_time",
        "end_time",
        "duration",
        "action_index",
        "action_indices",
        "action_description",
        "action_descriptions",
        "is_concurrent",
        "timeline_order",
    )
    return {
        key: value[key]
        for key in allowed
        if key in value and isinstance(value[key], (str, int, float, bool, list, type(None)))
    }


def _ddm_content_and_manifest(
    annotation: dict[str, list[dict[str, Any]]],
    target: ArtifactTarget,
    *,
    annotation_source_digests: Mapping[str, str] | None = None,
) -> tuple[bytes, dict[str, Any]]:
    content = canonical_json(annotation)
    videos = target.check.input_snapshot.get("videos")
    if not isinstance(videos, list):
        raise DatasetRefusedError(DatasetRefusalCode.ARTIFACT_UNAVAILABLE)
    snapshot_sources = target.check.input_snapshot.get("scope")
    scope_by_member = (
        {
            str(item.get("member_id")): item
            for item in snapshot_sources
            if isinstance(item, dict) and item.get("member_id") is not None
        }
        if isinstance(snapshot_sources, list)
        else {}
    )
    sources = []
    for video in videos:
        if not isinstance(video, dict):
            continue
        scope = scope_by_member.get(str(video.get("member_id")), {})
        sources.append(
            {
                "member_id": video["member_id"],
                "object_key": scope.get("object_key"),
                "object_version_id": video["object_version_id"],
                "source_sha256": video["source_sha256"],
                "actual_size": scope.get("actual_size"),
                "duration_seconds": video["duration_seconds"],
                "action_list_revision": video["action_list_revision"],
                "annotation_revision": video["annotation_revision"],
                "mode": video["mode"],
                "actions": list(video["actions"]),
                "segments": list(video["segments"]),
                "annotation_execution_id": video.get("annotation_execution_id"),
                "upstream_data_id": video.get("upstream_data_id"),
                "upstream_video_id": video.get("upstream_video_id"),
                "derived_video_size": video.get("derived_video_size"),
                "derived_video_sha256": video.get("derived_video_sha256"),
                "derived_video_duration_seconds": video.get("derived_video_duration_seconds"),
                "annotation_source_sha256": (annotation_source_digests or {}).get(
                    str(video.get("member_id"))
                ),
                "annotation_clips": list(video.get("annotation_clips", [])),
            }
        )
    manifest = {
        "artifact_format_version": 1,
        "input_digest": target.check.input_digest,
        "base_commit": target.check.base_commit,
        "contract_version": target.check.contract_version,
        "consumer_parameters": dict(
            target.check.input_snapshot.get("consumer_parameters", DDM_CONSUMER_PARAMETERS)
        ),
        "video_count": len(annotation),
        "sources": sources,
        "artifact_sha256": hashlib.sha256(content).hexdigest(),
        "artifact_size": len(content),
    }
    return content, manifest


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as content:
        for chunk in iter(lambda: content.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_downloaded_source(path: Path, source: Mapping[str, Any]) -> None:
    expected_size = source.get("actual_size")
    expected_sha256 = source.get("source_sha256")
    if not isinstance(expected_size, int) or not isinstance(expected_sha256, str):
        raise DatasetRefusedError(
            DatasetRefusalCode.ARTIFACT_UNAVAILABLE,
            detail="冻结范围缺少源对象大小或摘要",
        )
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as content:
        for chunk in iter(lambda: content.read(1024 * 1024), b""):
            size += len(chunk)
            digest.update(chunk)
    if size != expected_size or digest.hexdigest() != expected_sha256:
        raise DatasetRefusedError(
            DatasetRefusalCode.ARTIFACT_UNAVAILABLE,
            detail="下载的源对象与冻结摘要不一致",
        )


def complete_artifact(
    *,
    target: ArtifactTarget,
    object_key: str,
    artifact_sha256: str,
    artifact_size: int,
    manifest: Mapping[str, Any],
    now: datetime,
    datasets: DatasetRepository,
) -> DatasetArtifact:
    """登记已写入并核对摘要的制品。"""
    current_check = datasets.usage_check_by_id(target.check.id)
    if (
        current_check is None
        or current_check.dataset_id != target.artifact.dataset_id
        or current_check.input_digest != target.artifact.input_digest
        or current_check.status is not UsageCheckStatus.PASSED
        or not usage_check_is_current(check=current_check, datasets=datasets)
    ):
        raise DatasetRefusedError(
            DatasetRefusalCode.USAGE_STATE_CONFLICT,
            detail="制品生成期间用途检查输入已变化，请重新检查",
        )
    if (
        manifest.get("artifact_sha256") != artifact_sha256
        or manifest.get("artifact_size") != artifact_size
        or artifact_size <= 0
    ):
        raise DatasetRefusedError(
            DatasetRefusalCode.ARTIFACT_UNAVAILABLE,
            detail="制品摘要与清单不一致",
        )
    value = replace(
        target.artifact,
        status=ArtifactStatus.AVAILABLE,
        object_key=object_key,
        artifact_sha256=artifact_sha256,
        artifact_size=artifact_size,
        manifest=dict(manifest),
        failure_code=None,
        failure_detail=None,
        retryable=False,
        recovery_action=None,
        updated_at=now,
    )
    if not datasets.save_artifact(value, expected_updated_at=target.artifact.updated_at):
        raise DatasetRefusedError(DatasetRefusalCode.USAGE_STATE_CONFLICT)
    _logger.info(
        "dataset.artifact.completed",
        dataset_id=str(value.dataset_id),
        usage_kind=value.kind.value,
        artifact_id=str(value.id),
        usage_check_id=str(value.usage_check_id),
        input_digest=value.input_digest,
        actor_id=str(value.created_by),
        result=value.status.value,
    )
    return value


def fail_artifact(
    *, target: ArtifactTarget, code: str, detail: str, now: datetime, datasets: DatasetRepository
) -> DatasetArtifact:
    """记录制品失败而保留历史制品。"""
    retryable = code in {
        "STORAGE_UNAVAILABLE",
        "ARTIFACT_GENERATION_FAILED",
        "ARTIFACT_DATABASE_FAILURE",
        "ARTIFACT_INTEGRITY_FAILURE",
    }
    value = replace(
        target.artifact,
        status=ArtifactStatus.FAILED,
        failure_code=code,
        failure_detail=detail,
        retryable=retryable,
        recovery_action="retry_artifact" if retryable else "fix_input",
        updated_at=now,
    )
    if not datasets.save_artifact(value, expected_updated_at=target.artifact.updated_at):
        raise DatasetRefusedError(DatasetRefusalCode.USAGE_STATE_CONFLICT)
    _logger.warning(
        "dataset.artifact.failed",
        dataset_id=str(value.dataset_id),
        usage_kind=value.kind.value,
        artifact_id=str(value.id),
        usage_check_id=str(value.usage_check_id),
        input_digest=value.input_digest,
        actor_id=str(value.created_by),
        result=value.status.value,
        failure_code=code,
    )
    return value


def _freeze_ddm(
    *, dataset_id: UUID, datasets: DatasetRepository
) -> tuple[UsageValidationResult, dict[str, Any]]:
    members = list(datasets.list_members(dataset_id=dataset_id))
    videos: list[DdmVideoInput] = []
    pre_issues: list[UsageIssue] = []
    scope: list[dict[str, Any]] = []
    executions: dict[str, AnnotationExecution] = {}
    for member in members:
        scope.append(
            {
                "member_id": str(member.id),
                "status": member.status,
                "object_key": member.object_key,
                "object_version_id": member.object_key,
                "source_sha256": member.actual_sha256,
                "actual_size": member.actual_size,
                "duration_seconds": member.duration_seconds,
                "codec": member.codec,
                "container": member.container,
                "annotation_submission_id": None,
                "annotation_revision": None,
                "annotation_action_list_revision": None,
                "annotation_execution_id": None,
                "annotation_execution_status": None,
            }
        )
        if member.status != MemberStatus.REGISTERED:
            pre_issues.append(
                UsageIssue("DDM_MEMBER_NOT_REGISTERED", "视频尚未完成登记校验", str(member.id))
            )
            continue
        submission = datasets.latest_annotation_submission(member_id=member.id)
        if submission is None:
            pre_issues.append(
                UsageIssue("DDM_ANNOTATION_MISSING", "视频没有已保存动作标注", str(member.id))
            )
            continue
        execution = datasets.latest_annotation_execution(submission.id)
        if execution is None or execution.status is not AnnotationExecutionStatus.SUCCEEDED:
            pre_issues.append(
                UsageIssue(
                    "DDM_ANNOTATION_EXECUTION_MISSING", "视频缺少成功的标注执行结果", str(member.id)
                )
            )
            continue
        if (
            submission.dataset_id != member.dataset_id
            or submission.source_object_key != member.object_key
            or submission.source_sha256 != member.actual_sha256
            or execution.submission_id != submission.id
        ):
            pre_issues.append(
                UsageIssue(
                    "DDM_ANNOTATION_SOURCE_CHANGED",
                    "标注提交没有绑定当前已确认的源对象",
                    str(member.id),
                )
            )
            continue
        scope_item = scope[-1]
        scope_item["annotation_submission_id"] = str(submission.id)
        scope_item["annotation_revision"] = submission.revision
        scope_item["annotation_action_list_revision"] = submission.action_list_revision
        scope_item["annotation_execution_id"] = str(execution.id)
        scope_item["annotation_execution_status"] = execution.status.value
        executions[str(member.id)] = execution
        actions = datasets.action_list_by_revision(
            dataset_id=dataset_id,
            revision=submission.action_list_revision,
        )
        if actions is None:
            pre_issues.append(
                UsageIssue(
                    "DDM_ACTION_LIST_NOT_FOUND", "标注引用的动作列表修订不存在", str(member.id)
                )
            )
            continue
        if (
            member.duration_seconds is None
            or member.actual_size is None
            or member.object_key is None
            or member.actual_sha256 is None
        ):
            pre_issues.append(
                UsageIssue("DDM_SOURCE_FACT_MISSING", "视频缺少已确认的源对象事实", str(member.id))
            )
            continue
        videos.append(
            DdmVideoInput(
                member_id=member.id,
                object_key=member.object_key,
                source_sha256=member.actual_sha256,
                duration_seconds=member.duration_seconds,
                action_list_revision=submission.action_list_revision,
                annotation_revision=submission.revision,
                mode=submission.mode,
                actions=tuple(actions.actions),
                segments=tuple(
                    {
                        "start": segment.start,
                        "end": segment.end,
                        "action_index": segment.action_index,
                        "action_description": segment.action_description,
                    }
                    for segment in submission.segments
                ),
            )
        )
    validation = validate_ddm_input(
        tuple(videos),
        base_commit=BASE_COMMIT,
        contract_version=DDM_CONTRACT_VERSION,
    )
    snapshot = dict(validation.input_snapshot)
    snapshot["dataset_id"] = str(dataset_id)
    snapshot["consumer_parameters"] = dict(DDM_CONSUMER_PARAMETERS)
    for video in snapshot.get("videos", []):
        if isinstance(video, dict):
            execution = executions.get(str(video.get("member_id")))
            if execution is not None:
                video["annotation_execution_id"] = str(execution.id)
                video["annotation_clips"] = [
                    _stable_annotation_clip(clip) for clip in execution.clips
                ]
                video["upstream_data_id"] = execution.upstream_data_id
                video["upstream_video_id"] = execution.upstream_video_id
                video["derived_video_size"] = execution.derived_video_size
                video["derived_video_sha256"] = execution.derived_video_sha256
                video["derived_video_duration_seconds"] = execution.derived_video_duration_seconds
                for scope_item in scope:
                    if scope_item["member_id"] == video.get("member_id"):
                        scope_item["upstream_data_id"] = execution.upstream_data_id
                        scope_item["upstream_video_id"] = execution.upstream_video_id
                        scope_item["derived_video_size"] = execution.derived_video_size
                        scope_item["derived_video_sha256"] = execution.derived_video_sha256
                        scope_item["derived_video_duration_seconds"] = (
                            execution.derived_video_duration_seconds
                        )
                        break
    snapshot["scope"] = scope
    snapshot["pre_issues"] = [_issue_dict(item) for item in pre_issues]
    merged = _with_snapshot(validation, snapshot, tuple(pre_issues))
    summary = dict(merged.summary)
    summary["video_count"] = len(scope)
    merged = replace(merged, summary=summary)
    return merged, snapshot


def _validate_ddm_snapshot(snapshot: Mapping[str, Any]) -> UsageValidationResult:
    raw_videos = snapshot.get("videos")
    videos: list[DdmVideoInput] = []
    if isinstance(raw_videos, list):
        for raw in raw_videos:
            if not isinstance(raw, dict):
                continue
            try:
                videos.append(
                    DdmVideoInput(
                        member_id=UUID(str(raw["member_id"])),
                        object_key=str(raw["object_version_id"]),
                        source_sha256=str(raw["source_sha256"]),
                        duration_seconds=float(raw["duration_seconds"]),
                        action_list_revision=int(raw["action_list_revision"]),
                        annotation_revision=int(raw["annotation_revision"]),
                        mode=AnnotationMode(str(raw["mode"])),
                        actions=tuple(str(item) for item in raw["actions"]),
                        segments=tuple(raw["segments"]),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
    result = validate_ddm_input(
        tuple(videos),
        base_commit=str(snapshot.get("base_commit", BASE_COMMIT)),
        contract_version=str(snapshot.get("contract_version", DDM_CONTRACT_VERSION)),
    )
    pre_issues = tuple(
        _issue_from_mapping(item)
        for item in snapshot.get("pre_issues", [])
        if isinstance(item, dict)
    )
    return _with_snapshot(
        result,
        dict(snapshot),
        (*pre_issues, *_validate_base_clips(snapshot)),
    )


def _validate_base_clips(snapshot: Mapping[str, Any]) -> tuple[UsageIssue, ...]:
    """核对基座输出仍覆盖产品保存的原始动作时间段。"""
    raw_videos = snapshot.get("videos")
    if not isinstance(raw_videos, list):
        return (UsageIssue("DDM_BASE_OUTPUT_MISSING", "冻结输入没有视频范围", "videos"),)
    issues: list[UsageIssue] = []
    for video in raw_videos:
        if not isinstance(video, dict):
            continue
        location = str(video.get("member_id", "video"))
        raw_segments = video.get("segments")
        raw_clips = video.get("annotation_clips")
        if not isinstance(raw_segments, list) or not isinstance(raw_clips, list) or not raw_clips:
            issues.append(
                UsageIssue(
                    "DDM_BASE_OUTPUT_MISSING",
                    "标注执行没有保存基座输出",
                    location,
                )
            )
            continue
        actions = tuple(video.get("actions", ()))
        expected: list[tuple[float, float, int, str]] = []
        for _index, segment in enumerate(raw_segments):
            if not isinstance(segment, dict):
                continue
            try:
                expected.append(
                    (
                        float(segment["start"]),
                        float(segment["end"]),
                        int(segment["action_index"]),
                        str(segment["action_description"]),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        actual: list[tuple[float, float, int, str]] = []
        for index, clip in enumerate(raw_clips):
            if not isinstance(clip, dict):
                issues.append(
                    UsageIssue(
                        "DDM_BASE_OUTPUT_INVALID",
                        "基座输出条目不是对象",
                        f"{location}.annotation_clips[{index}]",
                    )
                )
                continue
            try:
                start = float(clip["start_time"])
                end = float(clip["end_time"])
                indices = clip.get("action_indices")
                descriptions = clip.get("action_descriptions")
                if indices is None:
                    indices = [clip["action_index"]]
                if descriptions is None:
                    descriptions = [clip["action_description"]]
                if not isinstance(indices, list) or not isinstance(descriptions, list):
                    raise ValueError("actions")
                if len(indices) != len(descriptions) or not indices:
                    raise ValueError("actions")
                duration = float(video["duration_seconds"])
                if (
                    not math.isfinite(start)
                    or not math.isfinite(end)
                    or end <= start
                    or start < 0
                    or end > duration
                ):
                    raise ValueError("range")
                for action_index, description in zip(indices, descriptions, strict=True):
                    if isinstance(action_index, bool) or not isinstance(action_index, int):
                        raise ValueError("action_index")
                    if action_index < 0 or action_index >= len(actions):
                        raise ValueError("action_index")
                    if actions[action_index] != description:
                        raise ValueError("description")
                    actual.append((start, end, action_index, str(description)))
            except (KeyError, TypeError, ValueError):
                issues.append(
                    UsageIssue(
                        "DDM_BASE_OUTPUT_INVALID",
                        "基座输出条目无法与动作清单对应",
                        f"{location}.annotation_clips[{index}]",
                    )
                )
        for index, (start, end, action_index, description) in enumerate(expected):
            matching = [
                (clip_start, clip_end)
                for clip_start, clip_end, clip_action, clip_description in actual
                if clip_action == action_index and clip_description == description
            ]
            if not _interval_covered(matching, start, end):
                issues.append(
                    UsageIssue(
                        "DDM_BASE_OUTPUT_LOSS",
                        "基座输出没有覆盖原始动作时间段",
                        f"{location}.segments[{index}]",
                    )
                )
        for index, (start, end, action_index, description) in enumerate(actual):
            matching = [
                (source_start, source_end)
                for source_start, source_end, source_action, source_description in expected
                if source_action == action_index and source_description == description
            ]
            if not _interval_covered(matching, start, end):
                issues.append(
                    UsageIssue(
                        "DDM_BASE_OUTPUT_MISMATCH",
                        "基座输出包含原始标注没有声明的时间范围",
                        f"{location}.annotation_clips[{index}]",
                    )
                )
    return tuple(issues)


def _interval_covered(intervals: Sequence[tuple[float, float]], start: float, end: float) -> bool:
    cursor = start
    for interval_start, interval_end in sorted(intervals):
        if interval_end <= cursor + _EPSILON:
            continue
        if interval_start > cursor + _EPSILON:
            return False
        cursor = max(cursor, interval_end)
        if cursor >= end - _EPSILON:
            return True
    return cursor >= end - _EPSILON


def _candidate_from_snapshot(snapshot: Mapping[str, Any], check: UsageCheck) -> VlmCandidate:
    raw_media = snapshot.get("media")
    media: list[VlmMediaReference] = []
    if isinstance(raw_media, list):
        for item in raw_media:
            if not isinstance(item, dict):
                continue
            try:
                media.append(
                    VlmMediaReference(
                        key=str(item["key"]),
                        member_id=UUID(str(item["member_id"])),
                        source_object_key=str(item["source_object_version_id"]),
                        source_sha256=str(item["source_sha256"]),
                        annotation_submission_id=(
                            UUID(str(item["annotation_submission_id"]))
                            if item.get("annotation_submission_id")
                            else None
                        ),
                        annotation_execution_id=(
                            UUID(str(item["annotation_execution_id"]))
                            if item.get("annotation_execution_id")
                            else None
                        ),
                        clip_index=(
                            int(item["clip_index"]) if item.get("clip_index") is not None else None
                        ),
                        action_indices=tuple(
                            int(action_index) for action_index in (item.get("action_indices") or ())
                        ),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
    return VlmCandidate(
        id=UUID(str(snapshot.get("candidate_id", check.candidate_id or new_id()))),
        dataset_id=check.dataset_id,
        revision=int(snapshot.get("revision", 0)),
        kind=VlmCandidateKind(str(snapshot.get("kind", snapshot.get("candidate_kind", "gqa")))),
        action_list_revision=int(snapshot.get("action_list_revision", 0)),
        records=tuple(item for item in snapshot.get("records", []) if isinstance(item, dict)),
        media=tuple(media),
        created_by=check.created_by,
        created_at=check.created_at,
    )


def _merge_storage_facts(
    *,
    kind: UsageKind,
    validation: UsageValidationResult,
    snapshot: Mapping[str, Any],
    storage: ObjectStorage,
    media_probe: MediaProbe,
    annotation_volume: AnnotationDataVolume | None,
    ddm_reader: object | None,
    vlm_reader: object | None,
) -> UsageValidationResult:
    issues = list(validation.issues)
    sampling_counts: dict[str, int] = {}
    scope = snapshot.get("scope")
    if isinstance(scope, list):
        try:
            UUID(str(snapshot["dataset_id"]))
        except (KeyError, TypeError, ValueError):
            return _with_snapshot(
                validation,
                dict(snapshot),
                (*issues, UsageIssue("USAGE_SCOPE_INVALID", "冻结范围缺少数据集身份", "scope")),
            )
        with TemporaryDirectory(prefix="nvsop-usage-") as temporary:
            for item in scope:
                if (
                    not isinstance(item, dict)
                    or item.get("status") != MemberStatus.REGISTERED.value
                ):
                    continue
                try:
                    member_id = UUID(str(item["member_id"]))
                except (KeyError, ValueError, TypeError):
                    issues.append(
                        UsageIssue("USAGE_SCOPE_INVALID", "冻结范围缺少视频身份", "scope")
                    )
                    continue
                is_annotation_clip = (
                    kind is UsageKind.VLM and item.get("annotation_execution_id") is not None
                )
                if is_annotation_clip:
                    _verify_frozen_media(
                        member_id=member_id,
                        source=item,
                        storage=storage,
                        media_probe=media_probe,
                        annotation_volume=annotation_volume,
                        directory=Path(temporary),
                        issues=issues,
                    )
                    continue
                object_key = item.get("object_key")
                object_version_id = item.get("object_version_id")
                if (
                    not isinstance(object_key, str)
                    or not object_key
                    or not isinstance(object_version_id, str)
                    or not object_version_id
                ):
                    issues.append(
                        UsageIssue(
                            "USAGE_SOURCE_MISSING", "视频缺少可读取的定稿对象", str(member_id)
                        )
                    )
                    continue
                try:
                    stat = storage.stat(object_key=object_key)
                    expected_size = item.get("actual_size")
                    if isinstance(expected_size, int) and stat.size != expected_size:
                        issues.append(
                            UsageIssue(
                                "USAGE_OBJECT_SIZE_CHANGED", "源对象大小已变化", str(member_id)
                            )
                        )
                    match kind:
                        case UsageKind.DDM:
                            _probe_frozen_video(
                                member_id=member_id,
                                source=item,
                                storage=storage,
                                media_probe=media_probe,
                                directory=Path(temporary),
                                issues=issues,
                            )
                        case UsageKind.VLM:
                            _verify_frozen_media(
                                member_id=member_id,
                                source=item,
                                storage=storage,
                                media_probe=media_probe,
                                annotation_volume=annotation_volume,
                                directory=Path(temporary),
                                issues=issues,
                            )
                        case _:
                            assert_never(kind)
                except ObjectNotFoundError:
                    issues.append(
                        UsageIssue("USAGE_OBJECT_NOT_FOUND", "源对象不存在", str(member_id))
                    )
                except ObjectStorageUnavailableError:
                    issues.append(
                        UsageIssue(
                            "USAGE_STORAGE_UNAVAILABLE",
                            "训练素材存储暂时不可用",
                            str(member_id),
                            retryable=True,
                            recovery_action="retry_usage_check",
                        )
                    )
            match kind:
                case UsageKind.DDM:
                    if annotation_volume is not None:
                        issues.extend(
                            _validate_annotation_volume(
                                snapshot,
                                annotation_volume,
                                media_probe=media_probe,
                            )
                        )
                    _validate_ddm_reader(
                        reader=ddm_reader,
                        snapshot=snapshot,
                        directory=Path(temporary),
                        issues=issues,
                        sampling_counts=sampling_counts,
                    )
                case UsageKind.VLM:
                    if not issues:
                        _validate_vlm_reader(
                            reader=vlm_reader,
                            snapshot=snapshot,
                            directory=Path(temporary),
                            issues=issues,
                        )
                case _:
                    assert_never(kind)
    match kind:
        case UsageKind.DDM:
            if sampling_counts:
                for sample_class in ("boundary", "non_boundary"):
                    count = sampling_counts.get(f"{sample_class}_sample_count", 0)
                    if count == 0:
                        issues.append(
                            UsageIssue(
                                "DDM_SAMPLE_CLASS_EMPTY",
                                f"受检范围不能产生非空的{sample_class}训练样本类",
                                f"sampling.{sample_class}",
                            )
                        )
        case UsageKind.VLM:
            pass
        case _:
            assert_never(kind)
    match kind:
        case UsageKind.DDM:
            if annotation_volume is None:
                issues.append(
                    UsageIssue(
                        "USAGE_ANNOTATION_VOLUME_UNAVAILABLE",
                        "标注数据卷暂时不可用，请稍后重试",
                        "annotation_volume",
                        retryable=True,
                        recovery_action="retry_usage_check",
                    )
                )
            else:
                pass
        case UsageKind.VLM:
            pass
        case _:
            assert_never(kind)
    result = _with_snapshot(validation, dict(snapshot), tuple(issues))
    match kind:
        case UsageKind.DDM:
            summary = dict(result.summary)
            summary.update(sampling_counts)
            summary.setdefault("boundary_sample_count", 0)
            summary.setdefault("non_boundary_sample_count", 0)
            result = replace(result, summary=summary)
        case UsageKind.VLM:
            pass
        case _:
            assert_never(kind)
    return result


def _validate_ddm_reader(
    *,
    reader: object,
    snapshot: Mapping[str, Any],
    directory: Path,
    issues: list[UsageIssue],
    sampling_counts: dict[str, int],
) -> None:
    """在冻结工作区实际构造 NVIDIA DDM 读取器。"""
    annotation: dict[str, list[dict[str, Any]]] = {}
    raw_sources = snapshot.get("annotation_sources")
    if isinstance(raw_sources, dict):
        for member_id, source in raw_sources.items():
            if not isinstance(member_id, str) or not isinstance(source, dict):
                continue
            encoded = source.get("content_base64")
            if not isinstance(encoded, str):
                continue
            try:
                document = json.loads(base64.b64decode(encoded, validate=True))
            except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
                continue
            if isinstance(document, list):
                annotation[member_id] = [item for item in document if isinstance(item, dict)]
    annotation_filename = "nvsop-ddm-reader.json"
    (directory / annotation_filename).write_text(
        json.dumps(annotation, ensure_ascii=False), encoding="utf-8"
    )
    sample_counts = getattr(reader, "sample_counts", None)
    if not callable(sample_counts):
        issues.append(
            UsageIssue(
                "DDM_READER_UNAVAILABLE",
                "NVIDIA DDM 训练读取器不可用，请稍后重试",
                "reader",
                retryable=True,
                recovery_action="retry_usage_check",
            )
        )
        return
    try:
        result = sample_counts(
            workspace=directory,
            annotation_filename=annotation_filename,
            parameters=dict(DDM_CONSUMER_PARAMETERS),
        )
        boundary = result.get("boundary_sample_count")
        non_boundary = result.get("non_boundary_sample_count")
        if not isinstance(boundary, int) or not isinstance(non_boundary, int):
            raise DdmReaderInputError
        sampling_counts.update(
            boundary_sample_count=boundary,
            non_boundary_sample_count=non_boundary,
        )
    except DdmSampleClassEmptyError:
        issues.append(
            UsageIssue(
                "DDM_SAMPLE_CLASS_EMPTY",
                "受检范围不能产生非空的边界或非边界训练样本类",
                "sampling",
                recovery_action="fix_input",
            )
        )
    except DdmReaderInputError:
        issues.append(
            UsageIssue(
                "DDM_READER_INPUT_INVALID",
                "冻结工作区不符合 NVIDIA DDM 训练读取器契约",
                "reader",
                recovery_action="fix_input",
            )
        )
    except DdmReaderUnavailableError:
        issues.append(
            UsageIssue(
                "DDM_READER_UNAVAILABLE",
                "NVIDIA DDM 训练读取器无法消费冻结工作区，请稍后重试",
                "reader",
                retryable=True,
                recovery_action="retry_usage_check",
            )
        )


def _validate_vlm_reader(
    *, reader: object, snapshot: Mapping[str, Any], directory: Path, issues: list[UsageIssue]
) -> None:
    """在冻结工作区实际构造 NVIDIA VLM 读取器。"""
    for item in snapshot.get("media", []):
        if not isinstance(item, dict):
            continue
        key = item.get("key")
        member_id = item.get("member_id")
        if not isinstance(key, str) or not isinstance(member_id, str):
            continue
        source = directory / _vlm_workspace_filename(key)
        destination = directory / PurePosixPath(key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.is_file() and source != destination:
            shutil.copyfile(source, destination)
    annotation_filename = "nvsop-vlm-reader.json"
    (directory / annotation_filename).write_text(
        json.dumps(list(snapshot.get("records", [])), ensure_ascii=False), encoding="utf-8"
    )
    validate = getattr(reader, "validate", None)
    if not callable(validate):
        issues.append(
            UsageIssue(
                "VLM_READER_UNAVAILABLE",
                "NVIDIA VLM 训练读取器不可用，请稍后重试",
                "reader",
                retryable=True,
                recovery_action="retry_usage_check",
            )
        )
        return
    try:
        validate(workspace=directory, annotation_filename=annotation_filename)
    except VlmReaderInputError:
        issues.append(
            UsageIssue(
                "VLM_READER_INPUT_INVALID",
                "冻结候选不符合 NVIDIA VLM 训练读取器契约",
                "reader",
                recovery_action="fix_input",
            )
        )
    except VlmReaderUnavailableError:
        issues.append(
            UsageIssue(
                "VLM_READER_UNAVAILABLE",
                "NVIDIA VLM 训练读取器无法消费冻结候选，请稍后重试",
                "reader",
                retryable=True,
                recovery_action="retry_usage_check",
            )
        )


def _validate_annotation_source_video(
    *,
    video: Mapping[str, Any],
    data_id: str,
    video_id: str,
    annotation_volume: AnnotationDataVolume,
    media_probe: MediaProbe,
    location: str,
    issues: list[UsageIssue],
) -> None:
    """核对标注卷中成功执行视频的摘要、大小和时长。"""
    expected_sha256 = video.get("derived_video_sha256")
    expected_size = video.get("derived_video_size")
    expected_duration = video.get("derived_video_duration_seconds")
    if not isinstance(expected_sha256, str) and not isinstance(expected_size, int):
        return
    try:
        with TemporaryDirectory(prefix="nvsop-annotation-source-") as directory:
            path = Path(directory) / "source.mp4"
            with path.open("wb") as destination:
                annotation_volume.read_video(
                    data_id=data_id,
                    video_id=video_id,
                    filename=None,
                    destination=destination,
                )
            if isinstance(expected_size, int) and path.stat().st_size != expected_size:
                issues.append(
                    UsageIssue(
                        "DDM_ANNOTATION_SOURCE_SIZE_CHANGED", "标注源视频大小已变化", location
                    )
                )
            if isinstance(expected_sha256, str) and _file_sha256(path) != expected_sha256:
                issues.append(
                    UsageIssue(
                        "DDM_ANNOTATION_SOURCE_DIGEST_MISMATCH", "标注源视频摘要已变化", location
                    )
                )
            metadata = media_probe.probe(str(path))
            if (
                isinstance(expected_duration, (int, float))
                and abs(metadata.duration_seconds - expected_duration) > 1e-3
            ):
                issues.append(
                    UsageIssue(
                        "DDM_ANNOTATION_SOURCE_DURATION_CHANGED",
                        "标注源视频时长已变化",
                        location,
                    )
                )
            _append_ddm_sampling_facts_issue(metadata, location, issues)
    except AnnotationDataVolumeInputError:
        issues.append(
            UsageIssue("DDM_ANNOTATION_SOURCE_MISSING", "标注源视频不存在或映射无效", location)
        )
    except AnnotationDataVolumeReadError:
        issues.append(
            UsageIssue(
                "DDM_ANNOTATION_SOURCE_MISSING",
                "标注源视频暂时不可读取",
                location,
                retryable=True,
                recovery_action="retry_usage_check",
            )
        )
    except AnnotationDataVolumeUnavailableError:
        issues.append(
            UsageIssue(
                "DDM_ANNOTATION_SOURCE_MISSING",
                "标注源视频不存在或不可读取",
                location,
                retryable=True,
                recovery_action="retry_usage_check",
            )
        )
    except MediaProbeUnavailableError:
        issues.append(
            UsageIssue(
                "USAGE_MEDIA_PROBE_UNAVAILABLE",
                "标注源视频媒体探测暂时不可用",
                location,
                retryable=True,
                recovery_action="retry_usage_check",
            )
        )
    except InvalidMediaError:
        issues.append(UsageIssue("DDM_ANNOTATION_SOURCE_INVALID", "标注源视频无法读取", location))


def _validate_annotation_volume(
    snapshot: Mapping[str, Any],
    annotation_volume: AnnotationDataVolume,
    *,
    media_probe: MediaProbe,
) -> list[UsageIssue]:
    """读取成功标注卷并核对其语义覆盖已保存时间段。"""
    raw_videos = snapshot.get("videos")
    if not isinstance(raw_videos, list):
        return [UsageIssue("DDM_ANNOTATION_SOURCE_MISSING", "冻结输入没有标注视频范围", "videos")]
    issues: list[UsageIssue] = []
    source_videos: list[dict[str, Any]] = []
    source_copies: dict[str, dict[str, str]] = {}
    for video in raw_videos:
        if not isinstance(video, dict):
            continue
        location = str(video.get("member_id", "video"))
        data_id = video.get("upstream_data_id")
        video_id = video.get("upstream_video_id")
        if not isinstance(data_id, str) or not isinstance(video_id, str):
            issues.append(
                UsageIssue("DDM_ANNOTATION_SOURCE_MISSING", "标注执行缺少基座文件身份", location)
            )
            continue
        _validate_annotation_source_video(
            video=video,
            data_id=data_id,
            video_id=video_id,
            annotation_volume=annotation_volume,
            media_probe=media_probe,
            location=location,
            issues=issues,
        )
        try:
            source_bytes = annotation_volume.read_annotation(data_id=data_id, video_id=video_id)
            document = json.loads(source_bytes)
            source_copies[location] = {
                "data_id": data_id,
                "video_id": video_id,
                "sha256": hashlib.sha256(source_bytes).hexdigest(),
                "content_base64": base64.b64encode(source_bytes).decode("ascii"),
            }
        except AnnotationDataVolumeInputError:
            issues.append(
                UsageIssue(
                    "DDM_ANNOTATION_SOURCE_MISSING",
                    "成功标注文件不存在或映射无效",
                    location,
                )
            )
            continue
        except AnnotationDataVolumeReadError:
            issues.append(
                UsageIssue(
                    "DDM_ANNOTATION_SOURCE_MISSING",
                    "成功标注文件暂时不可读取",
                    location,
                    retryable=True,
                    recovery_action="retry_usage_check",
                )
            )
            continue
        except AnnotationDataVolumeUnavailableError:
            issues.append(
                UsageIssue(
                    "DDM_ANNOTATION_SOURCE_MISSING",
                    "成功标注文件不存在或不可读取",
                    location,
                    retryable=True,
                    recovery_action="retry_usage_check",
                )
            )
            continue
        except (TypeError, UnicodeDecodeError, json.JSONDecodeError):
            issues.append(
                UsageIssue("DDM_ANNOTATION_SOURCE_INVALID", "成功标注文件不是有效 JSON", location)
            )
            continue
        if not isinstance(document, list):
            issues.append(
                UsageIssue("DDM_ANNOTATION_SOURCE_INVALID", "成功标注文件不是条目数组", location)
            )
            continue
        normalized: list[dict[str, Any]] = []
        actions = tuple(video.get("actions", ()))
        for index, entry in enumerate(document):
            parsed = _normalize_annotation_entry(
                entry, actions=actions, location=f"{location}[{index}]"
            )
            if isinstance(parsed, UsageIssue):
                issues.append(parsed)
            else:
                normalized.append(parsed)
        source_video = dict(video)
        source_video["annotation_clips"] = normalized
        source_videos.append(source_video)
    if isinstance(snapshot, dict) and source_copies:
        snapshot["annotation_sources"] = source_copies
    if source_videos:
        source_snapshot = dict(snapshot)
        source_snapshot["videos"] = source_videos
        issues.extend(_validate_base_clips(source_snapshot))
        for source_video in source_videos:
            location = str(source_video.get("member_id", "video"))
            persisted_clips = next(
                (
                    item.get("annotation_clips")
                    for item in raw_videos
                    if isinstance(item, dict) and str(item.get("member_id")) == location
                ),
                None,
            )
            if not _clip_coverage_matches(source_video.get("annotation_clips"), persisted_clips):
                issues.append(
                    UsageIssue(
                        "DDM_ANNOTATION_SOURCE_CHANGED",
                        "只读标注文件与成功执行记录的时间覆盖不一致",
                        location,
                    )
                )
    return issues


def _normalize_annotation_entry(
    entry: object, *, actions: Sequence[object], location: str
) -> dict[str, Any] | UsageIssue:
    if not isinstance(entry, dict):
        return UsageIssue("DDM_ANNOTATION_SOURCE_INVALID", "标注条目不是对象", location)
    try:
        start = float(entry["start_timestamp"])
        end = float(entry["end_timestamp"])
    except (KeyError, TypeError, ValueError):
        return UsageIssue("DDM_ANNOTATION_SOURCE_INVALID", "标注条目缺少有效时间", location)
    if not math.isfinite(start) or not math.isfinite(end) or end <= start:
        return UsageIssue("DDM_ANNOTATION_SOURCE_INVALID", "标注条目时间无效", location)
    if "actions" in entry or "descriptions" in entry:
        raw_indices = entry.get("actions")
        raw_descriptions = entry.get("descriptions")
    else:
        raw_indices = entry.get("action")
        raw_descriptions = entry.get("description")
    indices = (
        raw_indices
        if isinstance(raw_indices, list)
        else ([] if raw_indices is None else [raw_indices])
    )
    descriptions = (
        raw_descriptions
        if isinstance(raw_descriptions, list)
        else ([] if raw_descriptions is None else [raw_descriptions])
    )
    if not descriptions:
        return UsageIssue("DDM_ANNOTATION_SOURCE_INVALID", "标注条目没有动作", location)
    if not indices:
        inferred: list[int] = []
        for description in descriptions:
            if not isinstance(description, str):
                return UsageIssue("DDM_ANNOTATION_SOURCE_INVALID", "标注动作描述为空", location)
            matches = [
                index + 1
                for index, action_name in enumerate(actions)
                if isinstance(action_name, str) and action_name == description
            ]
            if len(matches) != 1:
                return UsageIssue(
                    "DDM_ANNOTATION_SOURCE_MISMATCH", "标注动作与动作清单不一致", location
                )
            inferred.append(matches[0])
        indices = inferred
    if len(indices) != len(descriptions):
        return UsageIssue("DDM_ANNOTATION_SOURCE_INVALID", "标注条目动作字段无效", location)
    normalized_indices: list[int] = []
    normalized_descriptions: list[str] = []
    for action, description in zip(indices, descriptions, strict=True):
        if isinstance(action, bool) or not isinstance(action, int) or action <= 0:
            return UsageIssue("DDM_ANNOTATION_SOURCE_INVALID", "标注动作编号无效", location)
        if not isinstance(description, str) or not description.strip():
            return UsageIssue("DDM_ANNOTATION_SOURCE_INVALID", "标注动作描述为空", location)
        if "final segment" in description.casefold():
            return UsageIssue(
                "DDM_ANNOTATION_RESERVED_DESCRIPTION",
                "标注输出包含基座保留描述，不能生成制品",
                location,
            )
        action_index = action - 1
        if action_index >= len(actions) or actions[action_index] != description:
            return UsageIssue(
                "DDM_ANNOTATION_SOURCE_MISMATCH", "标注动作与动作清单不一致", location
            )
        normalized_indices.append(action_index)
        normalized_descriptions.append(description)
    return {
        "start_time": start,
        "end_time": end,
        "action_indices": normalized_indices,
        "action_descriptions": normalized_descriptions,
    }


def _clip_coverage_matches(actual: object, expected: object) -> bool:
    if not isinstance(actual, list) or not isinstance(expected, list):
        return False

    def ranges(value: list[object]) -> list[tuple[float, float, int, str]]:
        result: list[tuple[float, float, int, str]] = []
        for item in value:
            if not isinstance(item, dict):
                continue
            indices = item.get("action_indices")
            descriptions = item.get("action_descriptions")
            if indices is None:
                indices = [item.get("action_index")]
            if descriptions is None:
                descriptions = [item.get("action_description")]
            if not isinstance(indices, list) or not isinstance(descriptions, list):
                continue
            try:
                start = float(item["start_time"])
                end = float(item["end_time"])
                result.extend(
                    (start, end, int(index), str(description))
                    for index, description in zip(indices, descriptions, strict=True)
                )
            except (KeyError, TypeError, ValueError):
                continue
        return result

    actual_ranges = ranges(actual)
    expected_ranges = ranges(expected)
    return all(
        _interval_covered(
            [
                (start, end)
                for start, end, index, description in expected_ranges
                if index == action and description == text
            ],
            actual_start,
            actual_end,
        )
        for actual_start, actual_end, action, text in actual_ranges
    ) and all(
        _interval_covered(
            [
                (start, end)
                for start, end, index, description in actual_ranges
                if index == action and description == text
            ],
            expected_start,
            expected_end,
        )
        for expected_start, expected_end, action, text in expected_ranges
    )


def _vlm_workspace_filename(key: str) -> str:
    return f"vlm-{hashlib.sha256(key.encode('utf-8')).hexdigest()}.mp4"


def _verify_frozen_media(
    *,
    member_id: UUID,
    source: Mapping[str, Any],
    storage: ObjectStorage,
    media_probe: MediaProbe,
    annotation_volume: AnnotationDataVolume | None,
    directory: Path,
    issues: list[UsageIssue],
) -> None:
    """验证 VLM 引用的完整视频或固定标注切片，不读取候选任意路径。"""
    is_clip = source.get("annotation_execution_id") is not None
    if not is_clip and (
        not isinstance(source.get("object_key"), str)
        or not source.get("object_key")
        or not isinstance(source.get("object_version_id"), str)
        or not source.get("object_version_id")
    ):
        issues.append(UsageIssue("USAGE_SOURCE_MISSING", "媒体对象身份不完整", str(member_id)))
        return
    key = source.get("key")
    path = directory / _vlm_workspace_filename(
        key if isinstance(key, str) and key else str(member_id)
    )
    try:
        with path.open("wb") as destination:
            if is_clip:
                data_id = source.get("upstream_data_id")
                video_id = source.get("upstream_video_id")
                filename = source.get("clip_filename")
                if not (
                    isinstance(data_id, str)
                    and data_id
                    and isinstance(video_id, str)
                    and video_id
                    and isinstance(filename, str)
                    and filename
                ):
                    issues.append(
                        UsageIssue(
                            "USAGE_SOURCE_MISSING", "标注切片缺少基座文件身份", str(member_id)
                        )
                    )
                    return
                if annotation_volume is None:
                    issues.append(
                        UsageIssue(
                            "USAGE_ANNOTATION_VOLUME_UNAVAILABLE",
                            "标注数据卷暂时不可用，请稍后重试",
                            str(member_id),
                            retryable=True,
                            recovery_action="retry_usage_check",
                        )
                    )
                    return
                annotation_volume.read_video(
                    data_id=data_id,
                    video_id=video_id,
                    filename=filename,
                    destination=destination,
                )
            else:
                storage.download_to(
                    object_key=str(source["object_key"]),
                    destination=destination,
                )
        if is_clip:
            expected_digest = source.get("derived_video_sha256")
            if (
                not isinstance(expected_digest, str)
                or len(expected_digest) != 64
                or any(character not in "0123456789abcdef" for character in expected_digest.lower())
            ):
                issues.append(
                    UsageIssue(
                        "USAGE_SOURCE_DIGEST_MISSING",
                        "标注切片缺少有效冻结摘要，不能验证固定输入",
                        str(member_id),
                    )
                )
                return
            if _file_sha256(path) != expected_digest:
                issues.append(
                    UsageIssue(
                        "USAGE_SOURCE_DIGEST_MISMATCH",
                        "标注切片与冻结摘要不一致",
                        str(member_id),
                    )
                )
                return
            expected_size = source.get("derived_video_size")
            if not isinstance(expected_size, int) or expected_size < 0:
                issues.append(
                    UsageIssue(
                        "USAGE_SOURCE_SIZE_MISSING",
                        "标注切片缺少有效冻结大小，不能验证固定输入",
                        str(member_id),
                    )
                )
                return
            if path.stat().st_size != expected_size:
                issues.append(
                    UsageIssue("USAGE_OBJECT_SIZE_CHANGED", "下载媒体大小已变化", str(member_id))
                )
                return
        elif _file_sha256(path) != source.get("source_sha256"):
            issues.append(
                UsageIssue(
                    "USAGE_SOURCE_DIGEST_MISMATCH", "下载的媒体与冻结摘要不一致", str(member_id)
                )
            )
            return
        metadata = media_probe.probe(str(path))
    except AnnotationDataVolumeInputError:
        issues.append(
            UsageIssue(
                "VLM_MEDIA_SOURCE_UNAVAILABLE",
                "标注切片文件不存在或映射无效",
                str(member_id),
            )
        )
        return
    except AnnotationDataVolumeReadError:
        issues.append(
            UsageIssue(
                "VLM_MEDIA_SOURCE_UNAVAILABLE",
                "标注切片文件暂时不可读取",
                str(member_id),
                retryable=True,
                recovery_action="retry_usage_check",
            )
        )
        return
    except AnnotationDataVolumeUnavailableError:
        issues.append(
            UsageIssue(
                "VLM_MEDIA_SOURCE_UNAVAILABLE",
                "标注切片文件不存在或不可读取",
                str(member_id),
                retryable=True,
                recovery_action="retry_usage_check",
            )
        )
        return
    except ObjectNotFoundError:
        issues.append(UsageIssue("USAGE_OBJECT_NOT_FOUND", "媒体对象不存在", str(member_id)))
        return
    except ObjectStorageUnavailableError:
        issues.append(
            UsageIssue(
                "USAGE_STORAGE_UNAVAILABLE",
                "训练素材存储暂时不可用",
                str(member_id),
                retryable=True,
                recovery_action="retry_usage_check",
            )
        )
        return
    except MediaProbeUnavailableError:
        issues.append(
            UsageIssue(
                "USAGE_MEDIA_PROBE_UNAVAILABLE",
                "媒体探测工具暂时不可用",
                str(member_id),
                retryable=True,
                recovery_action="retry_usage_check",
            )
        )
        return
    except InvalidMediaError:
        issues.append(UsageIssue("USAGE_MEDIA_INVALID", "媒体无法读取为有效视频", str(member_id)))
        return
    expected_size = source.get("derived_video_size") if is_clip else source.get("actual_size")
    if not is_clip and isinstance(expected_size, int) and path.stat().st_size != expected_size:
        issues.append(UsageIssue("USAGE_OBJECT_SIZE_CHANGED", "下载媒体大小已变化", str(member_id)))
    expected_duration = (
        float(source["clip_end_time"]) - float(source["clip_start_time"])
        if is_clip
        and source.get("clip_start_time") is not None
        and source.get("clip_end_time") is not None
        else source.get("duration_seconds")
    )
    if (
        isinstance(expected_duration, (int, float))
        and abs(metadata.duration_seconds - expected_duration) > 1e-3
    ):
        issues.append(UsageIssue("USAGE_DURATION_CHANGED", "媒体时长已变化", str(member_id)))
    if isinstance(source.get("codec"), str) and metadata.codec != source["codec"]:
        issues.append(UsageIssue("USAGE_CODEC_CHANGED", "媒体编码已变化", str(member_id)))
    if isinstance(source.get("container"), str) and metadata.container != source["container"]:
        issues.append(UsageIssue("USAGE_CONTAINER_CHANGED", "媒体容器已变化", str(member_id)))


def _append_ddm_sampling_facts_issue(
    metadata: MediaMetadata, location: str, issues: list[UsageIssue]
) -> bool:
    """要求 DDM 采样所需的真实帧率和帧数事实可用。"""
    if (
        metadata.fps is None
        or not math.isfinite(metadata.fps)
        or metadata.fps <= 0
        or metadata.frame_count is None
        or metadata.frame_count < 3
    ):
        issues.append(
            UsageIssue(
                "DDM_SAMPLING_FACTS_MISSING",
                "无法确认视频帧率或帧数，不能验证训练采样契约",
                location,
            )
        )
        return True
    return False


def _probe_frozen_video(
    *,
    member_id: UUID,
    source: Mapping[str, Any],
    storage: ObjectStorage,
    media_probe: MediaProbe,
    directory: Path,
    issues: list[UsageIssue],
) -> None:
    path = directory / f"{member_id}.mp4"
    object_key = source.get("object_key")
    object_version_id = source.get("object_version_id")
    if (
        not isinstance(object_key, str)
        or not object_key
        or not isinstance(object_version_id, str)
        or not object_version_id
    ):
        issues.append(UsageIssue("USAGE_SOURCE_MISSING", "源视频对象身份不完整", str(member_id)))
        return
    try:
        with path.open("wb") as destination:
            storage.download_to(
                object_key=object_key,
                destination=destination,
            )
        actual_digest = _file_sha256(path)
        expected_digest = source.get("source_sha256")
        if isinstance(expected_digest, str) and actual_digest != expected_digest:
            issues.append(
                UsageIssue(
                    "USAGE_SOURCE_DIGEST_MISMATCH",
                    "下载的源对象与冻结摘要不一致",
                    str(member_id),
                )
            )
            return
        metadata = media_probe.probe(str(path))
        if _append_ddm_sampling_facts_issue(metadata, str(member_id), issues):
            return
    except ObjectNotFoundError:
        issues.append(UsageIssue("USAGE_OBJECT_NOT_FOUND", "源对象不存在", str(member_id)))
        return
    except ObjectStorageUnavailableError:
        issues.append(
            UsageIssue(
                "USAGE_STORAGE_UNAVAILABLE",
                "训练素材存储暂时不可用",
                str(member_id),
                retryable=True,
                recovery_action="retry_usage_check",
            )
        )
        return
    except MediaProbeUnavailableError:
        issues.append(
            UsageIssue(
                "USAGE_MEDIA_PROBE_UNAVAILABLE",
                "媒体探测工具暂时不可用",
                str(member_id),
                retryable=True,
                recovery_action="retry_usage_check",
            )
        )
        return
    except InvalidMediaError:
        issues.append(
            UsageIssue("USAGE_MEDIA_INVALID", "冻结源对象无法读取为有效视频", str(member_id))
        )
        return
    expected_size = source.get("actual_size")
    if isinstance(expected_size, int) and path.stat().st_size != expected_size:
        issues.append(
            UsageIssue("USAGE_OBJECT_SIZE_CHANGED", "下载源对象大小已变化", str(member_id))
        )
    expected_duration = source.get("duration_seconds")
    if (
        isinstance(expected_duration, (int, float))
        and abs(metadata.duration_seconds - expected_duration) > 1e-3
    ):
        issues.append(UsageIssue("USAGE_DURATION_CHANGED", "源视频时长已变化", str(member_id)))
    if isinstance(source.get("codec"), str) and metadata.codec != source["codec"]:
        issues.append(UsageIssue("USAGE_CODEC_CHANGED", "源视频编码已变化", str(member_id)))
    if isinstance(source.get("container"), str) and metadata.container != source["container"]:
        issues.append(UsageIssue("USAGE_CONTAINER_CHANGED", "源视频容器已变化", str(member_id)))


def _with_snapshot(
    result: UsageValidationResult,
    snapshot: dict[str, Any],
    extra_issues: tuple[UsageIssue, ...],
) -> UsageValidationResult:
    issues = tuple(extra_issues) + tuple(result.issues)
    summary = dict(result.summary)
    return UsageValidationResult(
        passed=not issues,
        issues=issues,
        input_snapshot=snapshot,
        input_digest=_digest_snapshot(snapshot),
        summary=summary,
    )


def _failed_result(
    *, snapshot: Mapping[str, Any], summary: dict[str, int], issue: UsageIssue
) -> UsageValidationResult:
    return UsageValidationResult(
        passed=False,
        issues=(issue,),
        input_snapshot=dict(snapshot),
        input_digest=_digest_snapshot(snapshot),
        summary=summary,
    )


def _digest_snapshot(snapshot: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(snapshot)).hexdigest()


def _issue_dict(issue: UsageIssue) -> dict[str, Any]:
    return {
        "code": issue.code,
        "detail": issue.detail,
        "location": issue.location,
        "retryable": issue.retryable,
        "recovery_action": issue.recovery_action,
    }


def _issue_from_mapping(value: Mapping[str, Any]) -> UsageIssue:
    retryable = value.get("retryable", False)
    retryable = retryable if isinstance(retryable, bool) else False
    recovery_action = value.get("recovery_action")
    if not isinstance(recovery_action, str):
        recovery_action = "retry_usage_check" if retryable else "fix_input"
    return UsageIssue(
        code=str(value.get("code", "USAGE_INPUT_INVALID")),
        detail=str(value.get("detail", "用途输入无效")),
        location=str(value.get("location", "input")),
        retryable=retryable,
        recovery_action=recovery_action,
    )


def _candidate_for_check(
    dataset_id: UUID, candidate_id: UUID | None, datasets: DatasetRepository
) -> VlmCandidate:
    if candidate_id is None:
        raise DatasetRefusedError(
            DatasetRefusalCode.VLM_CANDIDATE_NOT_FOUND,
            detail="VLM 检查必须指定候选修订",
        )
    candidate = datasets.vlm_candidate_by_id(candidate_id)
    if candidate is None:
        raise DatasetRefusedError(DatasetRefusalCode.VLM_CANDIDATE_NOT_FOUND)
    if candidate.dataset_id != dataset_id:
        raise DatasetRefusedError(DatasetRefusalCode.RESOURCE_MISMATCH)
    return candidate


def _require_dataset(dataset_id: UUID, datasets: DatasetRepository) -> None:
    if datasets.dataset_by_id(dataset_id) is None:
        raise DatasetRefusedError(DatasetRefusalCode.DATASET_NOT_FOUND)
