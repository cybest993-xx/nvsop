"""训练数据用途检查的纯规则与确定性快照。"""

from __future__ import annotations

import hashlib
import json
import math
import re
import string
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlsplit

from factory_sop.dataset.model import DdmVideoInput, VlmCandidate, VlmCandidateKind

_EPSILON = 1e-6


@dataclass(frozen=True, slots=True)
class UsageIssue:
    """一条可定位且带恢复提示的用途检查失败。"""

    code: str
    detail: str
    location: str
    retryable: bool = False
    recovery_action: str | None = "fix_input"


@dataclass(frozen=True, slots=True)
class UsageValidationResult:
    """用途检查的确定性结果；快照不含操作者和执行时间。"""

    passed: bool
    issues: tuple[UsageIssue, ...]
    input_snapshot: dict[str, Any]
    input_digest: str
    summary: dict[str, int]


class DdmReaderInputError(Exception):
    """冻结输入不符合 NVIDIA DDM 读取器契约。"""


class DdmReaderUnavailableError(Exception):
    """NVIDIA DDM 读取器或其运行时依赖暂时不可用。"""


class DdmSampleClassEmptyError(DdmReaderInputError):
    """NVIDIA DDM 读取器无法产生某一训练样本类别。"""


class VlmReaderInputError(Exception):
    """冻结输入不符合 NVIDIA VLM 读取器契约。"""


class VlmReaderUnavailableError(Exception):
    """NVIDIA VLM 读取器或其运行时依赖暂时不可用。"""


def validate_ddm_input(
    videos: tuple[DdmVideoInput, ...],
    *,
    base_commit: str,
    contract_version: str,
) -> UsageValidationResult:
    """验证完整源视频和动作时间段，不运行 SOP 顺序判断。"""
    issues: list[UsageIssue] = []
    snapshot_videos: list[dict[str, Any]] = []
    segment_count = 0
    if not videos:
        issues.append(UsageIssue("DDM_EMPTY_DATASET", "数据集没有已登记视频", "dataset"))

    for video in sorted(videos, key=lambda item: str(item.member_id)):
        location = str(video.member_id)
        normalized_segments: list[dict[str, Any]] = []
        if not math.isfinite(video.duration_seconds) or video.duration_seconds <= 0:
            issues.append(UsageIssue("DDM_DURATION_INVALID", "视频时长必须是有限正数", location))
        if not video.object_version_id:
            issues.append(
                UsageIssue("DDM_SOURCE_VERSION_INVALID", "源视频对象代次不能为空", location)
            )
        if not _valid_sha256(video.source_sha256):
            issues.append(UsageIssue("DDM_SOURCE_DIGEST_INVALID", "源视频摘要格式无效", location))
        if not video.actions:
            issues.append(UsageIssue("DDM_ACTION_LIST_INVALID", "动作列表修订不能为空", location))
        if not video.segments:
            issues.append(UsageIssue("DDM_ANNOTATION_MISSING", "视频没有动作时间段标注", location))

        parsed: list[tuple[float, float, int, str, int]] = []
        for index, raw in enumerate(video.segments):
            try:
                start = _finite_number(raw.get("start", raw.get("start_timestamp")))
                end = _finite_number(raw.get("end", raw.get("end_timestamp")))
                action_index = raw.get("action_index", raw.get("actionIndex"))
                description = raw.get("action_description", raw.get("actionDescription"))
                if isinstance(action_index, bool) or not isinstance(action_index, int):
                    raise ValueError("action_index")
                if not isinstance(description, str):
                    raise ValueError("action_description")
            except (AttributeError, TypeError, ValueError):
                issues.append(
                    UsageIssue(
                        "DDM_SEGMENT_INVALID",
                        "动作时间段必须包含有限起止时刻、动作编号和描述",
                        f"{location}.segments[{index}]",
                    )
                )
                continue
            if start < 0 or end <= start or end > video.duration_seconds:
                issues.append(
                    UsageIssue(
                        "DDM_SEGMENT_OUT_OF_RANGE",
                        "动作时间段必须满足 0 ≤ start < end ≤ 视频时长",
                        f"{location}.segments[{index}]",
                    )
                )
            if action_index < 0 or action_index >= len(video.actions):
                issues.append(
                    UsageIssue(
                        "DDM_ACTION_INDEX_INVALID",
                        "动作编号不在冻结的动作列表修订内",
                        f"{location}.segments[{index}].action_index",
                    )
                )
            elif video.actions[action_index] != description:
                issues.append(
                    UsageIssue(
                        "DDM_ACTION_DESCRIPTION_MISMATCH",
                        "动作描述必须来自同一动作列表修订",
                        f"{location}.segments[{index}].action_description",
                    )
                )
            if "final segment" in description.casefold():
                issues.append(
                    UsageIssue(
                        "DDM_FINAL_SEGMENT_UNSUPPORTED",
                        "基座会过滤 final segment，不能静默丢弃有效标注",
                        f"{location}.segments[{index}].action_description",
                    )
                )
            parsed.append((start, end, action_index, description, index))
            normalized_segments.append(
                {
                    "start": start,
                    "end": end,
                    "action_index": action_index,
                    "action_description": description,
                }
            )

        parsed.sort(key=lambda item: (item[0], item[1], item[2], item[4]))
        cursor = 0.0
        previous_end = 0.0
        for start, end, _action_index, _description, index in parsed:
            if start > cursor + _EPSILON:
                issues.append(
                    UsageIssue(
                        "DDM_TIMELINE_GAP",
                        f"动作时间轴存在 {cursor:g}-{start:g} 秒空缺",
                        f"{location}.segments[{index}]",
                    )
                )
            if video.mode.value == "single_operator" and start < previous_end - _EPSILON:
                issues.append(
                    UsageIssue(
                        "DDM_SINGLE_OPERATOR_OVERLAP",
                        "单人模式动作时间段不能重叠；双人模式才允许并发区间",
                        location,
                    )
                )
            cursor = max(cursor, end)
            previous_end = max(previous_end, end)
        if parsed and video.duration_seconds - cursor > _EPSILON:
            issues.append(
                UsageIssue(
                    "DDM_TIMELINE_GAP",
                    f"动作时间轴在 {cursor:g}-{video.duration_seconds:g} 秒存在空缺",
                    location,
                )
            )
        segment_count += len(normalized_segments)
        snapshot_videos.append(
            {
                "member_id": location,
                "object_version_id": video.object_version_id,
                "source_sha256": video.source_sha256,
                "duration_seconds": video.duration_seconds,
                "action_list_revision": video.action_list_revision,
                "annotation_revision": video.annotation_revision,
                "mode": video.mode.value,
                "actions": list(video.actions),
                "segments": sorted(
                    normalized_segments,
                    key=lambda item: (
                        item["start"],
                        item["end"],
                        item["action_index"],
                        item["action_description"],
                    ),
                ),
            }
        )

    snapshot = {
        "base_commit": base_commit,
        "contract_version": contract_version,
        "videos": snapshot_videos,
    }
    return _result(
        passed=not issues,
        issues=issues,
        snapshot=snapshot,
        summary={"video_count": len(videos), "segment_count": segment_count},
    )


def validate_vlm_input(
    candidate: VlmCandidate,
    *,
    actions: tuple[str, ...],
    registered_member_ids: set[Any],
) -> UsageValidationResult:
    """验证 VLM 记录、显式媒体映射和动作编号。"""
    issues: list[UsageIssue] = []
    media_by_key: dict[str, Any] = {}
    media_by_basename: dict[str, str] = {}
    for index, media in enumerate(candidate.media):
        if not safe_media_key(media.key):
            issues.append(
                UsageIssue(
                    "VLM_MEDIA_KEY_INVALID",
                    "媒体映射键不能是 URL、绝对路径或路径穿越",
                    f"media[{index}]",
                )
            )
        if media.key in media_by_key:
            issues.append(
                UsageIssue("VLM_MEDIA_KEY_DUPLICATE", "媒体映射键必须唯一", f"media[{index}].key")
            )
        media_by_key[media.key] = media
        basename = PurePosixPath(media.key).name.casefold()
        previous_key = media_by_basename.get(basename)
        if previous_key is not None and previous_key != media.key:
            issues.append(
                UsageIssue(
                    "VLM_MEDIA_BASENAME_DUPLICATE",
                    "不同映射键不能共享同一媒体 basename，基座会按 basename 索引",
                    f"media[{index}].key",
                )
            )
        media_by_basename[basename] = media.key
        if media.member_id not in registered_member_ids:
            issues.append(
                UsageIssue(
                    "VLM_MEDIA_NOT_REGISTERED",
                    "媒体必须绑定同一数据集内已登记的视频",
                    f"media[{index}]",
                )
            )
        if not _valid_sha256(media.source_sha256) or not media.source_object_version_id:
            issues.append(
                UsageIssue(
                    "VLM_MEDIA_FACT_INVALID",
                    "媒体必须保留已确认的对象代次和摘要",
                    f"media[{index}]",
                )
            )
        _valid_action_indices(
            media.action_indices,
            actions,
            f"media[{index}].action_indices",
            issues,
        )

    if not candidate.records:
        issues.append(UsageIssue("VLM_RECORDS_EMPTY", "没有登记任何 VLM 候选记录", "records"))
    for index, record in enumerate(candidate.records):
        location = f"records[{index}]"
        if not isinstance(record, dict):
            issues.append(UsageIssue("VLM_RECORD_INVALID", "候选记录必须是 JSON 对象", location))
            continue
        for field in sorted(set(record) - _VLM_RECORD_FIELDS):
            issues.append(
                UsageIssue(
                    "VLM_RECORD_FIELD_UNSUPPORTED",
                    "候选记录包含当前读取器未定义的语义字段",
                    f"{location}.{field}",
                )
            )
        metadata = record.get("meta")
        if isinstance(metadata, dict):
            for field in sorted(set(metadata) - _VLM_METADATA_FIELDS):
                issues.append(
                    UsageIssue(
                        "VLM_SAMPLING_METADATA_UNSUPPORTED",
                        "采样元数据包含当前读取器未定义的语义字段",
                        f"{location}.meta.{field}",
                    )
                )
        if record.get("image") or record.get("images"):
            issues.append(
                UsageIssue(
                    "VLM_IMAGE_UNSUPPORTED",
                    "训练数据集候选只能引用视频，不能混入图片媒体",
                    location,
                )
            )
        conversations = record.get("conversations")
        if not isinstance(conversations, list) or len(conversations) != 2:
            issues.append(
                UsageIssue(
                    "VLM_CONVERSATION_INVALID",
                    "基座当前只接受恰好两轮 human/gpt 对话",
                    f"{location}.conversations",
                )
            )
        else:
            expected_roles = ("human", "gpt")
            for turn_index, (turn, expected_role) in enumerate(
                zip(conversations, expected_roles, strict=True)
            ):
                if not isinstance(turn, dict) or turn.get("from") != expected_role:
                    issues.append(
                        UsageIssue(
                            "VLM_CONVERSATION_ROLE_INVALID",
                            f"第 {turn_index + 1} 轮角色必须是 {expected_role}",
                            f"{location}.conversations[{turn_index}]",
                        )
                    )
                elif not isinstance(turn.get("value"), str) or not turn["value"].strip():
                    issues.append(
                        UsageIssue(
                            "VLM_CONVERSATION_TEXT_EMPTY",
                            "对话文本不能为空",
                            f"{location}.conversations[{turn_index}].value",
                        )
                    )
                elif turn_index == 0 and not _without_media_tags(turn["value"]).strip():
                    issues.append(
                        UsageIssue(
                            "VLM_CONVERSATION_TEXT_EMPTY",
                            "移除视频标签后用户问题不能为空",
                            f"{location}.conversations[{turn_index}].value",
                        )
                    )
        if metadata is not None and not isinstance(metadata, dict):
            issues.append(
                UsageIssue(
                    "VLM_SAMPLING_METADATA_INVALID", "meta 必须是 JSON 对象", f"{location}.meta"
                )
            )
        references = _video_references(record, location, issues)
        for reference in references:
            if reference not in media_by_key:
                issues.append(
                    UsageIssue(
                        "VLM_MEDIA_MAPPING_MISSING",
                        "视频引用必须精确对应一条已登记媒体映射",
                        f"{location}.video",
                    )
                )
        for field in ("frame_cnts", "min_frames", "max_frames", "dynamic_sample"):
            _validate_sampling_field(record, field, location, issues)
        if candidate.kind is VlmCandidateKind.MCQ:
            record_mapped_indices = {
                action_index
                for reference in references
                if (mapped_media := media_by_key.get(reference)) is not None
                for action_index in mapped_media.action_indices
            }
            _validate_mcq_action_mapping(
                record=record,
                location=location,
                actions=actions,
                mapped_indices=record_mapped_indices,
                issues=issues,
            )
        raw_action_indices = record.get("action_indices")
        record_indices = _valid_action_indices(
            raw_action_indices,
            actions,
            f"{location}.action_indices",
            issues,
        )
        for reference in references:
            mapped_media = media_by_key.get(reference)
            if mapped_media is None:
                continue
            mapped_indices = mapped_media.action_indices
            if (
                candidate.kind is VlmCandidateKind.MCQ
                and raw_action_indices is None
                and not mapped_indices
            ):
                issues.append(
                    UsageIssue(
                        "VLM_ACTION_INDEX_MISSING",
                        "MCQ 记录必须绑定显式或已保存标注动作映射",
                        f"{location}.action_indices",
                    )
                )
            if record_indices and mapped_indices and tuple(record_indices) != tuple(mapped_indices):
                issues.append(
                    UsageIssue(
                        "VLM_ACTION_INDEX_MISMATCH",
                        "记录动作编号与媒体动作映射不一致",
                        f"{location}.action_indices",
                    )
                )

    snapshot = {
        "candidate_id": str(candidate.id),
        "dataset_id": str(candidate.dataset_id),
        "revision": candidate.revision,
        "kind": candidate.kind.value,
        "action_list_revision": candidate.action_list_revision,
        "records": [_json_value(record) for record in candidate.records],
        "media": [
            {
                "key": media.key,
                "member_id": str(media.member_id),
                "source_object_version_id": media.source_object_version_id,
                "source_sha256": media.source_sha256,
                "action_indices": list(media.action_indices),
                "annotation_submission_id": (
                    str(media.annotation_submission_id)
                    if media.annotation_submission_id is not None
                    else None
                ),
                "annotation_execution_id": (
                    str(media.annotation_execution_id)
                    if media.annotation_execution_id is not None
                    else None
                ),
                "clip_index": media.clip_index,
            }
            for media in sorted(candidate.media, key=lambda item: item.key)
        ],
    }
    return _result(
        passed=not issues,
        issues=issues,
        snapshot=snapshot,
        summary={"record_count": len(candidate.records), "media_count": len(candidate.media)},
    )


def canonical_json(value: object) -> bytes:
    """返回跨运行稳定的 UTF-8 JSON 字节。"""
    return json.dumps(
        _json_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _result(
    *,
    passed: bool,
    issues: list[UsageIssue],
    snapshot: dict[str, Any],
    summary: dict[str, int],
) -> UsageValidationResult:
    return UsageValidationResult(
        passed=passed,
        issues=tuple(issues),
        input_snapshot=snapshot,
        input_digest=hashlib.sha256(canonical_json(snapshot)).hexdigest(),
        summary=summary,
    )


def _finite_number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("number")
    return result


def _valid_action_indices(
    value: object,
    actions: tuple[str, ...],
    location: str,
    issues: list[UsageIssue],
) -> tuple[int, ...]:
    if value is None or value == () or value == []:
        return ()
    integer_items = (
        [item for item in value if isinstance(item, int) and not isinstance(item, bool)]
        if isinstance(value, (list, tuple))
        else []
    )
    if (
        not isinstance(value, (list, tuple))
        or len(integer_items) != len(value)
        or len(set(integer_items)) != len(integer_items)
        or any(item < 1 or item > len(actions) for item in integer_items)
    ):
        issues.append(
            UsageIssue(
                "VLM_ACTION_INDEX_INVALID",
                "显式动作编号必须是动作列表内的正整数",
                location,
            )
        )
        return ()
    return tuple(value)


def _valid_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in string.hexdigits for character in value)


def safe_media_key(value: str) -> bool:
    parsed = urlsplit(value)
    parts = PurePosixPath(value).parts
    if (
        not value
        or value in {".", ".."}
        or "\\" in value
        or "\x00" in value
        or parsed.scheme
        or parsed.netloc
        or value.startswith("/")
        or value.endswith("/")
        or not parts
    ):
        return False
    return all(part not in {"", ".", ".."} for part in parts)


_ACTION_CHOICE = re.compile(r"(?m)^\s*\((\d+)\)\s*([^\n]+)")
_VLM_RECORD_FIELDS = frozenset(
    {
        "id",
        "conversations",
        "video",
        "videos",
        "image",
        "images",
        "meta",
        "action_indices",
        "frame_cnts",
        "min_frames",
        "max_frames",
        "dynamic_sample",
    }
)
_VLM_METADATA_FIELDS = frozenset({"frame_cnts", "min_frames", "max_frames", "dynamic_sample"})


def _validate_mcq_action_mapping(
    *,
    record: dict[str, Any],
    location: str,
    actions: tuple[str, ...],
    mapped_indices: set[int],
    issues: list[UsageIssue],
) -> None:
    conversations = record.get("conversations")
    if not isinstance(conversations, list) or len(conversations) != 2:
        return
    question = conversations[0].get("value") if isinstance(conversations[0], dict) else None
    answer = conversations[1].get("value") if isinstance(conversations[1], dict) else None
    if not isinstance(question, str) or not isinstance(answer, str):
        return
    options = {int(index): text.strip() for index, text in _ACTION_CHOICE.findall(question)}
    answers = {int(index): text.strip() for index, text in _ACTION_CHOICE.findall(answer)}
    if not options:
        issues.append(
            UsageIssue(
                "VLM_MCQ_OPTIONS_INVALID",
                "MCQ 问题没有可核对的动作选项",
                f"{location}.conversations[0]",
            )
        )
        return
    for index, text in options.items():
        if (
            index < 1
            or index > len(actions)
            or _action_text(actions[index - 1]) != _action_text(text)
        ):
            issues.append(
                UsageIssue(
                    "VLM_MCQ_OPTIONS_INVALID",
                    "MCQ 选项与动作列表不一致",
                    f"{location}.conversations[0]",
                )
            )
    if not answers:
        issues.append(
            UsageIssue(
                "VLM_MCQ_ANSWER_INVALID", "MCQ 答案没有动作编号选项", f"{location}.conversations[1]"
            )
        )
        return
    if any(
        index not in options or _action_text(options[index]) != _action_text(text)
        for index, text in answers.items()
    ):
        issues.append(
            UsageIssue(
                "VLM_MCQ_ANSWER_INVALID",
                "MCQ 答案引用了未提供或不匹配的动作选项",
                f"{location}.conversations[1]",
            )
        )
    if mapped_indices and set(answers) != set(mapped_indices):
        issues.append(
            UsageIssue(
                "VLM_MCQ_ANSWER_MISMATCH",
                "MCQ 答案与媒体动作映射不一致",
                f"{location}.conversations[1]",
            )
        )


def _action_text(value: str) -> str:
    return re.sub(r"^\(\d+\)\s*", "", value).strip()


def _without_media_tags(value: str) -> str:
    return re.sub(r"(?:\n)?</?(?:image|video)>(?:\n)?", "", value)


def _video_references(record: dict[str, Any], location: str, issues: list[UsageIssue]) -> list[str]:
    singular = record.get("video")
    plural = record.get("videos")
    if singular is not None and plural is not None:
        issues.append(
            UsageIssue("VLM_VIDEO_REFERENCE_AMBIGUOUS", "不能同时提供 video 和 videos", location)
        )
        return []
    value = plural if plural is not None else singular
    if isinstance(value, str):
        references = [value]
    elif isinstance(value, list) and all(isinstance(item, str) for item in value):
        references = value
    else:
        references = []
    if not references:
        issues.append(
            UsageIssue(
                "VLM_VIDEO_REFERENCE_MISSING", "记录必须引用至少一段视频", f"{location}.video"
            )
        )
    return references


def _validate_sampling_field(
    record: dict[str, Any], field: str, location: str, issues: list[UsageIssue]
) -> None:
    metadata = record.get("meta")
    source = metadata if isinstance(metadata, dict) and field in metadata else record
    value = source.get(field)
    if value is None:
        return
    field_location = f"{location}.meta.{field}" if source is metadata else f"{location}.{field}"
    if field == "dynamic_sample":
        valid = isinstance(value, bool)
    elif field == "frame_cnts":
        valid = (
            isinstance(value, list)
            and bool(value)
            and all(
                isinstance(item, int) and not isinstance(item, bool) and item > 0 for item in value
            )
        )
    else:
        valid = isinstance(value, int) and not isinstance(value, bool) and value > 0
    if not valid:
        issues.append(
            UsageIssue("VLM_SAMPLING_METADATA_INVALID", "采样元数据类型或取值无效", field_location)
        )
    if (
        field == "max_frames"
        and isinstance(source.get("min_frames"), int)
        and isinstance(value, int)
        and source["min_frames"] > value
    ):
        issues.append(
            UsageIssue("VLM_SAMPLING_METADATA_INVALID", "min_frames 不能大于 max_frames", location)
        )


def _json_value(value: object) -> object:
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if hasattr(value, "value") and isinstance(value.value, str):
        return value.value
    return value
