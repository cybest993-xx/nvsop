"""S041/#205：真实应用、会话与 CSRF 路径上的保留失败状态码与影响确认绑定。"""

from __future__ import annotations

import io
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from unittest.mock import patch

import pytest
from test_authorization_is_enforced import Backend

from factory_sop.auth.adapters.cookies import CSRF_COOKIE, CSRF_HEADER
from factory_sop.auth.permissions import Permission
from factory_sop.observability import configure_logging
from factory_sop.retention.api import (
    DEFAULT_RETENTION_POLICY,
    RetentionMode,
    RetentionPolicyMode,
    RetentionRule,
    StationRetentionRecord,
)

_DAY = 24 * 60 * 60
_EDIT = frozenset({Permission.RETENTION_POLICY_VIEW, Permission.RETENTION_POLICY_EDIT})
# 只缩短记录明细：owner 事实可用，估算完整，可正常确认。
_RECORD_SHORTENING = replace(
    DEFAULT_RETENTION_POLICY,
    record_detail=RetentionRule(RetentionMode.DURATION, 40 * _DAY),
)
# 缩短推理机本地已上报数据：该字段归边缘本地 SQLite，中心没有已接通的只读统计通道。
_LOCAL_SHORTENING = replace(
    DEFAULT_RETENTION_POLICY,
    local_reported=RetentionRule(RetentionMode.DURATION, _DAY),
)


def _headers(backend: Backend) -> dict[str, str]:
    return {CSRF_HEADER: backend.client.cookies[CSRF_COOKIE]}


def _estimate_station(backend: Backend, policy: dict[str, object] | None = None) -> dict[str, Any]:
    response = backend.client.post(
        f"/api/v1/retention/stations/{backend.station.id}/impact-estimate",
        json={"mode": "custom", "overrides": policy or DEFAULT_RETENTION_POLICY.to_wire()},
        headers=_headers(backend),
    )
    assert response.status_code == 200, response.text
    return cast(dict[str, Any], response.json())


@pytest.mark.parametrize(
    ("mode", "overrides", "error_code"),
    [
        ("custom", None, "RETENTION_POLICY_INVALID"),
        ("follow_global", DEFAULT_RETENTION_POLICY.to_wire(), "RETENTION_POLICY_INVALID"),
    ],
)
def test_invalid_station_selections_are_refused_before_any_confirmation(
    mode: str, overrides: dict[str, object] | None, error_code: str
) -> None:
    backend = Backend()
    backend.granted = _EDIT
    try:
        response = backend.client.put(
            f"/api/v1/retention/stations/{backend.station.id}",
            json={"mode": mode, "overrides": overrides},
            headers={**_headers(backend), "If-Match": "0"},
        )
        assert response.status_code == 422, response.text
        assert response.json()["error_code"] == error_code
        assert backend.retention_overrides.records == {}
    finally:
        backend.client.close()


@pytest.mark.parametrize("path", ["/api/v1/retention/policy", None])
def test_confirmation_without_an_estimate_token_is_refused(path: str | None) -> None:
    backend = Backend()
    backend.granted = _EDIT
    target = path or f"/api/v1/retention/stations/{backend.station.id}"
    body: dict[str, object] = (
        {"policy": _RECORD_SHORTENING.to_wire()}
        if path is not None
        else {"mode": "custom", "overrides": _RECORD_SHORTENING.to_wire()}
    )
    try:
        response = backend.client.put(
            target, json=body, headers={**_headers(backend), "If-Match": "0"}
        )
        assert response.status_code == 422, response.text
        assert response.json()["error_code"] == "RETENTION_ESTIMATE_REQUIRED"
        assert backend.retention_overrides.records == {}
        assert backend.retention_policies.state is None
    finally:
        backend.client.close()


def test_an_unestimated_edge_field_cannot_be_confirmed() -> None:
    """#205 复现信号：local_reported 缩短在缺只读统计通道时必须拒绝，不得按零放行。"""
    backend = Backend()
    backend.granted = _EDIT
    try:
        body = backend.client.post(
            "/api/v1/retention/policy/impact-estimate",
            json={"policy": _LOCAL_SHORTENING.to_wire()},
            headers=_headers(backend),
        )
        assert body.status_code == 200, body.text
        estimate = body.json()
        assert estimate["complete"] is False
        assert (
            "local_reported:edge_local_usage_channel_unavailable" in estimate["incomplete_reasons"]
        )
        assert estimate["fields"][0]["field"] == "local_reported"
        assert estimate["fields"][0]["exposed"] is None

        confirmed = backend.client.put(
            "/api/v1/retention/policy",
            json={"policy": _LOCAL_SHORTENING.to_wire()},
            headers={
                **_headers(backend),
                "If-Match": "0",
                "X-Retention-Estimate": estimate["estimate_token"],
            },
        )
        assert confirmed.status_code >= 400, confirmed.text
        assert confirmed.json()["error_code"] == "RETENTION_ESTIMATE_INCOMPLETE"
        assert "local_reported" in confirmed.json()["detail"]
        assert backend.retention_policies.state is None
    finally:
        backend.client.close()


def test_a_changed_target_invalidates_the_previous_estimate() -> None:
    backend = Backend()
    backend.granted = _EDIT
    try:
        token = _estimate_station(backend, _RECORD_SHORTENING.to_wire())["estimate_token"]
        changed = replace(
            _RECORD_SHORTENING,
            record_detail=RetentionRule(RetentionMode.DURATION, 30 * _DAY),
        )
        response = backend.client.put(
            f"/api/v1/retention/stations/{backend.station.id}",
            json={"mode": "custom", "overrides": changed.to_wire()},
            headers={**_headers(backend), "If-Match": "0", "X-Retention-Estimate": token},
        )
        assert response.status_code == 409, response.text
        assert response.json()["error_code"] == "RETENTION_ESTIMATE_STALE"
        assert backend.retention_overrides.records == {}
    finally:
        backend.client.close()


def test_a_concurrent_baseline_change_is_a_conflict_not_a_blind_sign() -> None:
    backend = Backend()
    backend.granted = _EDIT
    try:
        token = _estimate_station(backend, _RECORD_SHORTENING.to_wire())["estimate_token"]
        # 并发修改：另一名操作员先建立了该工位覆盖记录（revision 1）。
        backend.retention_overrides.records[backend.station.id] = StationRetentionRecord(
            station_id=backend.station.id,
            mode=RetentionPolicyMode.CUSTOM,
            overrides=_RECORD_SHORTENING,
            revision=1,
        )
        response = backend.client.put(
            f"/api/v1/retention/stations/{backend.station.id}",
            json={"mode": "custom", "overrides": _RECORD_SHORTENING.to_wire()},
            headers={**_headers(backend), "If-Match": "0", "X-Retention-Estimate": token},
        )
        assert response.status_code == 409, response.text
        # 该工位基准已是并发写入的版本：旧确认不能盲签。
        assert response.json()["error_code"] in {
            "RETENTION_POLICY_CONFLICT",
            "RETENTION_ESTIMATE_STALE",
        }
        assert backend.retention_overrides.records[backend.station.id].revision == 1
    finally:
        backend.client.close()


def test_confirming_the_station_group_writes_only_the_override_record() -> None:
    backend = Backend()
    backend.granted = _EDIT
    try:
        estimate = _estimate_station(backend)
        response = backend.client.put(
            f"/api/v1/retention/stations/{backend.station.id}",
            json={"mode": "custom", "overrides": DEFAULT_RETENTION_POLICY.to_wire()},
            headers={
                **_headers(backend),
                "If-Match": "0",
                "X-Retention-Estimate": estimate["estimate_token"],
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["revision"] == 1
        record = backend.retention_overrides.records[backend.station.id]
        assert record.revision == 1
        assert record.mode.value == "custom"
    finally:
        backend.client.close()


def test_global_policy_estimate_reports_completeness_and_confirms() -> None:
    backend = Backend()
    backend.granted = _EDIT
    try:
        estimate = backend.client.post(
            "/api/v1/retention/policy/impact-estimate",
            json={"policy": _RECORD_SHORTENING.to_wire()},
            headers=_headers(backend),
        )
        assert estimate.status_code == 200, estimate.text
        assert estimate.json()["complete"] is True
        assert estimate.json()["incomplete_reasons"] == []
        assert backend.retention_policies.state is None

        confirmed = backend.client.put(
            "/api/v1/retention/policy",
            json={"policy": _RECORD_SHORTENING.to_wire()},
            headers={
                **_headers(backend),
                "If-Match": "0",
                "X-Retention-Estimate": estimate.json()["estimate_token"],
            },
        )
        assert confirmed.status_code == 200, confirmed.text
        assert confirmed.json()["revision"] == 1
    finally:
        backend.client.close()


def test_an_incomplete_estimate_changes_nothing_and_reports_its_reason() -> None:
    backend = Backend()
    backend.granted = _EDIT
    try:
        estimate = backend.client.post(
            f"/api/v1/retention/stations/{backend.station.id}/impact-estimate",
            json={"mode": "custom", "overrides": _LOCAL_SHORTENING.to_wire()},
            headers=_headers(backend),
        )
        assert estimate.status_code == 200, estimate.text
        body = estimate.json()
        assert body["complete"] is False
        # 估算只读：不写覆盖记录，也不写策略。
        assert backend.retention_overrides.records == {}
        assert backend.retention_policies.state is None
        assert all(value["exposed"] is None for value in body["fields"])
    finally:
        backend.client.close()


def test_a_confirmed_change_emits_a_diagnostic_with_operator_values_and_estimate() -> None:
    stream = io.StringIO()
    configure_logging(log_level="info", stream=stream)
    backend = Backend()
    backend.granted = _EDIT
    try:
        estimate = _estimate_station(backend, _RECORD_SHORTENING.to_wire())
        response = backend.client.put(
            f"/api/v1/retention/stations/{backend.station.id}",
            json={"mode": "custom", "overrides": _RECORD_SHORTENING.to_wire()},
            headers={
                **_headers(backend),
                "If-Match": "0",
                "X-Retention-Estimate": estimate["estimate_token"],
            },
        )
        assert response.status_code == 200, response.text
    finally:
        backend.client.close()

    emitted = [json.loads(line) for line in stream.getvalue().splitlines() if line]
    changed = [line for line in emitted if line["event"] == "retention.policy.changed"]
    assert len(changed) == 1
    event = changed[0]
    assert event["module"] == "retention"
    assert event["scope"] == "station"
    assert event["station_id"] == str(backend.station.id)
    assert event["operator_id"] == str(backend.actor.id)
    assert event["operator"] == "系统管理员"
    assert event["before"] is None
    assert event["after"]["mode"] == "custom"
    assert event["estimate"]["digest"]
    assert event["estimate"]["complete"] is True
    # 不泄密：诊断不含会话/CSRF 令牌或媒体内容。
    serialized = json.dumps(event).lower()
    assert "csrf" not in serialized
    assert "session" not in serialized


def test_missing_confirmation_ttl_refuses_shortening_but_allows_extension() -> None:
    backend = Backend()
    backend.granted = _EDIT
    backend.app.state.settings = backend.app.state.settings.model_copy(
        update={"retention_estimate_ttl_seconds": None}
    )
    try:
        for target, expected in (
            (_RECORD_SHORTENING, 409),
            (
                replace(
                    DEFAULT_RETENTION_POLICY,
                    record_detail=RetentionRule(RetentionMode.DURATION, 120 * _DAY),
                ),
                200,
            ),
        ):
            estimate = backend.client.post(
                "/api/v1/retention/policy/impact-estimate",
                json={"policy": target.to_wire()},
                headers=_headers(backend),
            )
            assert estimate.status_code == 200, estimate.text
            assert backend.retention_policies.state is None
            confirmed = backend.client.put(
                "/api/v1/retention/policy",
                json={"policy": target.to_wire()},
                headers={
                    **_headers(backend),
                    "If-Match": "0",
                    "X-Retention-Estimate": estimate.json()["estimate_token"],
                },
            )
            assert confirmed.status_code == expected, confirmed.text
            if expected == 409:
                assert confirmed.json()["error_code"] == "RETENTION_ESTIMATE_TTL_UNCONFIGURED"
                assert backend.retention_policies.state is None
            else:
                assert backend.retention_policies.state is not None
                assert backend.retention_policies.state.policy == target
    finally:
        backend.client.close()


@pytest.mark.parametrize("station_scope", [False, True])
def test_confirmation_ttl_expiry_requires_a_fresh_estimate(station_scope: bool) -> None:
    backend = Backend()
    backend.granted = _EDIT
    backend.app.state.settings = backend.app.state.settings.model_copy(
        update={"retention_estimate_ttl_seconds": 60}
    )
    route = (
        f"/api/v1/retention/stations/{backend.station.id}"
        if station_scope
        else "/api/v1/retention/policy"
    )
    body = (
        {"mode": "custom", "overrides": _RECORD_SHORTENING.to_wire()}
        if station_scope
        else {"policy": _RECORD_SHORTENING.to_wire()}
    )
    start = datetime(2026, 10, 14, 12, tzinfo=UTC)
    try:
        with patch("factory_sop.retention.adapters.routes.datetime") as clock:
            clock.now.return_value = start
            estimate = backend.client.post(
                f"{route}/impact-estimate", json=body, headers=_headers(backend)
            )
            assert estimate.status_code == 200, estimate.text
            clock.now.return_value = start + timedelta(seconds=60)
            expired = backend.client.put(
                route,
                json=body,
                headers={
                    **_headers(backend),
                    "If-Match": "0",
                    "X-Retention-Estimate": estimate.json()["estimate_token"],
                },
            )
            assert expired.status_code == 409, expired.text
            assert expired.json()["error_code"] == "RETENTION_ESTIMATE_STALE"
            assert backend.retention_policies.state is None
            assert backend.retention_overrides.records == {}
            fresh = backend.client.post(
                f"{route}/impact-estimate", json=body, headers=_headers(backend)
            )
            assert fresh.status_code == 200, fresh.text
            clock.now.return_value = start + timedelta(seconds=119)
            confirmed = backend.client.put(
                route,
                json=body,
                headers={
                    **_headers(backend),
                    "If-Match": "0",
                    "X-Retention-Estimate": fresh.json()["estimate_token"],
                },
            )
            assert confirmed.status_code == 200, confirmed.text
            assert confirmed.json()["revision"] == 1
    finally:
        backend.client.close()
