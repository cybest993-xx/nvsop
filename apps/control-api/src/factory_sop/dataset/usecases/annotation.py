"""训练视频动作清单、标注上下文和切片候选的用例。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import re
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, BinaryIO, cast
from uuid import UUID

from factory_sop.auth.api import Caller, Permission, authorize
from factory_sop.dataset.annotation import (
    AnnotationBackend,
    AnnotationBackendExecutionError,
    AnnotationBackendUnavailableError,
    PreparedAnnotationVideo,
)
from factory_sop.dataset.errors import (
    DatasetFieldError,
    DatasetRefusalCode,
    DatasetRefusedError,
)
from factory_sop.dataset.media import InvalidMediaError, MediaProbe, MediaProbeUnavailableError
from factory_sop.dataset.model import (
    ActionListRevision,
    AnnotationContext,
    AnnotationExecution,
    AnnotationExecutionStatus,
    AnnotationMode,
    AnnotationSegment,
    AnnotationSubmission,
    DatasetMember,
    MemberStatus,
)
from factory_sop.dataset.repository import DatasetRepository
from factory_sop.dataset.storage import (
    ObjectNotFoundError,
    ObjectStorage,
    ObjectStorageUnavailableError,
)
from factory_sop.identifiers import new_id
from factory_sop.job.api import AnnotationJobQueue, ApplicationJob

_ACTION_RE = re.compile(r"^\((\d+)\).+")
_ALLOWED_SEGMENT_FIELDS = frozenset(
    {"start", "end", "actionIndex", "action_index", "actionDescription", "action_description"}
)
_TOKEN_VERSION = 2
_MAX_IDEMPOTENCY_KEY_LENGTH = 255


class AnnotationRefusedError(DatasetRefusedError):
    """标注用例拒绝了输入或当前状态。"""


@dataclass(frozen=True, slots=True)
class DecodedAnnotationContextToken:
    """验证签名后的上下文载荷。"""

    context_id: UUID
    dataset_id: UUID
    member_id: UUID
    action_list_revision: int
    annotation_revision: int
    source_object_version_id: str
    source_sha256: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class AnnotationSubmissionResult:
    """一次稳定业务提交及其当前执行代次和异步任务。"""

    submission: AnnotationSubmission
    execution: AnnotationExecution
    job: ApplicationJob


@dataclass(frozen=True, slots=True)
class AnnotationExecutionTarget:
    """worker 获得的标注执行快照；外部调用完成后按租约写回。"""

    job: ApplicationJob
    submission: AnnotationSubmission
    execution: AnnotationExecution
    member: DatasetMember
    context: AnnotationContext
    actions: ActionListRevision


@dataclass(frozen=True, slots=True)
class AnnotationContextPreparationTarget:
    """worker 获得的标注上下文准备快照。"""

    job: ApplicationJob
    context: AnnotationContext
    member: DatasetMember
    actions: ActionListRevision


@dataclass(frozen=True, slots=True)
class PreparedAnnotationCopy:
    """一次基座工作副本及其可核对的派生媒体事实。"""

    prepared: PreparedAnnotationVideo
    size: int
    sha256: str
    duration_seconds: float


def register_action_list(
    *,
    dataset_id: UUID,
    actions: Sequence[str],
    caller: Caller,
    now: datetime,
    datasets: DatasetRepository,
) -> ActionListRevision:
    """追加动作清单修订，旧修订保持可读。"""
    authorize(caller, Permission.DATASET_EDIT)
    _require_dataset(dataset_id=dataset_id, datasets=datasets)
    normalized = _normalize_actions(actions)
    latest = datasets.latest_action_list(dataset_id)
    revision = 1 if latest is None else latest.revision + 1
    value = ActionListRevision(
        dataset_id=dataset_id,
        revision=revision,
        actions=normalized,
        created_by=caller.user.id,
        created_at=now,
    )
    datasets.add_action_list(value)
    return value


def create_annotation_context(
    *,
    dataset_id: UUID,
    member_id: UUID,
    caller: Caller,
    now: datetime,
    datasets: DatasetRepository,
    secret: str,
    ttl_seconds: int,
    action_list_revision: int | None = None,
) -> AnnotationContext:
    """为已登记视频签发绑定源对象代次的短期上下文。"""
    authorize(caller, Permission.DATASET_VIEW)
    authorize(caller, Permission.DATASET_EDIT)
    _member_for_dataset(dataset_id=dataset_id, member_id=member_id, datasets=datasets)
    member = datasets.lock_annotation_member(member_id)
    if member is None:
        raise AnnotationRefusedError(DatasetRefusalCode.MEMBER_NOT_FOUND)
    _require_registered_member(member)
    actions = (
        datasets.action_list_by_revision(
            dataset_id=dataset_id,
            revision=action_list_revision,
        )
        if action_list_revision is not None
        else datasets.latest_action_list(dataset_id)
    )
    if actions is None:
        raise AnnotationRefusedError(
            DatasetRefusalCode.ACTION_LIST_NOT_FOUND,
            detail="请先登记一份动作清单",
        )
    source_version = member.object_version_id
    source_sha256 = member.actual_sha256
    previous_submissions = datasets.list_annotation_submissions(
        dataset_id=dataset_id,
        member_id=member_id,
    )
    annotation_revision = max(
        (submission.revision for submission in previous_submissions),
        default=0,
    )
    if not source_version or not source_sha256:
        raise AnnotationRefusedError(
            DatasetRefusalCode.ANNOTATION_CONTEXT_INVALID,
            detail="视频缺少已确认的对象代次或摘要",
        )
    context = AnnotationContext(
        id=new_id(),
        dataset_id=dataset_id,
        member_id=member_id,
        action_list_revision=actions.revision,
        annotation_revision=annotation_revision,
        source_object_version_id=source_version,
        source_sha256=source_sha256,
        created_by=caller.user.id,
        created_at=now,
        expires_at=now + timedelta(seconds=ttl_seconds),
    )
    context = replace(
        context,
        token=encode_annotation_context_token(context, secret=secret),
    )
    datasets.add_annotation_context(context)
    return context


class AnnotationGatewayResourceKind(StrEnum):
    """Nginx 预检需要核对的资源种类。"""

    UNSCOPED = "unscoped"
    INVALID = "invalid"
    DATASET = "dataset"
    MEMBER = "member"
    SUBMISSION = "submission"
    CONTEXT = "context"
    COMPATIBILITY_VIDEO = "compatibility_video"


@dataclass(frozen=True, slots=True)
class AnnotationGatewayResource:
    """HTTP 适配层解析出的资源目标，不携带原始 URI。"""

    kind: AnnotationGatewayResourceKind
    dataset_id: UUID | None = None
    member_id: UUID | None = None
    submission_id: UUID | None = None
    execution_id: UUID | None = None
    clip_index: int | None = None
    context_token: str | None = None


class AnnotationMediaKind(StrEnum):
    """标注媒体资源类型。"""

    VIDEO = "video"
    CLIP = "clip"
    ARCHIVE = "archive"


@dataclass(frozen=True, slots=True)
class AnnotationMediaAuthorization:
    """媒体授权后可传给基座的派生资源身份。"""

    upstream_video_id: str
    upstream_clip_id: str | None = None


@dataclass(frozen=True, slots=True)
class PreparedAnnotationContext:
    """已完成基座副本准备的上下文及其读取资料。"""

    context: AnnotationContext
    member: DatasetMember
    actions: ActionListRevision


def authorize_annotation_gateway_resource(
    *,
    resource: AnnotationGatewayResource,
    permission: Permission,
    caller: Caller,
    now: datetime,
    datasets: DatasetRepository,
    secret: str,
) -> None:
    """校验网关预检的权限和资源归属。"""
    authorize(caller, permission)
    if resource.kind is AnnotationGatewayResourceKind.UNSCOPED:
        return
    if resource.kind is AnnotationGatewayResourceKind.INVALID:
        raise DatasetRefusedError(DatasetRefusalCode.RESOURCE_MISMATCH)
    if resource.kind is AnnotationGatewayResourceKind.DATASET:
        if resource.dataset_id is None:
            raise DatasetRefusedError(DatasetRefusalCode.RESOURCE_MISMATCH)
        _require_dataset(dataset_id=resource.dataset_id, datasets=datasets)
        return
    if resource.kind is AnnotationGatewayResourceKind.MEMBER:
        _require_member_resource(resource=resource, datasets=datasets)
        return
    if resource.kind is AnnotationGatewayResourceKind.SUBMISSION:
        _require_submission_resource(resource=resource, caller=caller, datasets=datasets)
        return
    if resource.kind in {
        AnnotationGatewayResourceKind.CONTEXT,
        AnnotationGatewayResourceKind.COMPATIBILITY_VIDEO,
    }:
        if resource.context_token is None:
            raise DatasetRefusedError(DatasetRefusalCode.RESOURCE_MISMATCH)
        read_annotation_context(
            token=resource.context_token,
            caller=caller,
            now=now,
            datasets=datasets,
            secret=secret,
        )
        return
    raise AssertionError(f"未处理的网关资源种类: {resource.kind!r}")


def authorize_annotation_media_resource(
    *,
    kind: AnnotationMediaKind,
    resource: str,
    caller: Caller,
    now: datetime,
    datasets: DatasetRepository,
    secret: str,
) -> AnnotationMediaAuthorization:
    """校验媒体资源并返回只用于派生请求的基座身份。"""
    if kind is AnnotationMediaKind.VIDEO:
        prepared = read_prepared_annotation_context(
            token=resource,
            caller=caller,
            now=now,
            datasets=datasets,
            secret=secret,
        )
        assert prepared.context.upstream_video_id is not None
        return AnnotationMediaAuthorization(
            upstream_video_id=prepared.context.upstream_video_id,
        )
    parts = resource.split(":")
    expected_parts = 3 if kind is AnnotationMediaKind.CLIP else 2
    if len(parts) != expected_parts:
        raise AnnotationRefusedError(DatasetRefusalCode.RESOURCE_MISMATCH)
    try:
        submission_id = UUID(parts[0])
        execution_id = UUID(parts[1])
        clip_index = int(parts[2]) if kind is AnnotationMediaKind.CLIP else None
    except (ValueError, IndexError) as error:
        raise AnnotationRefusedError(DatasetRefusalCode.RESOURCE_MISMATCH) from error
    execution = _annotation_execution_resource(
        submission_id=submission_id,
        execution_id=execution_id,
        caller=caller,
        datasets=datasets,
    )
    if execution.upstream_video_id is None:
        raise AnnotationRefusedError(DatasetRefusalCode.ANNOTATION_CONTEXT_INVALID)
    clip_id = None
    if kind is AnnotationMediaKind.CLIP:
        assert clip_index is not None
        clip_id = str(
            _annotation_clip_resource(
                submission_id=submission_id,
                execution_id=execution_id,
                clip_index=clip_index,
                caller=caller,
                datasets=datasets,
            )["id"]
        )
    return AnnotationMediaAuthorization(
        upstream_video_id=execution.upstream_video_id,
        upstream_clip_id=clip_id,
    )


def read_prepared_annotation_context(
    *,
    token: str,
    caller: Caller,
    now: datetime,
    datasets: DatasetRepository,
    secret: str,
) -> PreparedAnnotationContext:
    """读取兼容媒体路径所需的已准备上下文。"""
    context = read_annotation_context(
        token=token,
        caller=caller,
        now=now,
        datasets=datasets,
        secret=secret,
    )
    member = datasets.member_by_id(context.member_id)
    actions = datasets.action_list_by_revision(
        dataset_id=context.dataset_id,
        revision=context.action_list_revision,
    )
    if member is None or actions is None:
        raise AnnotationRefusedError(DatasetRefusalCode.ANNOTATION_CONTEXT_INVALID)
    if not _context_is_prepared(context):
        _raise_context_preparation_state(context)
    return PreparedAnnotationContext(context=context, member=member, actions=actions)


def _require_member_resource(
    *, resource: AnnotationGatewayResource, datasets: DatasetRepository
) -> None:
    if resource.dataset_id is None or resource.member_id is None:
        raise DatasetRefusedError(DatasetRefusalCode.RESOURCE_MISMATCH)
    _require_dataset(dataset_id=resource.dataset_id, datasets=datasets)
    member = datasets.member_by_id(resource.member_id)
    if member is None or member.dataset_id != resource.dataset_id:
        raise DatasetRefusedError(DatasetRefusalCode.RESOURCE_MISMATCH)


def _require_submission_resource(
    *,
    resource: AnnotationGatewayResource,
    caller: Caller,
    datasets: DatasetRepository,
) -> None:
    if resource.submission_id is None:
        raise DatasetRefusedError(DatasetRefusalCode.RESOURCE_MISMATCH)
    if (resource.dataset_id is None) != (resource.member_id is None):
        raise DatasetRefusedError(DatasetRefusalCode.RESOURCE_MISMATCH)
    if resource.dataset_id is not None and resource.member_id is not None:
        _require_member_resource(resource=resource, datasets=datasets)
    submission = read_annotation_submission(
        submission_id=resource.submission_id,
        caller=caller,
        datasets=datasets,
    )
    if resource.dataset_id is not None and (
        submission.dataset_id != resource.dataset_id or submission.member_id != resource.member_id
    ):
        raise DatasetRefusedError(DatasetRefusalCode.RESOURCE_MISMATCH)
    if resource.execution_id is not None:
        _gateway_execution_resource(
            submission_id=submission.id,
            execution_id=resource.execution_id,
            clip_index=resource.clip_index,
            datasets=datasets,
        )


def _gateway_execution_resource(
    *,
    submission_id: UUID,
    execution_id: UUID,
    datasets: DatasetRepository,
    clip_index: int | None = None,
) -> AnnotationExecution:
    execution = datasets.annotation_execution_by_id(execution_id)
    if execution is None or execution.submission_id != submission_id:
        raise DatasetRefusedError(DatasetRefusalCode.RESOURCE_MISMATCH)
    if clip_index is not None and (clip_index < 0 or clip_index >= len(execution.clips)):
        raise DatasetRefusedError(DatasetRefusalCode.RESOURCE_MISMATCH)
    return execution


def _annotation_execution_resource(
    *,
    submission_id: UUID,
    execution_id: UUID,
    caller: Caller,
    datasets: DatasetRepository,
) -> AnnotationExecution:
    execution = read_annotation_execution(
        submission_id=submission_id,
        execution_id=execution_id,
        caller=caller,
        datasets=datasets,
    )
    if execution.status is not AnnotationExecutionStatus.SUCCEEDED:
        raise AnnotationRefusedError(
            DatasetRefusalCode.ANNOTATION_STATE_CONFLICT,
            detail="切片尚未完成，暂时不能下载",
        )
    return execution


def _annotation_clip_resource(
    *,
    submission_id: UUID,
    execution_id: UUID,
    clip_index: int,
    caller: Caller,
    datasets: DatasetRepository,
) -> dict[str, Any]:
    execution = _annotation_execution_resource(
        submission_id=submission_id,
        execution_id=execution_id,
        caller=caller,
        datasets=datasets,
    )
    if clip_index < 0 or clip_index >= len(execution.clips):
        raise AnnotationRefusedError(DatasetRefusalCode.RESOURCE_MISMATCH)
    clip = execution.clips[clip_index]
    if not isinstance(clip, dict) or not clip.get("id"):
        raise AnnotationRefusedError(DatasetRefusalCode.ANNOTATION_CONTEXT_INVALID)
    return clip


def _context_is_prepared(context: AnnotationContext) -> bool:
    return (
        context.preparation_status == "succeeded"
        and context.upstream_video_id is not None
        and context.upstream_data_id is not None
        and context.upstream_video_size is not None
        and context.upstream_video_sha256 is not None
        and context.upstream_video_duration_seconds is not None
    )


def _raise_context_preparation_state(context: AnnotationContext) -> None:
    """把异步准备状态转换为兼容路径的明确失败。"""
    if context.preparation_status == "failed":
        try:
            code = DatasetRefusalCode(context.preparation_failure_code or "")
        except ValueError:
            code = DatasetRefusalCode.ANNOTATION_BACKEND_UNAVAILABLE
        raise AnnotationRefusedError(
            code,
            detail=context.preparation_failure_detail or "标注媒体准备失败",
        )
    raise AnnotationRefusedError(
        DatasetRefusalCode.ANNOTATION_STATE_CONFLICT,
        detail="标注媒体仍在准备，请稍后重试",
    )


def begin_annotation_context_preparation(
    *,
    job: ApplicationJob,
    datasets: DatasetRepository,
) -> AnnotationContextPreparationTarget | None:
    """领取一条上下文准备租约；重复投递只允许一个 worker 前进。"""
    context = datasets.annotation_context_by_id(job.attempt_id)
    if (
        context is None
        or context.preparation_job_id != job.id
        or context.member_id != job.member_id
        or context.preparation_status not in {"pending", "running"}
    ):
        return None
    member = datasets.member_by_id(context.member_id)
    actions = datasets.action_list_by_revision(
        dataset_id=context.dataset_id,
        revision=context.action_list_revision,
    )
    if member is None or actions is None:
        return None
    running = replace(
        context,
        preparation_status="running",
        preparation_failure_code=None,
        preparation_failure_detail=None,
    )
    datasets.save_annotation_context(running)
    return AnnotationContextPreparationTarget(
        job=job,
        context=running,
        member=member,
        actions=actions,
    )


def complete_annotation_context_preparation(
    *,
    target: AnnotationContextPreparationTarget,
    prepared: PreparedAnnotationCopy,
    datasets: DatasetRepository,
) -> AnnotationContext:
    """登记已核验的上下文基座副本；只写回仍处于运行中的准备任务。"""
    current = datasets.annotation_context_by_id(target.context.id)
    member = datasets.lock_annotation_member(target.context.member_id)
    if (
        current is None
        or member is None
        or current.preparation_job_id != target.job.id
        or current.preparation_status != "running"
        or member.dataset_id != target.context.dataset_id
        or member.object_version_id != target.context.source_object_version_id
        or member.actual_sha256 != target.context.source_sha256
    ):
        raise AnnotationRefusedError(
            DatasetRefusalCode.ANNOTATION_STATE_CONFLICT,
            detail="标注上下文准备租约已失效",
        )
    _require_registered_member(member)
    updated = replace(
        current,
        upstream_data_id=prepared.prepared.data_id,
        upstream_video_id=prepared.prepared.video_id,
        upstream_video_size=prepared.size,
        upstream_video_sha256=prepared.sha256,
        upstream_video_duration_seconds=prepared.duration_seconds,
        preparation_status="succeeded",
        preparation_failure_code=None,
        preparation_failure_detail=None,
    )
    datasets.save_annotation_context(updated)
    return updated


def fail_annotation_context_preparation(
    *,
    target: AnnotationContextPreparationTarget,
    code: str,
    detail: str,
    datasets: DatasetRepository,
) -> AnnotationContext:
    """记录上下文准备失败，不伪造可播放的基座身份。"""
    current = datasets.annotation_context_by_id(target.context.id)
    if (
        current is None
        or current.preparation_job_id != target.job.id
        or current.preparation_status != "running"
    ):
        raise AnnotationRefusedError(
            DatasetRefusalCode.ANNOTATION_STATE_CONFLICT,
            detail="标注上下文准备租约已失效",
        )
    updated = replace(
        current,
        upstream_data_id=None,
        upstream_video_id=None,
        upstream_video_size=None,
        upstream_video_sha256=None,
        upstream_video_duration_seconds=None,
        preparation_status="failed",
        preparation_failure_code=code,
        preparation_failure_detail=detail,
    )
    datasets.save_annotation_context(updated)
    return updated


def encode_annotation_context_token(context: AnnotationContext, *, secret: str) -> str:
    """签发不可读上下文身份；签名载荷同时绑定精确源对象。"""
    payload = {
        "v": _TOKEN_VERSION,
        "id": str(context.id),
        "dataset_id": str(context.dataset_id),
        "member_id": str(context.member_id),
        "action_list_revision": context.action_list_revision,
        "annotation_revision": context.annotation_revision,
        "source_object_version_id": context.source_object_version_id,
        "source_sha256": context.source_sha256,
        "expires_at": context.expires_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
    }
    encoded = _b64url(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    )
    signature = _b64url(hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest())
    return f"{encoded}.{signature}"


def decode_annotation_context_token(
    token: str,
    *,
    secret: str,
    now: datetime,
) -> DecodedAnnotationContextToken:
    """验证上下文签名、版本和有效期；任何不确定值都拒绝。"""
    try:
        encoded, signature = token.split(".", 1)
        expected = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest()
        supplied = _decode_b64url(signature)
        if not hmac.compare_digest(expected, supplied):
            raise ValueError("signature")
        raw = json.loads(_decode_b64url(encoded))
        if not isinstance(raw, dict) or raw.get("v") != _TOKEN_VERSION:
            raise ValueError("version")
        expires_at = datetime.fromisoformat(str(raw["expires_at"]).replace("Z", "+00:00"))
        decoded = DecodedAnnotationContextToken(
            context_id=UUID(str(raw["id"])),
            dataset_id=UUID(str(raw["dataset_id"])),
            member_id=UUID(str(raw["member_id"])),
            action_list_revision=int(raw["action_list_revision"]),
            annotation_revision=int(raw["annotation_revision"]),
            source_object_version_id=str(raw["source_object_version_id"]),
            source_sha256=str(raw["source_sha256"]),
            expires_at=expires_at,
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError, UnicodeError) as error:
        raise AnnotationRefusedError(
            DatasetRefusalCode.ANNOTATION_CONTEXT_INVALID,
            detail="标注上下文签名无效",
        ) from error
    if decoded.expires_at <= now.astimezone(UTC):
        raise AnnotationRefusedError(
            DatasetRefusalCode.ANNOTATION_CONTEXT_INVALID,
            detail="标注上下文已过期",
        )
    return decoded


def resolve_annotation_context(
    *,
    token: str,
    secret: str,
    now: datetime,
    datasets: DatasetRepository,
    dataset_id: UUID | None = None,
    member_id: UUID | None = None,
) -> AnnotationContext:
    """验证签名载荷与数据库绑定，防止上下文被换到另一视频。"""
    decoded = decode_annotation_context_token(token, secret=secret, now=now)
    if dataset_id is not None and decoded.dataset_id != dataset_id:
        raise AnnotationRefusedError(
            DatasetRefusalCode.RESOURCE_MISMATCH, detail="上下文不属于该数据集"
        )
    if member_id is not None and decoded.member_id != member_id:
        raise AnnotationRefusedError(
            DatasetRefusalCode.RESOURCE_MISMATCH, detail="上下文不属于该视频"
        )
    stored = datasets.annotation_context_by_id(decoded.context_id)
    if stored is None:
        raise AnnotationRefusedError(
            DatasetRefusalCode.ANNOTATION_CONTEXT_INVALID,
            detail="标注上下文不存在",
        )
    if (
        stored.dataset_id != decoded.dataset_id
        or stored.member_id != decoded.member_id
        or stored.action_list_revision != decoded.action_list_revision
        or stored.annotation_revision != decoded.annotation_revision
        or stored.source_object_version_id != decoded.source_object_version_id
        or stored.source_sha256 != decoded.source_sha256
        or stored.expires_at != decoded.expires_at
    ):
        raise AnnotationRefusedError(
            DatasetRefusalCode.ANNOTATION_CONTEXT_INVALID,
            detail="标注上下文绑定内容已变化",
        )
    return stored


def read_action_list(
    *,
    dataset_id: UUID,
    caller: Caller,
    datasets: DatasetRepository,
) -> ActionListRevision:
    """读取数据集最新动作清单。"""
    authorize(caller, Permission.DATASET_VIEW)
    _require_dataset(dataset_id=dataset_id, datasets=datasets)
    value = datasets.latest_action_list(dataset_id)
    if value is None:
        raise AnnotationRefusedError(DatasetRefusalCode.ACTION_LIST_NOT_FOUND)
    return value


def list_action_lists(
    *,
    dataset_id: UUID,
    caller: Caller,
    datasets: DatasetRepository,
) -> Sequence[ActionListRevision]:
    """读取动作清单历史；修订内容不被折叠。"""
    authorize(caller, Permission.DATASET_VIEW)
    _require_dataset(dataset_id=dataset_id, datasets=datasets)
    # 仓储暂时只需提供最新与指定读取；revision 是有限、连续的历史键。
    latest = datasets.latest_action_list(dataset_id)
    if latest is None:
        return []
    rows = [
        value
        for revision in range(1, latest.revision + 1)
        if (value := datasets.action_list_by_revision(dataset_id=dataset_id, revision=revision))
        is not None
    ]
    return rows


def read_annotation_context(
    *,
    token: str,
    caller: Caller,
    now: datetime,
    datasets: DatasetRepository,
    secret: str,
) -> AnnotationContext:
    """在查看权限下读取签名上下文及其绑定。"""
    authorize(caller, Permission.DATASET_VIEW)
    context = resolve_annotation_context(
        token=token,
        secret=secret,
        now=now,
        datasets=datasets,
    )
    member = datasets.member_by_id(context.member_id)
    if (
        member is None
        or member.dataset_id != context.dataset_id
        or member.status != MemberStatus.REGISTERED
        or member.object_version_id != context.source_object_version_id
        or member.actual_sha256 != context.source_sha256
    ):
        raise AnnotationRefusedError(
            DatasetRefusalCode.ANNOTATION_CONTEXT_INVALID,
            detail="视频对象已变化，请重新打开标注上下文",
        )
    if (
        datasets.action_list_by_revision(
            dataset_id=context.dataset_id,
            revision=context.action_list_revision,
        )
        is None
    ):
        raise AnnotationRefusedError(DatasetRefusalCode.ANNOTATION_CONTEXT_INVALID)
    return context


def read_annotation_submission(
    *,
    submission_id: UUID,
    caller: Caller,
    datasets: DatasetRepository,
) -> AnnotationSubmission:
    """读取一项标注业务提交的不可变输入。"""
    authorize(caller, Permission.DATASET_VIEW)
    value = datasets.annotation_submission_by_id(submission_id)
    if value is None:
        raise AnnotationRefusedError(DatasetRefusalCode.ANNOTATION_NOT_FOUND)
    return value


def read_scoped_annotation_submission(
    *,
    dataset_id: UUID,
    member_id: UUID,
    submission_id: UUID,
    caller: Caller,
    datasets: DatasetRepository,
) -> AnnotationSubmission:
    """读取并核对数据集、视频与标注提交的完整归属。"""
    value = read_annotation_submission(
        submission_id=submission_id,
        caller=caller,
        datasets=datasets,
    )
    if value.dataset_id != dataset_id or value.member_id != member_id:
        raise AnnotationRefusedError(DatasetRefusalCode.RESOURCE_MISMATCH)
    return value


def read_annotation_execution(
    *,
    submission_id: UUID,
    execution_id: UUID,
    caller: Caller,
    datasets: DatasetRepository,
) -> AnnotationExecution:
    """读取并核对标注提交下的一次执行代次。"""
    submission = read_annotation_submission(
        submission_id=submission_id,
        caller=caller,
        datasets=datasets,
    )
    return _gateway_execution_resource(
        submission_id=submission.id,
        execution_id=execution_id,
        datasets=datasets,
    )


def list_annotation_submissions(
    *,
    dataset_id: UUID,
    member_id: UUID,
    caller: Caller,
    datasets: DatasetRepository,
) -> Sequence[AnnotationSubmission]:
    """读取一个视频的全部标注提交历史。"""
    authorize(caller, Permission.DATASET_VIEW)
    _member_for_dataset(dataset_id=dataset_id, member_id=member_id, datasets=datasets)
    return datasets.list_annotation_submissions(dataset_id=dataset_id, member_id=member_id)


def submit_annotation(
    *,
    dataset_id: UUID,
    member_id: UUID,
    context_token: str,
    raw_segments: Sequence[Mapping[str, Any]],
    mode: AnnotationMode,
    idempotency_key: str,
    caller: Caller,
    now: datetime,
    datasets: DatasetRepository,
    jobs: AnnotationJobQueue,
    secret: str,
    expected_revision: int | None = None,
) -> AnnotationSubmissionResult:
    """保存原始段集合并创建一条幂等的异步切片候选。"""
    authorize(caller, Permission.DATASET_EDIT)
    if not idempotency_key.strip() or len(idempotency_key) > _MAX_IDEMPOTENCY_KEY_LENGTH:
        raise AnnotationRefusedError(
            DatasetRefusalCode.ANNOTATION_IDEMPOTENCY_CONFLICT,
            detail="标注幂等键不能为空且不能超过 255 个字符",
        )
    member = _member_for_dataset(dataset_id=dataset_id, member_id=member_id, datasets=datasets)
    _require_registered_member(member)
    context = resolve_annotation_context(
        token=context_token,
        secret=secret,
        now=now,
        datasets=datasets,
        dataset_id=dataset_id,
        member_id=member_id,
    )
    if (
        context.preparation_status != "succeeded"
        or context.upstream_data_id is None
        or context.upstream_video_id is None
        or context.upstream_video_size is None
        or context.upstream_video_sha256 is None
        or context.upstream_video_duration_seconds is None
    ):
        raise AnnotationRefusedError(
            DatasetRefusalCode.ANNOTATION_STATE_CONFLICT,
            detail="标注媒体仍在准备，请稍后重试",
        )
    locked_member = datasets.lock_annotation_member(member_id)
    if locked_member is None:
        raise AnnotationRefusedError(DatasetRefusalCode.MEMBER_NOT_FOUND)
    member = locked_member
    _require_registered_member(member)
    if (
        member.object_version_id != context.source_object_version_id
        or member.actual_sha256 != context.source_sha256
    ):
        raise AnnotationRefusedError(
            DatasetRefusalCode.ANNOTATION_CONTEXT_INVALID,
            detail="视频对象已变化，请重新打开标注上下文",
        )
    action_list = datasets.action_list_by_revision(
        dataset_id=dataset_id,
        revision=context.action_list_revision,
    )
    if action_list is None:
        raise AnnotationRefusedError(DatasetRefusalCode.ACTION_LIST_NOT_FOUND)
    normalized, preserved = _normalize_segments(
        raw_segments,
        actions=action_list.actions,
        duration_seconds=member.duration_seconds,
    )
    digest = _request_digest(
        context=context,
        mode=mode,
        segments=normalized,
        raw_segments=preserved,
    )
    existing = datasets.annotation_submission_by_idempotency(
        dataset_id=dataset_id,
        idempotency_key=idempotency_key,
    )
    if existing is not None:
        if existing.request_digest != digest:
            raise AnnotationRefusedError(
                DatasetRefusalCode.ANNOTATION_IDEMPOTENCY_CONFLICT,
                detail="幂等键已对应另一份标注内容",
            )
        execution = datasets.latest_annotation_execution(existing.id)
        if execution is None or execution.job_id is None:
            raise AnnotationRefusedError(
                DatasetRefusalCode.ANNOTATION_STATE_CONFLICT,
                detail="标注提交缺少执行任务",
            )
        job = jobs.get_or_create_annotation(
            member_id=member_id,
            attempt_id=execution.id,
            now=now,
        )
        return AnnotationSubmissionResult(existing, execution, job)

    if expected_revision is None:
        expected_revision = context.annotation_revision
    if isinstance(expected_revision, bool) or expected_revision < 0:
        raise AnnotationRefusedError(
            DatasetRefusalCode.STALE_REVISION,
            detail="标注修订号无效",
        )
    current_submissions = datasets.list_annotation_submissions(
        dataset_id=dataset_id,
        member_id=member_id,
    )
    current_revision = max(
        (submission.revision for submission in current_submissions),
        default=0,
    )
    if expected_revision != current_revision:
        raise AnnotationRefusedError(
            DatasetRefusalCode.STALE_REVISION,
            detail="标注已被其他用户修改，请重新读取后再提交",
        )

    submission = AnnotationSubmission(
        id=new_id(),
        dataset_id=dataset_id,
        member_id=member_id,
        context_id=context.id,
        revision=current_revision + 1,
        action_list_revision=action_list.revision,
        source_object_version_id=context.source_object_version_id,
        source_sha256=context.source_sha256,
        idempotency_key=idempotency_key,
        request_digest=digest,
        mode=mode,
        segments=normalized,
        raw_segments=preserved,
        created_by=caller.user.id,
        created_at=now,
    )
    execution = AnnotationExecution(
        id=new_id(),
        submission_id=submission.id,
        generation=1,
        job_id=None,
        status=AnnotationExecutionStatus.PENDING,
        clips=(),
        failure_code=None,
        failure_detail=None,
        created_at=now,
        updated_at=now,
    )
    datasets.add_annotation_submission(submission)
    datasets.add_annotation_execution(execution)
    job = jobs.get_or_create_annotation(
        member_id=member_id,
        attempt_id=execution.id,
        now=now,
    )
    execution = replace(execution, job_id=job.id)
    if not datasets.save_annotation_execution(execution, expected_updated_at=now):
        raise AnnotationRefusedError(
            DatasetRefusalCode.ANNOTATION_STATE_CONFLICT,
            detail="标注执行任务保存失败",
        )
    return AnnotationSubmissionResult(submission, execution, job)


def retry_annotation(
    *,
    submission_id: UUID,
    caller: Caller,
    now: datetime,
    datasets: DatasetRepository,
    jobs: AnnotationJobQueue,
    dataset_id: UUID | None = None,
    member_id: UUID | None = None,
) -> AnnotationSubmissionResult:
    """为同一业务提交追加新的执行代次，不改写旧候选。"""
    authorize(caller, Permission.DATASET_EDIT)
    submission = datasets.annotation_submission_by_id(submission_id)
    if submission is None:
        raise AnnotationRefusedError(DatasetRefusalCode.ANNOTATION_NOT_FOUND)
    if (
        dataset_id is not None
        and member_id is not None
        and (submission.dataset_id != dataset_id or submission.member_id != member_id)
    ):
        raise AnnotationRefusedError(DatasetRefusalCode.RESOURCE_MISMATCH)
    member = datasets.lock_annotation_member(submission.member_id)
    if member is None:
        raise AnnotationRefusedError(DatasetRefusalCode.MEMBER_NOT_FOUND)
    _require_registered_member(member)
    latest = datasets.latest_annotation_execution(submission.id)
    if latest is None:
        raise AnnotationRefusedError(DatasetRefusalCode.ANNOTATION_STATE_CONFLICT)
    if latest.status in {
        AnnotationExecutionStatus.PENDING,
        AnnotationExecutionStatus.RUNNING,
    }:
        raise AnnotationRefusedError(
            DatasetRefusalCode.ANNOTATION_STATE_CONFLICT,
            detail="当前切片任务仍在执行",
        )
    execution = AnnotationExecution(
        id=new_id(),
        submission_id=submission.id,
        generation=latest.generation + 1,
        job_id=None,
        status=AnnotationExecutionStatus.PENDING,
        clips=(),
        failure_code=None,
        failure_detail=None,
        created_at=now,
        updated_at=now,
    )
    datasets.add_annotation_execution(execution)
    job = jobs.get_or_create_annotation(
        member_id=submission.member_id,
        attempt_id=execution.id,
        now=now,
    )
    execution = replace(execution, job_id=job.id)
    if not datasets.save_annotation_execution(execution, expected_updated_at=now):
        raise AnnotationRefusedError(
            DatasetRefusalCode.ANNOTATION_STATE_CONFLICT,
            detail="标注执行任务保存失败",
        )
    return AnnotationSubmissionResult(submission, execution, job)


def begin_annotation_execution(
    *,
    job: ApplicationJob,
    datasets: DatasetRepository,
    now: datetime,
) -> AnnotationExecutionTarget | None:
    """领取一条标注执行租约；重复投递只允许一个 worker 前进。"""
    execution = datasets.annotation_execution_by_id(job.attempt_id)
    if execution is None or execution.job_id != job.id:
        return None
    if execution.status not in {
        AnnotationExecutionStatus.PENDING,
        AnnotationExecutionStatus.RUNNING,
    }:
        return None
    submission = datasets.annotation_submission_by_id(execution.submission_id)
    if submission is None or submission.member_id != job.member_id:
        return None
    member = datasets.member_by_id(submission.member_id)
    context = datasets.annotation_context_by_id(submission.context_id)
    actions = datasets.action_list_by_revision(
        dataset_id=submission.dataset_id,
        revision=submission.action_list_revision,
    )
    if member is None or context is None or actions is None:
        return None
    running = replace(execution, status=AnnotationExecutionStatus.RUNNING, updated_at=now)
    if not datasets.save_annotation_execution(running, expected_updated_at=execution.updated_at):
        return None
    return AnnotationExecutionTarget(
        job=job,
        submission=submission,
        execution=running,
        member=member,
        context=context,
        actions=actions,
    )


def complete_annotation_execution(
    *,
    target: AnnotationExecutionTarget,
    clips: Sequence[Mapping[str, Any]],
    now: datetime,
    datasets: DatasetRepository,
) -> AnnotationExecution:
    """按执行租约追加成功候选；旧代次保持不变。"""
    member = datasets.lock_annotation_member(target.submission.member_id)
    if (
        member is None
        or member.dataset_id != target.submission.dataset_id
        or member.status != MemberStatus.REGISTERED
        or member.object_version_id != target.submission.source_object_version_id
        or member.actual_sha256 != target.submission.source_sha256
    ):
        raise AnnotationRefusedError(
            DatasetRefusalCode.ANNOTATION_CONTEXT_INVALID,
            detail="视频对象已变化，不能发布本次切片结果",
        )
    value = replace(
        target.execution,
        status=AnnotationExecutionStatus.SUCCEEDED,
        clips=_validated_clips(clips),
        failure_code=None,
        failure_detail=None,
        updated_at=now,
    )
    if not datasets.save_annotation_execution(
        value,
        expected_updated_at=target.execution.updated_at,
    ):
        raise AnnotationRefusedError(
            DatasetRefusalCode.ANNOTATION_STATE_CONFLICT,
            detail="标注执行租约已失效",
        )
    return value


def fail_annotation_execution(
    *,
    target: AnnotationExecutionTarget,
    code: str,
    detail: str,
    now: datetime,
    datasets: DatasetRepository,
) -> AnnotationExecution:
    """按执行租约保存失败原因，不删除已有候选。"""
    value = replace(
        target.execution,
        status=AnnotationExecutionStatus.FAILED,
        clips=(),
        failure_code=code,
        failure_detail=detail,
        updated_at=now,
    )
    if not datasets.save_annotation_execution(
        value,
        expected_updated_at=target.execution.updated_at,
    ):
        raise AnnotationRefusedError(
            DatasetRefusalCode.ANNOTATION_STATE_CONFLICT,
            detail="标注执行租约已失效",
        )
    return value


def prepare_annotation_context_copy(
    *,
    context: AnnotationContext,
    member: DatasetMember,
    actions: ActionListRevision,
    storage: ObjectStorage,
    backend: AnnotationBackend,
    media_probe: MediaProbe,
) -> PreparedAnnotationCopy:
    """在不写数据库的阶段创建页面使用的基座副本。"""
    if member.dataset_id != context.dataset_id:
        raise AnnotationRefusedError(DatasetRefusalCode.ANNOTATION_CONTEXT_INVALID)
    _require_registered_member(member)
    if (
        member.object_version_id != context.source_object_version_id
        or member.actual_sha256 != context.source_sha256
    ):
        raise AnnotationRefusedError(
            DatasetRefusalCode.ANNOTATION_CONTEXT_INVALID,
            detail="视频对象已变化，请重新打开标注上下文",
        )
    if actions.dataset_id != context.dataset_id or actions.revision != context.action_list_revision:
        raise AnnotationRefusedError(
            DatasetRefusalCode.ANNOTATION_CONTEXT_INVALID,
            detail="动作清单上下文已变化，请重新打开标注上下文",
        )
    return _prepare_backend_copy(
        member=member,
        source_version_id=context.source_object_version_id,
        source_sha256=context.source_sha256,
        actions=actions.actions,
        storage=storage,
        backend=backend,
        media_probe=media_probe,
    )


def prepare_annotation_execution_copy(
    *,
    target: AnnotationExecutionTarget,
    storage: ObjectStorage,
    backend: AnnotationBackend,
    media_probe: MediaProbe,
) -> PreparedAnnotationCopy:
    """在执行租约事务外创建一代独立基座副本。"""
    _require_registered_member(target.member)
    if (
        target.context.dataset_id != target.submission.dataset_id
        or target.context.member_id != target.submission.member_id
        or target.context.source_object_version_id != target.submission.source_object_version_id
        or target.context.source_sha256 != target.submission.source_sha256
        or any(
            segment.action_index < 0
            or segment.action_index >= len(target.actions.actions)
            or target.actions.actions[segment.action_index] != segment.action_description
            for segment in target.submission.segments
        )
    ):
        raise AnnotationRefusedError(
            DatasetRefusalCode.ANNOTATION_CONTEXT_INVALID,
            detail="标注执行绑定内容已变化",
        )
    return _prepare_backend_copy(
        member=target.member,
        source_version_id=target.submission.source_object_version_id,
        source_sha256=target.submission.source_sha256,
        actions=target.actions.actions,
        storage=storage,
        backend=backend,
        media_probe=media_probe,
    )


def save_annotation_execution_copy(
    *,
    target: AnnotationExecutionTarget,
    prepared: PreparedAnnotationCopy,
    now: datetime,
    datasets: DatasetRepository,
) -> AnnotationExecutionTarget:
    """在短事务内核对上下文与租约并登记基座副本身份。"""
    member = datasets.lock_annotation_member(target.submission.member_id)
    context = datasets.annotation_context_by_id(target.submission.context_id)
    actions = datasets.action_list_by_revision(
        dataset_id=target.submission.dataset_id,
        revision=target.submission.action_list_revision,
    )
    if member is None or context is None or actions is None:
        raise AnnotationRefusedError(DatasetRefusalCode.ANNOTATION_CONTEXT_INVALID)
    if (
        member.dataset_id != target.submission.dataset_id
        or member.object_version_id != target.submission.source_object_version_id
        or member.actual_sha256 != target.submission.source_sha256
        or context.dataset_id != target.submission.dataset_id
        or context.member_id != target.submission.member_id
        or context.source_object_version_id != target.submission.source_object_version_id
        or context.source_sha256 != target.submission.source_sha256
        or actions.actions != target.actions.actions
        or any(
            segment.action_index < 0
            or segment.action_index >= len(actions.actions)
            or actions.actions[segment.action_index] != segment.action_description
            for segment in target.submission.segments
        )
    ):
        raise AnnotationRefusedError(
            DatasetRefusalCode.ANNOTATION_CONTEXT_INVALID,
            detail="标注执行绑定内容已变化",
        )
    _require_registered_member(member)
    execution = datasets.annotation_execution_by_id(target.execution.id)
    if (
        execution is None
        or execution.status is not AnnotationExecutionStatus.RUNNING
        or execution.updated_at != target.execution.updated_at
    ):
        raise AnnotationRefusedError(
            DatasetRefusalCode.ANNOTATION_STATE_CONFLICT,
            detail="标注执行租约已失效",
        )
    updated = replace(
        execution,
        upstream_data_id=prepared.prepared.data_id,
        upstream_video_id=prepared.prepared.video_id,
        derived_video_size=prepared.size,
        derived_video_sha256=prepared.sha256,
        derived_video_duration_seconds=prepared.duration_seconds,
        updated_at=now,
    )
    if not datasets.save_annotation_execution(updated, expected_updated_at=execution.updated_at):
        raise AnnotationRefusedError(
            DatasetRefusalCode.ANNOTATION_STATE_CONFLICT,
            detail="标注执行租约已失效",
        )
    return replace(target, execution=updated, member=member, context=context, actions=actions)


def _prepare_backend_copy(
    *,
    member: DatasetMember,
    source_version_id: str,
    source_sha256: str,
    actions: Sequence[str],
    storage: ObjectStorage,
    backend: AnnotationBackend,
    media_probe: MediaProbe,
) -> PreparedAnnotationCopy:
    """读取固定源对象并核对基座派生副本的媒体事实。"""
    if member.object_version_id != source_version_id or member.actual_sha256 != source_sha256:
        raise AnnotationRefusedError(
            DatasetRefusalCode.ANNOTATION_CONTEXT_INVALID,
            detail="视频对象已变化，请重新打开标注上下文",
        )
    if not member.object_key:
        raise AnnotationRefusedError(
            DatasetRefusalCode.ANNOTATION_CONTEXT_INVALID,
            detail="视频没有可读取的对象",
        )
    try:
        with tempfile.NamedTemporaryFile(
            prefix="nvsop-annotation-", suffix=Path(member.original_filename).suffix
        ) as source:
            binary_source = cast(BinaryIO, source)
            storage.download_to(
                object_key=member.object_key,
                destination=binary_source,
            )
            source.flush()
            source.seek(0)
            digest = hashlib.sha256()
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
            if digest.hexdigest() != source_sha256:
                raise AnnotationRefusedError(
                    DatasetRefusalCode.SHA256_MISMATCH,
                    detail="标注源对象摘要与登记事实不一致",
                )
            source.seek(0)
            prepared = backend.prepare_video(
                source=binary_source,
                filename=member.original_filename,
                actions=actions,
            )
            with tempfile.NamedTemporaryFile(
                prefix="nvsop-annotation-derived-", suffix=".mp4"
            ) as derived:
                derived_source = cast(BinaryIO, derived)
                backend.download_video(video_id=prepared.video_id, destination=derived_source)
                derived.flush()
                derived_path = Path(derived.name)
                derived_size = derived_path.stat().st_size
                derived_sha256 = _file_sha256(derived_path)
                derived_metadata = media_probe.probe(str(derived_path))
    except ObjectNotFoundError as error:
        raise AnnotationRefusedError(
            DatasetRefusalCode.OBJECT_NOT_FOUND,
            detail="标注源对象不存在",
        ) from error
    except ObjectStorageUnavailableError as error:
        raise AnnotationRefusedError(
            DatasetRefusalCode.STORAGE_UNAVAILABLE,
            detail="标注源对象暂时不可读取",
        ) from error
    except (MediaProbeUnavailableError, InvalidMediaError) as error:
        raise AnnotationRefusedError(
            DatasetRefusalCode.MEDIA_PROBE_UNAVAILABLE,
            detail="标注派生视频的媒体事实无法确认",
        ) from error
    except AnnotationBackendUnavailableError as error:
        raise AnnotationRefusedError(
            DatasetRefusalCode.ANNOTATION_BACKEND_UNAVAILABLE,
            detail="标注基座暂时不可用",
        ) from error
    except AnnotationBackendExecutionError as error:
        raise AnnotationRefusedError(
            DatasetRefusalCode.ANNOTATION_EXECUTION_FAILED,
            detail=str(error),
        ) from error
    if (
        not math.isfinite(derived_metadata.duration_seconds)
        or abs(derived_metadata.duration_seconds - (member.duration_seconds or 0.0)) > 0.1
    ):
        raise AnnotationRefusedError(
            DatasetRefusalCode.ANNOTATION_EXECUTION_FAILED,
            detail="基座转码后的视频时长无法与源视频坐标对应",
        )
    return PreparedAnnotationCopy(
        prepared=prepared,
        size=derived_size,
        sha256=derived_sha256,
        duration_seconds=derived_metadata.duration_seconds,
    )


def _require_dataset(*, dataset_id: UUID, datasets: DatasetRepository) -> None:
    if datasets.dataset_by_id(dataset_id) is None:
        raise AnnotationRefusedError(DatasetRefusalCode.DATASET_NOT_FOUND)


def _member_for_dataset(
    *, dataset_id: UUID, member_id: UUID, datasets: DatasetRepository
) -> DatasetMember:
    _require_dataset(dataset_id=dataset_id, datasets=datasets)
    member = datasets.member_by_id(member_id)
    if member is None:
        raise AnnotationRefusedError(DatasetRefusalCode.MEMBER_NOT_FOUND)
    if member.dataset_id != dataset_id:
        raise AnnotationRefusedError(DatasetRefusalCode.RESOURCE_MISMATCH)
    return member


def _require_registered_member(member: DatasetMember) -> None:
    if member.status != MemberStatus.REGISTERED:
        raise AnnotationRefusedError(
            DatasetRefusalCode.ANNOTATION_MEMBER_NOT_REGISTERED,
            detail="只有已完成视频校验的成员才能标注",
        )
    if (
        member.actual_size is None
        or member.actual_sha256 is None
        or member.duration_seconds is None
        or not member.object_version_id
    ):
        raise AnnotationRefusedError(
            DatasetRefusalCode.ANNOTATION_MEMBER_NOT_REGISTERED,
            detail="视频校验尚未提供完整媒体事实",
        )


def _normalize_actions(actions: Sequence[str]) -> tuple[str, ...]:
    if not actions:
        raise AnnotationRefusedError(
            DatasetRefusalCode.ACTION_LIST_INVALID,
            detail="动作清单不能为空",
        )
    normalized: list[str] = []
    for expected, value in enumerate(actions, 1):
        if not isinstance(value, str) or not value.strip():
            raise AnnotationRefusedError(
                DatasetRefusalCode.ACTION_LIST_INVALID,
                detail="动作描述不能为空",
            )
        match = _ACTION_RE.fullmatch(value.strip())
        if match is None or int(match.group(1)) != expected:
            raise AnnotationRefusedError(
                DatasetRefusalCode.ACTION_LIST_INVALID,
                detail="动作号必须从 1 开始连续，并匹配 (step) 描述格式",
            )
        normalized.append(value.strip())
    return tuple(normalized)


def _normalize_segments(
    raw_segments: Sequence[Mapping[str, Any]],
    *,
    actions: Sequence[str],
    duration_seconds: float | None,
) -> tuple[tuple[AnnotationSegment, ...], tuple[dict[str, Any], ...]]:
    if not raw_segments:
        raise AnnotationRefusedError(
            DatasetRefusalCode.ANNOTATION_STATE_CONFLICT,
            detail="至少需要一个动作时间段",
        )
    normalized: list[AnnotationSegment] = []
    preserved: list[dict[str, Any]] = []
    for index, item in enumerate(raw_segments):
        if not isinstance(item, Mapping):
            raise AnnotationRefusedError(DatasetRefusalCode.ANNOTATION_STATE_CONFLICT)
        try:
            start = float(item["start"])
            end = float(item["end"])
            if (
                "actionIndex" in item
                and "action_index" in item
                and item["actionIndex"] != item["action_index"]
            ):
                raise ValueError("duplicate action index")
            action_index_value = item.get("actionIndex", item.get("action_index"))
            if isinstance(action_index_value, bool):
                raise ValueError("action index")
            if isinstance(action_index_value, float) and not action_index_value.is_integer():
                raise ValueError("action index")
            action_index = int(action_index_value)
        except (KeyError, TypeError, ValueError) as error:
            raise AnnotationRefusedError(
                DatasetRefusalCode.ANNOTATION_STATE_CONFLICT,
                detail="动作时间段字段无效",
                field_errors=(
                    DatasetFieldError(
                        field=f"segments[{index}]",
                        message="必须包含有效的 start、end 和 actionIndex",
                    ),
                ),
            ) from error
        if (
            not math.isfinite(start)
            or not math.isfinite(end)
            or start < 0
            or end <= start
            or duration_seconds is None
            or end > duration_seconds
            or action_index < 0
            or action_index >= len(actions)
        ):
            raise AnnotationRefusedError(
                DatasetRefusalCode.ANNOTATION_STATE_CONFLICT,
                detail="动作时间段超出视频或动作清单范围",
                field_errors=(
                    DatasetFieldError(
                        field=f"segments[{index}]",
                        message="必须满足 0 ≤ start < end ≤ 视频时长，且动作编号有效",
                    ),
                ),
            )
        normalized.append(
            AnnotationSegment(
                start=start,
                end=end,
                action_index=action_index,
                action_description=actions[action_index],
            )
        )
        preserved.append(
            _copy_json_object(
                {
                    key: value
                    for key, value in item.items()
                    if isinstance(key, str) and key in _ALLOWED_SEGMENT_FIELDS
                }
            )
        )
    return tuple(normalized), tuple(preserved)


def _request_digest(
    *,
    context: AnnotationContext,
    mode: AnnotationMode,
    segments: Sequence[AnnotationSegment],
    raw_segments: Sequence[Mapping[str, Any]],
) -> str:
    value = {
        "dataset_id": str(context.dataset_id),
        "member_id": str(context.member_id),
        "action_list_revision": context.action_list_revision,
        "source_object_version_id": context.source_object_version_id,
        "source_sha256": context.source_sha256,
        "mode": mode.value,
        "segments": [segment.as_wire() for segment in segments],
        "raw_segments": list(raw_segments),
    }
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _validated_clips(clips: Sequence[Mapping[str, Any]]) -> tuple[dict[str, Any], ...]:
    if not clips:
        raise AnnotationRefusedError(
            DatasetRefusalCode.ANNOTATION_EXECUTION_FAILED,
            detail="标注基座没有返回切片结果",
        )
    validated: list[dict[str, Any]] = []
    for clip in clips:
        if not isinstance(clip, Mapping):
            raise AnnotationRefusedError(
                DatasetRefusalCode.ANNOTATION_EXECUTION_FAILED,
                detail="标注基座返回的切片缺少资源身份",
            )
        clip_id = clip.get("id")
        if not isinstance(clip_id, str) or not clip_id:
            raise AnnotationRefusedError(
                DatasetRefusalCode.ANNOTATION_EXECUTION_FAILED,
                detail="标注基座返回的切片缺少资源身份",
            )
        validated.append(_copy_json_object(clip))
    return tuple(validated)


def _copy_json_object(value: Mapping[str, Any]) -> dict[str, Any]:
    try:
        copied = json.loads(json.dumps(dict(value), ensure_ascii=False))
    except (TypeError, ValueError) as error:
        raise AnnotationRefusedError(
            DatasetRefusalCode.ANNOTATION_STATE_CONFLICT,
            detail="动作时间段包含不可保存的字段",
        ) from error
    if not isinstance(copied, dict):
        raise AnnotationRefusedError(DatasetRefusalCode.ANNOTATION_STATE_CONFLICT)
    return copied


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _padding(value: str) -> str:
    return value + "=" * (-len(value) % 4)


def _decode_b64url(value: str) -> bytes:
    decoded = base64.urlsafe_b64decode(_padding(value))
    if _b64url(decoded) != value:
        raise ValueError("non-canonical base64")
    return decoded
