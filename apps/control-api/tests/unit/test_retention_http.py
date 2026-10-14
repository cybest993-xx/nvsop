"""S041：真实应用、会话与 CSRF 路径上的保留失败状态码。"""

from __future__ import annotations

import pytest
from test_authorization_is_enforced import Backend

from factory_sop.auth.adapters.cookies import CSRF_COOKIE, CSRF_HEADER
from factory_sop.auth.permissions import Permission
from factory_sop.retention.api import DEFAULT_RETENTION_POLICY


@pytest.mark.parametrize(
    ("mode", "overrides", "revision", "status", "error_code"),
    [
        ("follow_global", None, 1, 409, "RETENTION_POLICY_CONFLICT"),
        ("custom", None, 0, 422, "RETENTION_POLICY_INVALID"),
        ("follow_global", DEFAULT_RETENTION_POLICY.to_wire(), 0, 422, "RETENTION_POLICY_INVALID"),
    ],
)
def test_station_policy_failures_are_explicit_http_refusals(
    mode: str,
    overrides: dict[str, object] | None,
    revision: int,
    status: int,
    error_code: str,
) -> None:
    backend = Backend()
    backend.granted = frozenset(
        {Permission.RETENTION_POLICY_VIEW, Permission.RETENTION_POLICY_EDIT}
    )
    try:
        response = backend.client.put(
            f"/api/v1/retention/stations/{backend.station.id}",
            json={"mode": mode, "overrides": overrides},
            headers={
                CSRF_HEADER: backend.client.cookies[CSRF_COOKIE],
                "If-Match": str(revision),
            },
        )
        assert response.status_code == status
        assert response.json()["error_code"] == error_code
        assert backend.retention_overrides.records == {}
    finally:
        backend.client.close()
