"""用途检查的纯 dataset seam。"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

from factory_sop.dataset.model import (
    AnnotationMode,
    DdmVideoInput,
    VlmCandidate,
    VlmCandidateKind,
    VlmMediaReference,
)
from factory_sop.dataset.usage import (
    UsageIssue,
    UsageValidationResult,
    canonical_json,
    safe_media_key,
    validate_ddm_input,
    validate_vlm_input,
)

NOW = datetime(2026, 9, 11, 1, 0, tzinfo=UTC)
DATASET_ID = UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f201")
MEMBER_ID = UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f202")
EXPECTED_DDM_INPUT_DIGEST = (
    "94231c8492f85af91c70ee2b5e34c628a8f86eb8be34f7d27a1bae5d1fb0f4d5"  # pragma: allowlist secret
)


def _video(*, mode: AnnotationMode = AnnotationMode.SINGLE_OPERATOR) -> DdmVideoInput:
    return DdmVideoInput(
        member_id=MEMBER_ID,
        object_key="object-1",
        source_sha256="a" * 64,
        duration_seconds=10.0,
        action_list_revision=1,
        annotation_revision=1,
        mode=mode,
        actions=("(1) 取料", "(2) 安装"),
        segments=(
            {"start": 0.0, "end": 4.0, "action_index": 0, "action_description": "(1) 取料"},
            {"start": 4.0, "end": 10.0, "action_index": 1, "action_description": "(2) 安装"},
        ),
    )


def test_ddm_keeps_two_operator_overlap_and_freezes_a_deterministic_input() -> None:
    value = _video(mode=AnnotationMode.TWO_OPERATOR)
    value = replace(
        value,
        segments=(
            value.segments[0],
            {"start": 3.0, "end": 10.0, "action_index": 1, "action_description": "(2) 安装"},
        ),
    )

    result = validate_ddm_input((value,), base_commit="base-commit", contract_version="ddm-v1")

    assert result == UsageValidationResult(
        passed=True,
        issues=(),
        input_snapshot={
            "base_commit": "base-commit",
            "contract_version": "ddm-v1",
            "videos": [
                {
                    "member_id": str(MEMBER_ID),
                    "object_version_id": "object-1",
                    "source_sha256": "a" * 64,
                    "duration_seconds": 10.0,
                    "action_list_revision": 1,
                    "annotation_revision": 1,
                    "mode": "two_operator",
                    "actions": ["(1) 取料", "(2) 安装"],
                    "segments": [
                        {
                            "start": 0.0,
                            "end": 4.0,
                            "action_index": 0,
                            "action_description": "(1) 取料",
                        },
                        {
                            "start": 3.0,
                            "end": 10.0,
                            "action_index": 1,
                            "action_description": "(2) 安装",
                        },
                    ],
                }
            ],
        },
        input_digest=EXPECTED_DDM_INPUT_DIGEST,
        summary={"video_count": 1, "segment_count": 2},
    )


def test_ddm_rejects_single_operator_overlap_instead_of_reinterpreting_it() -> None:
    value = _video()
    value = replace(
        value,
        segments=(
            value.segments[0],
            {"start": 3.0, "end": 10.0, "action_index": 1, "action_description": "(2) 安装"},
        ),
    )

    result = validate_ddm_input((value,), base_commit="base-commit", contract_version="ddm-v1")

    assert result.passed is False
    assert result.issues == (
        UsageIssue(
            "DDM_SINGLE_OPERATOR_OVERLAP",
            "单人模式动作时间段不能重叠；双人模式才允许并发区间",
            str(MEMBER_ID),
        ),
    )


def test_ddm_rejects_non_hex_source_digest_and_incomplete_timeline() -> None:
    value = replace(
        _video(),
        source_sha256="z" * 64,
        segments=(_video().segments[0],),
    )

    result = validate_ddm_input((value,), base_commit="base-commit", contract_version="ddm-v1")

    assert result.passed is False
    assert {issue.code for issue in result.issues} == {
        "DDM_SOURCE_DIGEST_INVALID",
        "DDM_TIMELINE_GAP",
    }


def test_media_mapping_rejects_dot_and_path_like_keys() -> None:
    assert safe_media_key(".") is False
    assert safe_media_key("..") is False
    assert safe_media_key("video.mp4") is True


def test_vlm_requires_explicit_registered_media_and_exact_two_turn_conversation() -> None:
    candidate = VlmCandidate(
        id=UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f203"),
        dataset_id=DATASET_ID,
        revision=1,
        kind=VlmCandidateKind.GQA,
        action_list_revision=1,
        records=(
            {
                "conversations": [
                    {"from": "human", "value": "<video>做什么？"},
                    {"from": "gpt", "value": "取料"},
                ],
                "video": "line-a.mp4",
            },
        ),
        media=(
            VlmMediaReference(
                key="line-a.mp4",
                member_id=MEMBER_ID,
                source_object_key="object-1",
                source_sha256="a" * 64,
            ),
        ),
        created_by=UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f204"),
        created_at=NOW,
    )

    result = validate_vlm_input(
        candidate,
        actions=("(1) 取料", "(2) 安装"),
        registered_member_ids={MEMBER_ID},
    )

    expected_snapshot = {
        "candidate_id": str(candidate.id),
        "dataset_id": str(candidate.dataset_id),
        "revision": 1,
        "kind": "gqa",
        "action_list_revision": 1,
        "records": [
            {
                "conversations": [
                    {"from": "human", "value": "<video>做什么？"},
                    {"from": "gpt", "value": "取料"},
                ],
                "video": "line-a.mp4",
            }
        ],
        "media": [
            {
                "key": "line-a.mp4",
                "member_id": str(MEMBER_ID),
                "source_object_version_id": "object-1",
                "source_sha256": "a" * 64,
                "action_indices": [],
                "annotation_submission_id": None,
                "annotation_execution_id": None,
                "clip_index": None,
            }
        ],
    }
    assert result == UsageValidationResult(
        passed=True,
        issues=(),
        input_snapshot=expected_snapshot,
        input_digest=hashlib.sha256(canonical_json(expected_snapshot)).hexdigest(),
        summary={"record_count": 1, "media_count": 1},
    )


def test_vlm_accepts_base_nested_sampling_metadata_and_mcq_answer_mapping() -> None:
    candidate = VlmCandidate(
        id=UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f209"),
        dataset_id=DATASET_ID,
        revision=1,
        kind=VlmCandidateKind.MCQ,
        action_list_revision=1,
        records=(
            {
                "conversations": [
                    {"from": "human", "value": "选择动作\n(1) 取料\n(2) 安装"},
                    {"from": "gpt", "value": "(1) 取料"},
                ],
                "video": "01_line-a_1_1.mp4",
                "meta": {
                    "frame_cnts": [8],
                    "min_frames": 4,
                    "max_frames": 8,
                    "dynamic_sample": True,
                },
            },
        ),
        media=(
            VlmMediaReference(
                key="01_line-a_1_1.mp4",
                member_id=MEMBER_ID,
                source_object_key="object-1",
                source_sha256="a" * 64,
                action_indices=(1,),
            ),
        ),
        created_by=UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f210"),
        created_at=NOW,
    )

    result = validate_vlm_input(
        candidate,
        actions=("(1) 取料", "(2) 安装"),
        registered_member_ids={MEMBER_ID},
    )

    assert result.passed is True


def test_vlm_rejects_empty_frame_counts() -> None:
    candidate = VlmCandidate(
        id=UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f215"),
        dataset_id=DATASET_ID,
        revision=1,
        kind=VlmCandidateKind.GQA,
        action_list_revision=1,
        records=(
            {
                "conversations": [
                    {"from": "human", "value": "选择动作"},
                    {"from": "gpt", "value": "取料"},
                ],
                "video": "line-a.mp4",
                "meta": {"frame_cnts": []},
            },
        ),
        media=(
            VlmMediaReference(
                key="line-a.mp4",
                member_id=MEMBER_ID,
                source_object_key="object-1",
                source_sha256="a" * 64,
            ),
        ),
        created_by=UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f216"),
        created_at=NOW,
    )

    result = validate_vlm_input(
        candidate,
        actions=("(1) 取料",),
        registered_member_ids={MEMBER_ID},
    )

    assert result.passed is False
    assert any(issue.code == "VLM_SAMPLING_METADATA_INVALID" for issue in result.issues)


def test_vlm_does_not_infer_action_indices_from_media_filename() -> None:
    candidate = VlmCandidate(
        id=UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f213"),
        dataset_id=DATASET_ID,
        revision=1,
        kind=VlmCandidateKind.MCQ,
        action_list_revision=1,
        records=(
            {
                "conversations": [
                    {"from": "human", "value": "选择动作\\n(1) 取料"},
                    {"from": "gpt", "value": "(1) 取料"},
                ],
                "video": "01_Install_8_2_11.mp4",
            },
        ),
        media=(
            VlmMediaReference(
                key="01_Install_8_2_11.mp4",
                member_id=MEMBER_ID,
                source_object_key="object-1",
                source_sha256="a" * 64,
            ),
        ),
        created_by=UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f214"),
        created_at=NOW,
    )

    result = validate_vlm_input(
        candidate,
        actions=("(1) 取料",),
        registered_member_ids={MEMBER_ID},
    )

    assert result.passed is False
    assert any(issue.code == "VLM_ACTION_INDEX_MISSING" for issue in result.issues)


def test_vlm_rejects_duplicate_basenames_even_when_mapping_keys_differ() -> None:
    media = tuple(
        VlmMediaReference(
            key=key,
            member_id=MEMBER_ID,
            source_object_key="object-1",
            source_sha256="a" * 64,
        )
        for key in ("videos/line-a.mp4", "clips/line-a.mp4")
    )
    candidate = VlmCandidate(
        id=UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f211"),
        dataset_id=DATASET_ID,
        revision=1,
        kind=VlmCandidateKind.GQA,
        action_list_revision=1,
        records=(),
        media=media,
        created_by=UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f212"),
        created_at=NOW,
    )

    result = validate_vlm_input(
        candidate,
        actions=("(1) 取料",),
        registered_member_ids={MEMBER_ID},
    )

    assert any(issue.code == "VLM_MEDIA_BASENAME_DUPLICATE" for issue in result.issues)


def test_vlm_rejects_image_only_records_and_empty_video_prompts() -> None:
    candidate = VlmCandidate(
        id=UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f207"),
        dataset_id=DATASET_ID,
        revision=1,
        kind=VlmCandidateKind.GQA,
        action_list_revision=1,
        records=(
            {
                "conversations": [
                    {"from": "human", "value": "<video>"},
                    {"from": "gpt", "value": "取料"},
                ],
                "images": ["frame.png"],
            },
        ),
        media=(),
        created_by=UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f208"),
        created_at=NOW,
    )

    result = validate_vlm_input(
        candidate,
        actions=("(1) 取料",),
        registered_member_ids={MEMBER_ID},
    )

    assert result.passed is False
    assert {issue.code for issue in result.issues} == {
        "VLM_IMAGE_UNSUPPORTED",
        "VLM_CONVERSATION_TEXT_EMPTY",
        "VLM_VIDEO_REFERENCE_MISSING",
    }


def test_vlm_rejects_action_numbers_outside_the_frozen_action_list() -> None:
    candidate = VlmCandidate(
        id=UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f205"),
        dataset_id=DATASET_ID,
        revision=1,
        kind=VlmCandidateKind.MCQ,
        action_list_revision=1,
        records=(
            {
                "conversations": [
                    {"from": "human", "value": "选择动作"},
                    {"from": "gpt", "value": "(3) 不存在"},
                ],
                "video": "line-a.mp4",
                "action_indices": [3],
            },
        ),
        media=(
            VlmMediaReference(
                key="line-a.mp4",
                member_id=MEMBER_ID,
                source_object_key="object-1",
                source_sha256="a" * 64,
            ),
        ),
        created_by=UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f206"),
        created_at=NOW,
    )

    result = validate_vlm_input(
        candidate,
        actions=("(1) 取料", "(2) 安装"),
        registered_member_ids={MEMBER_ID},
    )

    assert result.passed is False
    assert [issue.code for issue in result.issues] == [
        "VLM_MCQ_OPTIONS_INVALID",
        "VLM_ACTION_INDEX_INVALID",
    ]
