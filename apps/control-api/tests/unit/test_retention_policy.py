"""S040 保留策略纯规则与用例（不依赖数据库）。"""

from __future__ import annotations

from dataclasses import replace

import pytest
from auth_fakes import caller_holding

from factory_sop.auth.authorization import AuthorizationRefusedError
from factory_sop.auth.permissions import Permission
from factory_sop.retention.api import (
    DEFAULT_RETENTION_POLICY,
    RetentionCategory,
    RetentionMode,
    RetentionPolicy,
    RetentionPolicyConflictError,
    RetentionPolicyState,
    RetentionRule,
    category_for,
    check_reslice_window,
    get_policy,
    update_policy,
)


class FakeRetentionPolicyRepository:
    def __init__(self) -> None:
        self.state: RetentionPolicyState | None = None
        self.replace_calls = 0

    def get_global(self) -> RetentionPolicyState | None:
        return self.state

    def replace_if_current(self, *, expected_revision: int, value: RetentionPolicyState) -> bool:
        self.replace_calls += 1
        if (0 if self.state is None else self.state.revision) != expected_revision:
            return False
        self.state = value
        return True


def _policy(**overrides: RetentionRule) -> RetentionPolicy:
    return replace(DEFAULT_RETENTION_POLICY, **overrides)


def test_rules_are_distinct_strict_and_wire_round_trips() -> None:
    assert (
        len(
            {
                RetentionRule(RetentionMode.UNSET),
                RetentionRule(RetentionMode.PERMANENT),
                RetentionRule(RetentionMode.DISCARD),
                RetentionRule(RetentionMode.DURATION, 90 * 24 * 60 * 60),
            }
        )
        == 4
    )
    assert RetentionRule(RetentionMode.PERMANENT).to_wire() == {
        "mode": "permanent",
        "seconds": None,
    }
    assert RetentionPolicy.from_wire(DEFAULT_RETENTION_POLICY.to_wire()) == DEFAULT_RETENTION_POLICY
    with pytest.raises(ValueError, match="complete group"):
        RetentionPolicy.from_wire({"evidence_pass": {"mode": "permanent", "seconds": None}})
    with pytest.raises(ValueError, match="positive number of seconds"):
        RetentionRule(RetentionMode.DURATION, 0)


def test_policy_enforces_independence_unset_discard_and_detail_floor() -> None:
    policy = _policy(
        evidence_pass=RetentionRule(RetentionMode.DISCARD),
        evidence_fail=RetentionRule(RetentionMode.PERMANENT),
        evidence_indeterminate=RetentionRule(RetentionMode.DURATION, 30 * 24 * 60 * 60),
    )
    assert policy.evidence_pass.mode is RetentionMode.DISCARD
    assert policy.evidence_fail.mode is RetentionMode.PERMANENT
    assert policy.evidence_indeterminate.seconds == 30 * 24 * 60 * 60
    with pytest.raises(ValueError, match="must be set"):
        _policy(evidence_pass=RetentionRule(RetentionMode.UNSET))
    for name in ("evidence_fail", "evidence_indeterminate", "disposal", "local_reported"):
        with pytest.raises(ValueError, match="discard is only for pass"):
            _policy(**{name: RetentionRule(RetentionMode.DISCARD)})
    with pytest.raises(ValueError, match="shorter than compression age"):
        _policy(
            record_detail=RetentionRule(RetentionMode.DURATION, 60),
            record_compression_age=RetentionRule(RetentionMode.DURATION, 3600),
        )


def test_pure_entries_keep_latched_violations_and_bound_reslice() -> None:
    assert category_for("indeterminate", has_latched_violation=True) is RetentionCategory.FAIL
    assert category_for("pass", has_latched_violation=False) is RetentionCategory.PASS
    with pytest.raises(ValueError, match="unsupported judgment verdict"):
        category_for("unknown", has_latched_violation=False)
    check_reslice_window(604800, recording_window_seconds=604800)
    with pytest.raises(ValueError, match="must not exceed the recording window"):
        check_reslice_window(604801, recording_window_seconds=604800)
    with pytest.raises(ValueError, match="positive integer"):
        check_reslice_window(0, recording_window_seconds=604800)


def test_usecases_default_fallback_permission_and_cas() -> None:
    repository = FakeRetentionPolicyRepository()
    state = get_policy(caller_holding(Permission.RETENTION_POLICY_VIEW), repository)
    assert state.policy == DEFAULT_RETENTION_POLICY
    assert state.revision == 0
    with pytest.raises(AuthorizationRefusedError):
        get_policy(caller_holding(), repository)
    caller = caller_holding(Permission.RETENTION_POLICY_EDIT)
    created = update_policy(
        caller, DEFAULT_RETENTION_POLICY, expected_revision=0, repository=repository
    )
    assert created.revision == 1
    assert repository.replace_calls == 1
    with pytest.raises(RetentionPolicyConflictError):
        update_policy(caller, DEFAULT_RETENTION_POLICY, expected_revision=0, repository=repository)
    denied = FakeRetentionPolicyRepository()
    with pytest.raises(AuthorizationRefusedError):
        update_policy(
            caller_holding(Permission.RETENTION_POLICY_VIEW),
            DEFAULT_RETENTION_POLICY,
            expected_revision=0,
            repository=denied,
        )
    assert denied.replace_calls == 0
