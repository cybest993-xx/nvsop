"""训练数据集的应用用例：创建分组、逐视频上传、确认、校验和恢复。"""

from __future__ import annotations

import hashlib
import math
import shutil
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import BinaryIO, NoReturn, cast
from uuid import UUID

from factory_sop.auth.api import Caller, Permission, authorize
from factory_sop.dataset.errors import DatasetRefusalCode, DatasetRefusedError
from factory_sop.dataset.media import (
    InvalidMediaError,
    MediaMetadata,
    MediaProbe,
    MediaProbeUnavailableError,
)
from factory_sop.dataset.model import (
    AttemptStatus,
    ConfirmationResult,
    DatasetMember,
    MemberStatus,
    RetryMode,
    RetryResult,
    TrainingDataset,
    UploadAttempt,
    UploadInstructions,
    UploadRequestResult,
)
from factory_sop.dataset.repository import DatasetRepository
from factory_sop.dataset.storage import (
    ObjectNotFoundError,
    ObjectStorage,
    ObjectStorageUnavailableError,
)
from factory_sop.identifiers import new_id
from factory_sop.job.api import ApplicationJob, ValidationJobQueue
from factory_sop.observability import get_logger

_logger = get_logger("dataset")

_ARCHIVE_SUFFIXES = (
    ".zip",
    ".tar",
    ".tar.gz",
    ".tgz",
    ".gz",
    ".bz2",
    ".xz",
    ".zst",
    ".7z",
    ".rar",
    ".cab",
)
_ARCHIVE_SIGNATURES = (
    b"PK\x03\x04",
    b"PK\x05\x06",
    b"PK\x07\x08",
    b"\x1f\x8b",
    b"7z\xbc\xaf'\x1c",
    b"Rar!\x1a\x07",
    b"BZh",
    b"\xfd7zXZ\x00",
    b"\x28\xb5\x2f\xfd",
    b"MSCF",
    b"!<arch>\n",
)
_MAX_FILENAME_LENGTH = 255
_MAX_SOURCE_LENGTH = 255
_MAX_IDEMPOTENCY_LENGTH = 255
_CODEC_ALIASES = {"h265": "hevc"}
# 流式上传说明指向中心正式入口；浏览器与脚本不再拿到对象存储直传地址。
_API_PREFIX = "/api/v1"
_STREAM_COPY_BYTES = 1024 * 1024


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """一次 worker 校验完成后可查询的成员快照。"""

    status: str
    actual_size: int | None
    actual_sha256: str | None
    duration_seconds: float | None
    codec: str | None
    failure_code: str | None
    failure_detail: str | None
    recovery_action: str | None


@dataclass(frozen=True, slots=True)
class ValidationTarget:
    """worker 在事务外读取对象前锁定的当前成员与尝试快照。"""

    member: DatasetMember
    attempt: UploadAttempt


@dataclass(frozen=True, slots=True)
class VideoContentUploadTarget:
    """一次经授权的流式上传目标：服务端生成的对象键与本次允许的字节上限。"""

    dataset_id: UUID
    member_id: UUID
    attempt_id: UUID
    object_key: str
    max_bytes: int


def create_training_dataset(
    *,
    name: str,
    caller: Caller,
    now: datetime,
    datasets: DatasetRepository,
) -> TrainingDataset:
    """创建有名称的训练数据集；读取和导入权限彼此不隐式授予。"""
    authorize(caller, Permission.DATASET_IMPORT)
    if not name.strip() or len(name) > 255:
        _refuse(
            DatasetRefusalCode.DATASET_NAME_INVALID,
            "训练数据集名称不能为空且不能超过 255 个字符",
        )
    dataset = TrainingDataset(
        id=new_id(),
        name=name.strip(),
        created_by=caller.user.id,
        updated_by=caller.user.id,
        created_at=now,
        updated_at=now,
    )
    datasets.add_dataset(dataset)
    _logger.info(
        "dataset.training_dataset.created",
        dataset_id=str(dataset.id),
        actor_id=str(caller.user.id),
    )
    return dataset


def list_training_datasets(
    *,
    caller: Caller,
    page: int,
    page_size: int,
    datasets: DatasetRepository,
) -> tuple[Sequence[TrainingDataset], int]:
    """列出训练数据集及真实总数。"""
    authorize(caller, Permission.DATASET_VIEW)
    return datasets.page_datasets(page=page, page_size=page_size)


def read_training_dataset(
    *,
    dataset_id: UUID,
    caller: Caller,
    datasets: DatasetRepository,
) -> TrainingDataset:
    """读取一个训练数据集，找不到或身份不匹配时拒绝。"""
    authorize(caller, Permission.DATASET_VIEW)
    dataset = datasets.dataset_by_id(dataset_id)
    if dataset is None:
        _refuse(DatasetRefusalCode.DATASET_NOT_FOUND, "训练数据集不存在")
    return dataset


def list_dataset_members(
    *,
    dataset_id: UUID,
    caller: Caller,
    page: int,
    page_size: int,
    datasets: DatasetRepository,
) -> tuple[Sequence[DatasetMember], int]:
    """列出一个训练数据集内的视频成员。"""
    authorize(caller, Permission.DATASET_VIEW)
    _require_dataset(dataset_id=dataset_id, datasets=datasets)
    return datasets.page_members(dataset_id=dataset_id, page=page, page_size=page_size)


def read_dataset_member(
    *,
    dataset_id: UUID,
    member_id: UUID,
    caller: Caller,
    datasets: DatasetRepository,
) -> DatasetMember:
    """读取成员并验证其确实属于所请求的数据集。"""
    authorize(caller, Permission.DATASET_VIEW)
    member = datasets.member_by_id(member_id)
    if member is None:
        _refuse(DatasetRefusalCode.MEMBER_NOT_FOUND, "视频成员不存在")
    if member.dataset_id != dataset_id:
        _refuse(DatasetRefusalCode.RESOURCE_MISMATCH, "视频不属于所请求的训练数据集")
    return member


def request_video_upload(
    *,
    dataset_id: UUID,
    original_filename: str,
    source: str,
    declared_size: int,
    declared_sha256: str,
    idempotency_key: str | None,
    caller: Caller,
    now: datetime,
    datasets: DatasetRepository,
    max_upload_bytes: int,
    upload_ttl_seconds: int,
) -> UploadRequestResult:
    """申请单个视频的短期流式上传说明，并保证同一幂等键不新增成员。

    授权是第一条语句；声明校验和对象键分配都在它之后。客户端只提交元数据，服务端
    生成唯一对象键，故文件名既不能成为路径也不能覆盖另一条已登记素材。
    """
    authorize(caller, Permission.DATASET_IMPORT)
    _validate_upload_declaration(
        original_filename=original_filename,
        source=source,
        declared_size=declared_size,
        declared_sha256=declared_sha256,
        idempotency_key=idempotency_key,
        max_upload_bytes=max_upload_bytes,
        upload_ttl_seconds=upload_ttl_seconds,
    )
    # 摘要十六进制大小写等价；持久化前统一小写，避免合法的大写声明在 worker 中被误拒。
    declared_sha256 = declared_sha256.casefold()
    _require_dataset(dataset_id=dataset_id, datasets=datasets)

    if idempotency_key is not None:
        existing_attempt = datasets.attempt_by_idempotency(
            dataset_id=dataset_id, idempotency_key=idempotency_key
        )
        if existing_attempt is not None:
            existing_member = datasets.member_by_id(existing_attempt.member_id)
            if existing_member is None:
                _refuse(DatasetRefusalCode.RESOURCE_MISMATCH, "幂等上传尝试的成员不存在")
            if (
                existing_attempt.declared_size != declared_size
                or existing_attempt.declared_sha256 != declared_sha256
                or existing_member.original_filename != original_filename
                or existing_member.source != source
            ):
                _refuse(DatasetRefusalCode.IDEMPOTENCY_CONFLICT, "同一幂等键的上传声明不能改变")
            if (
                existing_member.status == MemberStatus.REGISTERED
                or existing_attempt.status == AttemptStatus.REGISTERED
            ):
                # 已登记成员的对象必须是定稿对象；再次申请不能返回仍可覆盖源对象的旧授权。
                return UploadRequestResult(
                    member=existing_member,
                    attempt=existing_attempt,
                    upload=None,
                )
            if existing_attempt.status != AttemptStatus.PENDING_UPLOAD:
                _refuse(
                    DatasetRefusalCode.STATE_CONFLICT,
                    "该幂等上传尝试已结案，请使用明确的恢复动作",
                )
            renewed = replace(
                existing_attempt,
                expires_at=now + timedelta(seconds=upload_ttl_seconds),
            )
            datasets.save_attempt(renewed)
            return UploadRequestResult(
                member=existing_member,
                attempt=renewed,
                upload=_upload_instructions(
                    dataset_id=dataset_id,
                    member_id=existing_member.id,
                    attempt_id=renewed.id,
                    object_key=renewed.object_key,
                    max_upload_bytes=max_upload_bytes,
                    expires_at=renewed.expires_at,
                ),
            )

    member_id = new_id()
    attempt_id = new_id()
    expires_at = now + timedelta(seconds=upload_ttl_seconds)
    object_key = _object_key(dataset_id=dataset_id, member_id=member_id, attempt_id=attempt_id)
    upload = _upload_instructions(
        dataset_id=dataset_id,
        member_id=member_id,
        attempt_id=attempt_id,
        object_key=object_key,
        max_upload_bytes=max_upload_bytes,
        expires_at=expires_at,
    )
    member = DatasetMember(
        id=member_id,
        dataset_id=dataset_id,
        original_filename=original_filename,
        source=source,
        declared_size=declared_size,
        declared_sha256=declared_sha256,
        current_attempt_id=attempt_id,
        status=MemberStatus.PENDING_UPLOAD,
        actual_size=None,
        actual_sha256=None,
        duration_seconds=None,
        codec=None,
        container=None,
        object_key=None,
        object_version_id=None,
        validation_job_id=None,
        failure_code=None,
        failure_detail=None,
        recovery_action=None,
        created_by=caller.user.id,
        updated_by=caller.user.id,
        created_at=now,
        updated_at=now,
    )
    attempt = UploadAttempt(
        id=attempt_id,
        dataset_id=dataset_id,
        member_id=member_id,
        idempotency_key=idempotency_key,
        object_key=object_key,
        declared_size=declared_size,
        declared_sha256=declared_sha256,
        expires_at=expires_at,
        status=AttemptStatus.PENDING_UPLOAD,
        created_at=now,
        validation_job_id=None,
        object_version_id=None,
    )
    datasets.add_member(member)
    datasets.add_attempt(attempt)
    _logger.info(
        "dataset.video_upload.issued",
        dataset_id=str(dataset_id),
        member_id=str(member.id),
        attempt_id=str(attempt.id),
        actor_id=str(caller.user.id),
        expires_at=expires_at.isoformat(),
    )
    return UploadRequestResult(member=member, attempt=attempt, upload=upload)


def confirm_video_upload(
    *,
    dataset_id: UUID,
    member_id: UUID,
    attempt_id: UUID,
    caller: Caller,
    now: datetime,
    datasets: DatasetRepository,
    jobs: ValidationJobQueue,
) -> ConfirmationResult:
    """接受一次上传确认并持久化一个幂等校验任务。

    该用例不接收对象 URL、摘要结论或客户端媒体元数据。签名过期不会单独否定已经
    上传的对象；对象是否存在、大小、摘要和编码由后台 worker 读取真实内容决定。
    """
    authorize(caller, Permission.DATASET_IMPORT)
    _require_dataset(dataset_id=dataset_id, datasets=datasets)
    member = datasets.member_by_id(member_id)
    attempt = datasets.attempt_by_id(attempt_id)
    if member is None:
        _refuse(DatasetRefusalCode.MEMBER_NOT_FOUND, "视频成员不存在")
    if attempt is None:
        _refuse(DatasetRefusalCode.ATTEMPT_NOT_FOUND, "上传尝试不存在")
    if member.dataset_id != dataset_id or attempt.member_id != member_id:
        _refuse(DatasetRefusalCode.RESOURCE_MISMATCH, "上传尝试与视频成员归属不一致")
    if member.current_attempt_id != attempt_id:
        _refuse(DatasetRefusalCode.VALIDATION_STALE, "上传尝试已不是当前尝试")

    if member.status == MemberStatus.REGISTERED:
        return ConfirmationResult(member=member, job=None)
    if member.status in (MemberStatus.PENDING_VALIDATION, MemberStatus.VALIDATING):
        job = jobs.get_or_create_validation(member_id=member.id, attempt_id=attempt.id, now=now)
        return ConfirmationResult(member=member, job=job)
    if member.status == MemberStatus.FAILED:
        return ConfirmationResult(member=member, job=None)
    if member.status != MemberStatus.PENDING_UPLOAD:
        _refuse(DatasetRefusalCode.STATE_CONFLICT, "视频状态不允许确认上传")

    job = jobs.get_or_create_validation(member_id=member.id, attempt_id=attempt.id, now=now)
    pending = replace(
        member,
        status=MemberStatus.PENDING_VALIDATION,
        validation_job_id=job.id,
        updated_by=caller.user.id,
        updated_at=now,
        failure_code=None,
        failure_detail=None,
        recovery_action=None,
    )
    if not datasets.save_member(pending, expected_attempt_id=attempt.id):
        current = datasets.member_by_id(member.id)
        if current is not None and current.validation_job_id == job.id:
            return ConfirmationResult(member=current, job=job)
        _refuse(DatasetRefusalCode.VALIDATION_STALE, "上传尝试在确认时已发生变化")
    datasets.save_attempt(
        replace(attempt, status=AttemptStatus.PENDING_VALIDATION, validation_job_id=job.id)
    )
    _logger.info(
        "dataset.video_upload.confirmed",
        dataset_id=str(dataset_id),
        member_id=str(member.id),
        attempt_id=str(attempt.id),
        job_id=str(job.id),
        actor_id=str(caller.user.id),
    )
    return ConfirmationResult(member=pending, job=job)


def retry_video_upload(
    *,
    dataset_id: UUID,
    member_id: UUID,
    mode: RetryMode,
    idempotency_key: str | None = None,
    caller: Caller,
    now: datetime,
    datasets: DatasetRepository,
    jobs: ValidationJobQueue,
    max_upload_bytes: int,
    upload_ttl_seconds: int,
) -> RetryResult:
    """重新申请对象或重新校验固定内容；已登记成员不可原位替换。"""
    authorize(caller, Permission.DATASET_IMPORT)
    _require_dataset(dataset_id=dataset_id, datasets=datasets)
    member = datasets.member_by_id(member_id)
    if member is None:
        _refuse(DatasetRefusalCode.MEMBER_NOT_FOUND, "视频成员不存在")
    if member.dataset_id != dataset_id:
        _refuse(DatasetRefusalCode.RESOURCE_MISMATCH, "视频不属于所请求的训练数据集")
    if member.status != MemberStatus.FAILED:
        _refuse(DatasetRefusalCode.STATE_CONFLICT, "只有失败视频可以恢复")
    attempt = datasets.attempt_by_id(member.current_attempt_id)
    if attempt is None:
        _refuse(DatasetRefusalCode.ATTEMPT_NOT_FOUND, "当前上传尝试不存在")

    if mode is RetryMode.VALIDATION:
        if member.recovery_action != RetryMode.VALIDATION.value:
            _refuse(DatasetRefusalCode.STATE_CONFLICT, "该失败原因需要重新上传视频")
        job = jobs.get_or_create_validation(member_id=member.id, attempt_id=attempt.id, now=now)
        pending = replace(
            member,
            status=MemberStatus.PENDING_VALIDATION,
            validation_job_id=job.id,
            failure_code=None,
            failure_detail=None,
            recovery_action=None,
            updated_by=caller.user.id,
            updated_at=now,
        )
        if not datasets.save_member(pending, expected_attempt_id=attempt.id):
            _refuse(DatasetRefusalCode.VALIDATION_STALE, "上传尝试已不是当前尝试")
        pending_attempt = replace(attempt, status=AttemptStatus.PENDING_VALIDATION)
        datasets.save_attempt(pending_attempt)
        return RetryResult(member=pending, attempt=pending_attempt, upload=None, job=job)

    if mode is not RetryMode.UPLOAD:
        _refuse(DatasetRefusalCode.STATE_CONFLICT, "未知恢复动作")
    _validate_idempotency_key(idempotency_key)
    new_attempt_id = new_id()
    expires_at = now + timedelta(seconds=upload_ttl_seconds)
    object_key = _object_key(dataset_id=dataset_id, member_id=member.id, attempt_id=new_attempt_id)
    upload = _upload_instructions(
        dataset_id=dataset_id,
        member_id=member.id,
        attempt_id=new_attempt_id,
        object_key=object_key,
        max_upload_bytes=max_upload_bytes,
        expires_at=expires_at,
    )
    new_attempt = UploadAttempt(
        id=new_attempt_id,
        dataset_id=dataset_id,
        member_id=member.id,
        idempotency_key=idempotency_key,
        object_key=object_key,
        declared_size=member.declared_size,
        declared_sha256=member.declared_sha256,
        expires_at=expires_at,
        status=AttemptStatus.PENDING_UPLOAD,
        created_at=now,
        validation_job_id=None,
        object_version_id=None,
    )
    reset = replace(
        member,
        current_attempt_id=new_attempt_id,
        status=MemberStatus.PENDING_UPLOAD,
        actual_size=None,
        actual_sha256=None,
        duration_seconds=None,
        codec=None,
        container=None,
        object_key=None,
        object_version_id=None,
        validation_job_id=None,
        failure_code=None,
        failure_detail=None,
        recovery_action=None,
        updated_by=caller.user.id,
        updated_at=now,
    )
    if not datasets.save_member(reset, expected_attempt_id=member.current_attempt_id):
        _refuse(DatasetRefusalCode.VALIDATION_STALE, "上传尝试已不是当前尝试")
    datasets.save_attempt(replace(attempt, status=AttemptStatus.SUPERSEDED))
    datasets.add_attempt(new_attempt)
    _logger.info(
        "dataset.video_upload.retry_issued",
        dataset_id=str(dataset_id),
        member_id=str(member.id),
        old_attempt_id=str(attempt.id),
        attempt_id=str(new_attempt.id),
        actor_id=str(caller.user.id),
    )
    return RetryResult(member=reset, attempt=new_attempt, upload=upload, job=None)


def begin_video_content_upload(
    *,
    dataset_id: UUID,
    member_id: UUID,
    attempt_id: UUID,
    caller: Caller,
    now: datetime,
    datasets: DatasetRepository,
    max_upload_bytes: int,
) -> VideoContentUploadTarget:
    """在开始流式读取请求体前校验上传授权、当前尝试和字节上限。

    流式上传只在通过该检查后才触碰磁盘；越权、尝试过期或非当前尝试都在写入前拒绝，
    不会留下任何已定稿文件或业务记录。上限取部署单视频限制与声明大小的较小者，客户端
    无法靠漏报声明大小绕过限制。
    """
    authorize(caller, Permission.DATASET_IMPORT)
    _require_dataset(dataset_id=dataset_id, datasets=datasets)
    member = datasets.member_by_id(member_id)
    if member is None:
        _refuse(DatasetRefusalCode.MEMBER_NOT_FOUND, "视频成员不存在")
    if member.dataset_id != dataset_id:
        _refuse(DatasetRefusalCode.RESOURCE_MISMATCH, "视频不属于所请求的训练数据集")
    attempt = datasets.attempt_by_id(attempt_id)
    if attempt is None:
        _refuse(DatasetRefusalCode.ATTEMPT_NOT_FOUND, "上传尝试不存在")
    if attempt.member_id != member_id:
        _refuse(DatasetRefusalCode.RESOURCE_MISMATCH, "上传尝试与视频成员归属不一致")
    if member.current_attempt_id != attempt_id:
        _refuse(DatasetRefusalCode.VALIDATION_STALE, "上传尝试已不是当前尝试")
    if (
        member.status != MemberStatus.PENDING_UPLOAD
        or attempt.status != AttemptStatus.PENDING_UPLOAD
    ):
        _refuse(DatasetRefusalCode.STATE_CONFLICT, "视频当前状态不允许上传")
    if attempt.expires_at <= now:
        _refuse(
            DatasetRefusalCode.STATE_CONFLICT,
            "上传授权已过期，请重新申请上传",
            recovery_action=RetryMode.UPLOAD.value,
        )
    return VideoContentUploadTarget(
        dataset_id=dataset_id,
        member_id=member_id,
        attempt_id=attempt_id,
        object_key=attempt.object_key,
        max_bytes=min(max_upload_bytes, attempt.declared_size),
    )


def begin_video_validation(
    *,
    job: ApplicationJob,
    datasets: DatasetRepository,
    now: datetime,
) -> ValidationTarget | None:
    """在短事务中把当前视频标记为校验中，返回随后可脱离事务使用的快照。"""
    member = datasets.member_by_id(job.member_id)
    attempt = datasets.attempt_by_id(job.attempt_id)
    if member is None or attempt is None:
        _refuse(DatasetRefusalCode.RESOURCE_MISMATCH, "校验任务关联的资源不存在")
    if member.current_attempt_id != attempt.id:
        return None
    if member.status == MemberStatus.REGISTERED:
        return ValidationTarget(member=member, attempt=attempt)
    if member.status not in (MemberStatus.PENDING_VALIDATION, MemberStatus.VALIDATING):
        _refuse(DatasetRefusalCode.STATE_CONFLICT, "校验任务关联的视频状态无效")
    if member.status == MemberStatus.VALIDATING:
        renewed = replace(member, updated_at=now)
        if not datasets.save_member(
            renewed,
            expected_attempt_id=attempt.id,
            expected_updated_at=member.updated_at,
        ):
            return None
        return ValidationTarget(member=renewed, attempt=attempt)
    validating = replace(member, status=MemberStatus.VALIDATING, updated_at=now)
    if not datasets.save_member(
        validating,
        expected_attempt_id=attempt.id,
        expected_updated_at=member.updated_at,
    ):
        return None
    return ValidationTarget(member=validating, attempt=attempt)


def validate_video_upload(
    *,
    job: ApplicationJob,
    datasets: DatasetRepository,
    storage: ObjectStorage,
    probe: MediaProbe,
    supported_codecs: frozenset[str],
    now: datetime,
    target: ValidationTarget,
) -> ValidationResult:
    """由 worker 校验一个已接受任务，并仅按当前尝试条件写回结果。

    调用者必须先通过 `begin_video_validation` 取得带租约的快照；对象读取、摘要计算和
    媒体探测都在数据库事务之外完成，最后一步条件写回防止迟到的旧 worker 覆盖重试后的
    新尝试。临时文件由上下文管理器清理。
    """
    member = target.member
    attempt = target.attempt
    if member.current_attempt_id != attempt.id:
        return _validation_result(member)
    if member.status == MemberStatus.REGISTERED:
        return _validation_result(member)
    validating = member

    try:
        stat = storage.stat(object_key=attempt.object_key)
    except ObjectNotFoundError:
        return _fail_validation(
            member=validating,
            attempt=attempt,
            datasets=datasets,
            storage=storage,
            now=now,
            code=DatasetRefusalCode.OBJECT_NOT_FOUND,
            detail="训练素材存储中没有找到已分配的文件，请重新上传",
            recovery_action=RetryMode.UPLOAD.value,
        )
    except (ObjectStorageUnavailableError, OSError):
        return _fail_validation(
            member=validating,
            attempt=attempt,
            datasets=datasets,
            storage=storage,
            now=now,
            code=DatasetRefusalCode.STORAGE_UNAVAILABLE,
            detail="训练素材存储暂时不可用，请稍后重新校验",
            recovery_action=RetryMode.VALIDATION.value,
        )

    if stat.size <= 0:
        return _fail_validation(
            member=validating,
            attempt=attempt,
            datasets=datasets,
            storage=storage,
            now=now,
            code=DatasetRefusalCode.EMPTY_OBJECT,
            detail="对象为空，无法作为视频登记",
            recovery_action=RetryMode.UPLOAD.value,
            actual_size=stat.size,
        )
    if stat.size != attempt.declared_size:
        return _fail_validation(
            member=validating,
            attempt=attempt,
            datasets=datasets,
            storage=storage,
            now=now,
            code=DatasetRefusalCode.SIZE_MISMATCH,
            detail="对象实际大小与登记声明不一致",
            recovery_action=RetryMode.UPLOAD.value,
            actual_size=stat.size,
        )

    try:
        with tempfile.NamedTemporaryFile(mode="w+b", suffix=".video") as temporary:
            stream = cast(BinaryIO, temporary)
            storage.download_to(object_key=attempt.object_key, destination=stream)
            stream.flush()
            stream.seek(0)
            downloaded_size, actual_sha256 = _sha256_stream(stream)
            if downloaded_size != stat.size:
                return _fail_validation(
                    member=validating,
                    attempt=attempt,
                    datasets=datasets,
                    storage=storage,
                    now=now,
                    code=DatasetRefusalCode.SIZE_MISMATCH,
                    detail="对象读取到的实际大小与训练素材存储声明不一致",
                    recovery_action=RetryMode.UPLOAD.value,
                    actual_size=downloaded_size,
                    actual_sha256=actual_sha256,
                )
            if actual_sha256 != attempt.declared_sha256:
                return _fail_validation(
                    member=validating,
                    attempt=attempt,
                    datasets=datasets,
                    storage=storage,
                    now=now,
                    code=DatasetRefusalCode.SHA256_MISMATCH,
                    detail="对象内容摘要与登记声明不一致",
                    recovery_action=RetryMode.UPLOAD.value,
                    actual_size=stat.size,
                    actual_sha256=actual_sha256,
                )
            stream.seek(0)
            if _looks_like_archive(stream):
                return _fail_validation(
                    member=validating,
                    attempt=attempt,
                    datasets=datasets,
                    storage=storage,
                    now=now,
                    code=DatasetRefusalCode.ARCHIVE_CONTENT_REJECTED,
                    detail="压缩包或伪装成视频的压缩包不允许导入",
                    recovery_action=RetryMode.UPLOAD.value,
                    actual_size=stat.size,
                    actual_sha256=actual_sha256,
                )
            metadata = probe.probe(temporary.name)
            if not _valid_media_metadata(metadata):
                return _fail_validation(
                    member=validating,
                    attempt=attempt,
                    datasets=datasets,
                    storage=storage,
                    now=now,
                    code=DatasetRefusalCode.INVALID_MEDIA,
                    detail="媒体时长或编码信息无效",
                    recovery_action=RetryMode.UPLOAD.value,
                    actual_size=stat.size,
                    actual_sha256=actual_sha256,
                )
            if _canonical_codec(metadata.codec) not in {
                _canonical_codec(item) for item in supported_codecs
            }:
                return _fail_validation(
                    member=validating,
                    attempt=attempt,
                    datasets=datasets,
                    storage=storage,
                    now=now,
                    code=DatasetRefusalCode.UNSUPPORTED_CODEC,
                    detail=f"不支持的视频编码：{metadata.codec}",
                    recovery_action=RetryMode.UPLOAD.value,
                    actual_size=stat.size,
                    actual_sha256=actual_sha256,
                )

            final_object_key = _final_object_key(attempt)
            try:
                stream.seek(0)
                with storage.writing(object_key=final_object_key) as finalized_sink:
                    shutil.copyfileobj(stream, finalized_sink, length=_STREAM_COPY_BYTES)
                final_stat = storage.stat(object_key=final_object_key)
                with tempfile.NamedTemporaryFile(
                    mode="w+b", suffix=".registered-video"
                ) as finalized:
                    finalized_stream = cast(BinaryIO, finalized)
                    storage.download_to(
                        object_key=final_object_key,
                        destination=finalized_stream,
                    )
                    finalized_stream.flush()
                    finalized_stream.seek(0)
                    finalized_size, finalized_sha256 = _sha256_stream(finalized_stream)
            except (ObjectNotFoundError, ObjectStorageUnavailableError, OSError):
                failed = _fail_validation(
                    member=validating,
                    attempt=attempt,
                    datasets=datasets,
                    storage=storage,
                    now=now,
                    code=DatasetRefusalCode.STORAGE_UNAVAILABLE,
                    detail="无法读取已保存的定稿对象，请稍后重新校验",
                    recovery_action=RetryMode.VALIDATION.value,
                    actual_size=downloaded_size,
                    actual_sha256=actual_sha256,
                )
                _cleanup_objects(
                    storage=storage,
                    object_keys=(final_object_key,),
                    member_id=member.id,
                    attempt_id=attempt.id,
                    event="dataset.video_validation.final_object_cleanup_failed",
                )
                return failed
            if (
                final_stat.size != downloaded_size
                or finalized_size != downloaded_size
                or finalized_sha256 != actual_sha256
            ):
                failed = _fail_validation(
                    member=validating,
                    attempt=attempt,
                    datasets=datasets,
                    storage=storage,
                    now=now,
                    code=DatasetRefusalCode.STORAGE_UNAVAILABLE,
                    detail="定稿对象内容与已校验内容不一致，请稍后重新校验",
                    recovery_action=RetryMode.VALIDATION.value,
                    actual_size=downloaded_size,
                    actual_sha256=actual_sha256,
                )
                _cleanup_objects(
                    storage=storage,
                    object_keys=(final_object_key,),
                    member_id=member.id,
                    attempt_id=attempt.id,
                    event="dataset.video_validation.final_object_cleanup_failed",
                )
                return failed
    except ObjectNotFoundError:
        return _fail_validation(
            member=validating,
            attempt=attempt,
            datasets=datasets,
            storage=storage,
            now=now,
            code=DatasetRefusalCode.OBJECT_NOT_FOUND,
            detail="校验读取时对象已不存在，请重新上传",
            recovery_action=RetryMode.UPLOAD.value,
        )
    except (ObjectStorageUnavailableError, OSError):
        return _fail_validation(
            member=validating,
            attempt=attempt,
            datasets=datasets,
            storage=storage,
            now=now,
            code=DatasetRefusalCode.STORAGE_UNAVAILABLE,
            detail="训练素材存储暂时不可用，请稍后重新校验",
            recovery_action=RetryMode.VALIDATION.value,
        )
    except MediaProbeUnavailableError:
        return _fail_validation(
            member=validating,
            attempt=attempt,
            datasets=datasets,
            storage=storage,
            now=now,
            code=DatasetRefusalCode.MEDIA_PROBE_UNAVAILABLE,
            detail="媒体探测暂时不可用，请稍后重新校验",
            recovery_action=RetryMode.VALIDATION.value,
            actual_size=stat.size,
            actual_sha256=actual_sha256,
        )
    except InvalidMediaError:
        return _fail_validation(
            member=validating,
            attempt=attempt,
            datasets=datasets,
            storage=storage,
            now=now,
            code=DatasetRefusalCode.INVALID_MEDIA,
            detail="文件不是含有效视频流的媒体",
            recovery_action=RetryMode.UPLOAD.value,
            actual_size=stat.size,
            actual_sha256=actual_sha256,
        )

    registered = replace(
        validating,
        status=MemberStatus.REGISTERED,
        actual_size=final_stat.size,
        actual_sha256=actual_sha256,
        duration_seconds=metadata.duration_seconds,
        codec=metadata.codec,
        container=metadata.container,
        object_key=final_object_key,
        object_version_id=final_object_key,
        failure_code=None,
        failure_detail=None,
        recovery_action=None,
        updated_at=now,
    )
    if not datasets.save_member(
        registered,
        expected_attempt_id=attempt.id,
        expected_updated_at=member.updated_at,
    ):
        current = datasets.member_by_id(member.id)
        if current is None or current.current_attempt_id != attempt.id:
            _cleanup_objects(
                storage=storage,
                object_keys=(final_object_key, attempt.object_key),
                member_id=member.id,
                attempt_id=attempt.id,
                event="dataset.video_validation.stale_object_cleanup_failed",
            )
        return _validation_result(current or member)
    datasets.save_attempt(
        replace(
            attempt,
            status=AttemptStatus.REGISTERED,
            validation_job_id=job.id,
            object_version_id=final_object_key,
        )
    )
    _cleanup_objects(
        storage=storage,
        object_keys=(attempt.object_key,) if final_object_key != attempt.object_key else (),
        member_id=member.id,
        attempt_id=attempt.id,
        event="dataset.video_validation.temporary_cleanup_failed",
    )
    _logger.info(
        "dataset.video_validation.succeeded",
        member_id=str(member.id),
        attempt_id=str(attempt.id),
        job_id=str(job.id),
    )
    return _validation_result(registered)


def _validate_upload_declaration(
    *,
    original_filename: str,
    source: str,
    declared_size: int,
    declared_sha256: str,
    idempotency_key: str | None,
    max_upload_bytes: int,
    upload_ttl_seconds: int,
) -> None:
    if (
        not original_filename
        or len(original_filename) > _MAX_FILENAME_LENGTH
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in original_filename)
    ):
        _refuse(
            DatasetRefusalCode.FILENAME_INVALID,
            "原始文件名不能为空、不能超过 255 个字符或包含控制字符",
        )
    if "/" in original_filename or "\\" in original_filename:
        _refuse(DatasetRefusalCode.FILENAME_INVALID, "原始文件名不能包含路径分隔符")
    if not source.strip() or len(source) > _MAX_SOURCE_LENGTH:
        _refuse(DatasetRefusalCode.SOURCE_INVALID, "视频来源不能为空且不能超过 255 个字符")
    if declared_size <= 0:
        _refuse(DatasetRefusalCode.SIZE_INVALID, "声明大小必须大于零")
    if max_upload_bytes <= 0 or declared_size > max_upload_bytes:
        _refuse(DatasetRefusalCode.SIZE_EXCEEDED, "视频超过当前部署的单视频大小上限")
    if len(declared_sha256) != 64 or any(
        character not in "0123456789abcdefABCDEF" for character in declared_sha256
    ):
        _refuse(DatasetRefusalCode.SHA256_INVALID, "sha256 必须是 64 位十六进制摘要")
    if original_filename.casefold().endswith(_ARCHIVE_SUFFIXES):
        _refuse(
            DatasetRefusalCode.ARCHIVE_REJECTED,
            "压缩包不允许导入，请逐个选择视频文件",
            recovery_action=RetryMode.UPLOAD.value,
        )
    _validate_idempotency_key(idempotency_key)
    if upload_ttl_seconds <= 0:
        _refuse(DatasetRefusalCode.STATE_CONFLICT, "上传授权有效期配置无效")


def _validate_idempotency_key(idempotency_key: str | None) -> None:
    if idempotency_key is not None and (
        not idempotency_key.strip() or len(idempotency_key) > _MAX_IDEMPOTENCY_LENGTH
    ):
        _refuse(DatasetRefusalCode.IDEMPOTENCY_CONFLICT, "幂等键不能为空且不能超过 255 个字符")


def _upload_instructions(
    *,
    dataset_id: UUID,
    member_id: UUID,
    attempt_id: UUID,
    object_key: str,
    max_upload_bytes: int,
    expires_at: datetime,
) -> UploadInstructions:
    """构造指向中心正式入口的流式上传说明；不签发对象存储直传地址。"""
    return UploadInstructions(
        method="PUT",
        url=(
            f"{_API_PREFIX}/training-datasets/{dataset_id}/members/{member_id}"
            f"/attempts/{attempt_id}/content"
        ),
        fields={},
        headers={"Content-Type": "application/octet-stream"},
        expires_at=expires_at,
        max_bytes=max_upload_bytes,
        object_key=object_key,
    )


def _require_dataset(*, dataset_id: UUID, datasets: DatasetRepository) -> TrainingDataset:
    dataset = datasets.dataset_by_id(dataset_id)
    if dataset is None:
        _refuse(DatasetRefusalCode.DATASET_NOT_FOUND, "训练数据集不存在")
    return dataset


def _object_key(*, dataset_id: UUID, member_id: UUID, attempt_id: UUID) -> str:
    """服务端唯一生成对象键；调用者永远不能选择 bucket 或路径。"""
    return f"training-datasets/{dataset_id}/members/{member_id}/attempts/{attempt_id}/video"


def _final_object_key(attempt: UploadAttempt) -> str:
    """生成按上传尝试隔离、只有服务端会写入的定稿键。"""
    return (
        f"training-datasets/{attempt.dataset_id}/members/{attempt.member_id}/"
        f"attempts/{attempt.id}/registered-video"
    )


def _sha256_stream(stream: BinaryIO) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    reader = stream.read
    while chunk := reader(1024 * 1024):
        size += len(chunk)
        digest.update(chunk)
    return size, digest.hexdigest()


def _looks_like_archive(stream: BinaryIO) -> bool:
    header = stream.read(512)
    return header.startswith(_ARCHIVE_SIGNATURES) or (
        len(header) >= 265 and header[257:262] == b"ustar"
    )


def _canonical_codec(codec: str) -> str:
    """统一配置名与 ffprobe 返回的编码名。"""
    normalized = codec.strip().casefold()
    return _CODEC_ALIASES.get(normalized, normalized)


def _valid_media_metadata(metadata: MediaMetadata) -> bool:
    return (
        math.isfinite(metadata.duration_seconds)
        and metadata.duration_seconds > 0
        and bool(metadata.codec.strip())
        and bool(metadata.container.strip())
    )


def _cleanup_objects(
    *,
    storage: ObjectStorage,
    object_keys: tuple[str, ...],
    member_id: UUID,
    attempt_id: UUID,
    event: str,
) -> None:
    """在状态写回后清理对象；清理失败不回滚已经可查询的业务事实。"""
    for object_key in dict.fromkeys(object_keys):
        try:
            storage.delete(object_key=object_key)
        except (ObjectStorageUnavailableError, OSError):
            _logger.warning(
                event,
                member_id=str(member_id),
                attempt_id=str(attempt_id),
                object_key=object_key,
            )


def _fail_validation(
    *,
    member: DatasetMember,
    attempt: UploadAttempt,
    datasets: DatasetRepository,
    storage: ObjectStorage,
    now: datetime,
    code: DatasetRefusalCode,
    detail: str,
    recovery_action: str,
    actual_size: int | None = None,
    actual_sha256: str | None = None,
) -> ValidationResult:
    failed = replace(
        member,
        status=MemberStatus.FAILED,
        actual_size=actual_size,
        actual_sha256=actual_sha256,
        duration_seconds=None,
        codec=None,
        container=None,
        object_key=None,
        object_version_id=None,
        failure_code=code.value,
        failure_detail=detail,
        recovery_action=recovery_action,
        updated_at=now,
    )
    if datasets.save_member(
        failed,
        expected_attempt_id=attempt.id,
        expected_updated_at=member.updated_at,
    ):
        datasets.save_attempt(replace(attempt, status=AttemptStatus.FAILED))
        if recovery_action == RetryMode.UPLOAD.value:
            _cleanup_objects(
                storage=storage,
                object_keys=(attempt.object_key,),
                member_id=member.id,
                attempt_id=attempt.id,
                event="dataset.video_validation.temporary_cleanup_failed",
            )
        _logger.info(
            "dataset.video_validation.failed",
            member_id=str(member.id),
            attempt_id=str(attempt.id),
            error_code=code.value,
            recovery_action=recovery_action,
        )
        return _validation_result(failed)
    current = datasets.member_by_id(member.id)
    if recovery_action == RetryMode.UPLOAD.value and (
        current is None or current.current_attempt_id != attempt.id
    ):
        _cleanup_objects(
            storage=storage,
            object_keys=(attempt.object_key,),
            member_id=member.id,
            attempt_id=attempt.id,
            event="dataset.video_validation.stale_object_cleanup_failed",
        )
    return _validation_result(current or member)


def _validation_result(member: DatasetMember) -> ValidationResult:
    return ValidationResult(
        status=member.status,
        actual_size=member.actual_size,
        actual_sha256=member.actual_sha256,
        duration_seconds=member.duration_seconds,
        codec=member.codec,
        failure_code=member.failure_code,
        failure_detail=member.failure_detail,
        recovery_action=member.recovery_action,
    )


def _refuse(
    code: DatasetRefusalCode,
    detail: str,
    *,
    recovery_action: str | None = None,
) -> NoReturn:
    """记录稳定拒绝事件后抛出，拒绝本身不改变持久化状态。"""
    _logger.info(
        "dataset.operation.refused",
        error_code=code.value,
        detail=detail,
        recovery_action=recovery_action,
    )
    raise DatasetRefusedError(code, detail=detail, recovery_action=recovery_action)
