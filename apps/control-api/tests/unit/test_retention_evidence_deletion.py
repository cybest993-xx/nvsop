"""#206 逐证据删除资格的纯规则：保护不因年龄解除，未知一律不删。

规则本身不读仓储、不读表：它只把各 owner 的事实映射成 ``eligible/protected/unknown`` 与机器原因码。
真实 PostgreSQL 与主机签名 HTTP 的边界证据在 ``test_evidence_deletion_protection`` 中。
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from factory_sop.retention.api import (
    DEFAULT_RETENTION_POLICY,
    EvidenceDeletionFacts,
    EvidenceDeletionReason,
    EvidenceMediaState,
    RetentionCategory,
    RetentionMode,
    RetentionRule,
    evidence_deletion_decision,
    evidence_retention_rule,
)
from nvsop_contracts import EvidenceDeletionDecision

_DAY = 24 * 60 * 60
# 保护用例一律用极长的年龄，证明保护与年龄无关；未知用例可以把 `age_seconds` 覆盖成 None。
_LONG_AGO: dict[str, Any] = {"age_seconds": float(10_000 * _DAY)}


def facts() -> EvidenceDeletionFacts:
    """一条"已过保留期、无任何保护"的可用引用；用例只覆盖自己关心的那一项。"""
    return EvidenceDeletionFacts(
        registered=True,
        media_state=EvidenceMediaState.AVAILABLE,
        source_matches=True,
        generation_matches=True,
        material_matches=True,
        review_state_known=True,
        review_active=False,
        reclip_pending=False,
        playback_active=False,
        claim_pending=False,
        station_known=True,
        station_active=True,
        instance_known=True,
        instance_closed=True,
        latched_violation=False,
        category=RetentionCategory.PASS,
        age_seconds=float(200 * _DAY),
    )


def decide(
    value: EvidenceDeletionFacts, rule: RetentionRule | None = None
) -> tuple[EvidenceDeletionDecision, EvidenceDeletionReason]:
    return evidence_deletion_decision(
        value,
        rule=rule
        if rule is not None
        else evidence_retention_rule(DEFAULT_RETENTION_POLICY, value.category),
    )


def test_expired_unprotected_evidence_is_the_only_eligible_case() -> None:
    decision, reason = decide(replace(facts(), age_seconds=float(91 * _DAY)))
    assert (decision, reason) == (EvidenceDeletionDecision.ELIGIBLE, EvidenceDeletionReason.EXPIRED)


def test_not_yet_expired_evidence_is_protected() -> None:
    decision, reason = decide(replace(facts(), age_seconds=float(89 * _DAY)))
    assert (decision, reason) == (
        EvidenceDeletionDecision.PROTECTED,
        EvidenceDeletionReason.NOT_EXPIRED,
    )


def test_permanent_rule_never_expires_and_discard_is_immediately_eligible() -> None:
    permanent, permanent_reason = decide(replace(facts(), category=RetentionCategory.FAIL))
    # `evidence_fail` 的代码默认是永久：类别本身不会因为年龄进入可删状态。
    assert (permanent, permanent_reason) == (
        EvidenceDeletionDecision.PROTECTED,
        EvidenceDeletionReason.RETENTION_PERMANENT,
    )
    discarded, discarded_reason = decide(
        replace(facts(), age_seconds=None), RetentionRule(RetentionMode.DISCARD)
    )
    assert (discarded, discarded_reason) == (
        EvidenceDeletionDecision.ELIGIBLE,
        EvidenceDeletionReason.RETENTION_DISCARD,
    )


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"instance_closed": False}, EvidenceDeletionReason.INSTANCE_OPEN),
        ({"latched_violation": True}, EvidenceDeletionReason.LATCHED_VIOLATION),
        ({"review_active": True}, EvidenceDeletionReason.REVIEW_IN_PROGRESS),
        ({"reclip_pending": True}, EvidenceDeletionReason.RECLIP_PENDING),
        ({"playback_active": True}, EvidenceDeletionReason.PLAYBACK_IN_PROGRESS),
        ({"claim_pending": True}, EvidenceDeletionReason.DELETION_CLAIM_PENDING),
    ],
)
def test_persistent_protection_holds_regardless_of_age(
    overrides: dict[str, Any], expected: EvidenceDeletionReason
) -> None:
    decision, reason = decide(replace(facts(), **(_LONG_AGO | overrides)))
    assert (decision, reason) == (EvidenceDeletionDecision.PROTECTED, expected)


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"registered": False}, EvidenceDeletionReason.EVIDENCE_UNREGISTERED),
        (
            {"media_state": EvidenceMediaState.DELETED},
            EvidenceDeletionReason.EVIDENCE_MEDIA_DELETED,
        ),
        ({"media_state": EvidenceMediaState.PENDING}, EvidenceDeletionReason.EVIDENCE_UNAVAILABLE),
        ({"media_state": EvidenceMediaState.FAILED}, EvidenceDeletionReason.EVIDENCE_UNAVAILABLE),
        ({"media_state": EvidenceMediaState.UNKNOWN}, EvidenceDeletionReason.EVIDENCE_UNAVAILABLE),
        ({"source_matches": False}, EvidenceDeletionReason.SOURCE_HOST_MISMATCH),
        ({"generation_matches": False}, EvidenceDeletionReason.GENERATION_MISMATCH),
        ({"material_matches": False}, EvidenceDeletionReason.MATERIAL_MISMATCH),
        ({"review_state_known": False}, EvidenceDeletionReason.REVIEW_STATE_UNKNOWN),
        ({"station_known": False}, EvidenceDeletionReason.STATION_UNKNOWN),
        ({"station_active": False}, EvidenceDeletionReason.STATION_DEACTIVATED),
        ({"instance_known": False}, EvidenceDeletionReason.INSTANCE_UNKNOWN),
        ({"category": None}, EvidenceDeletionReason.INSTANCE_CATEGORY_UNKNOWN),
        ({"age_seconds": None}, EvidenceDeletionReason.AGE_UNKNOWN),
    ],
)
def test_unknown_facts_are_never_eligible(
    overrides: dict[str, Any], expected: EvidenceDeletionReason
) -> None:
    decision, reason = decide(replace(facts(), **(_LONG_AGO | overrides)))
    assert (decision, reason) == (EvidenceDeletionDecision.UNKNOWN, expected)


def test_latched_violation_keeps_its_own_reason() -> None:
    # 锁存违规是不可撤销的事实：它给出自己的原因码，不被其它同时成立的事实掩盖。
    decision, reason = decide(replace(facts(), instance_closed=False, latched_violation=True))
    assert (decision, reason) == (
        EvidenceDeletionDecision.PROTECTED,
        EvidenceDeletionReason.LATCHED_VIOLATION,
    )


def test_unknown_review_state_outranks_a_pending_claim() -> None:
    # 未知复核状态意味着"不能证明没有在审复核"，先于其它保护表达为未知（不删）。
    decision, reason = decide(replace(facts(), review_state_known=False, claim_pending=True))
    assert (decision, reason) == (
        EvidenceDeletionDecision.UNKNOWN,
        EvidenceDeletionReason.REVIEW_STATE_UNKNOWN,
    )


def test_unknown_category_is_not_treated_as_any_retention_class() -> None:
    decision, reason = decide(replace(facts(), category=None))
    assert (decision, reason) == (
        EvidenceDeletionDecision.UNKNOWN,
        EvidenceDeletionReason.INSTANCE_CATEGORY_UNKNOWN,
    )


def test_rule_lookup_is_per_category_and_never_guessed() -> None:
    duration = RetentionRule(RetentionMode.DURATION, 90 * _DAY)
    assert evidence_retention_rule(DEFAULT_RETENTION_POLICY, RetentionCategory.PASS) == duration
    assert (
        evidence_retention_rule(DEFAULT_RETENTION_POLICY, RetentionCategory.INDETERMINATE)
        == duration
    )
    assert evidence_retention_rule(DEFAULT_RETENTION_POLICY, RetentionCategory.FAIL) == (
        RetentionRule(RetentionMode.PERMANENT)
    )
    assert evidence_retention_rule(DEFAULT_RETENTION_POLICY, None) is None
