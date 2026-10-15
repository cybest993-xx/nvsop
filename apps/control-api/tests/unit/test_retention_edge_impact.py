"""只读统计的来源票据、真实 HTTP 边界、部分失败与来源绑定。"""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

import pytest
from auth_fakes import caller_holding

from factory_sop.auth.api import Caller, Permission
from factory_sop.auth.authorization import AuthorizationRefusedError
from factory_sop.retention.access import (
    ReportedImpactAccessRefusedError,
    check_reported_impact_context,
    issue_reported_impact_context,
)
from factory_sop.retention.adapters.edge_impact import HttpEdgeImpactGateway
from factory_sop.retention.api import EdgeImpactResult, EdgeImpactSource
from nvsop_contracts.retention_impact import ReportedImpactQuery

HOST = UUID("00000000-0000-4000-8000-000000000001")
OTHER = UUID("00000000-0000-4000-8000-000000000002")
NOW = datetime(2026, 10, 14, 12, tzinfo=UTC)
DAY = 86400
QUERY = ReportedImpactQuery(str(HOST), 7, "a" * 64, None, (str(OTHER),), 14 * DAY, DAY, NOW)
EDIT = caller_holding(Permission.RETENTION_POLICY_EDIT)
# 仅用于合成测试,不是部署凭据。
SECRET = "synthetic-retention-context-key"  # pragma: allowlist secret


def test_context_rechecks_current_permission_and_expires_at_configured_boundary() -> None:
    context = issue_reported_impact_context(
        EDIT,
        query=QUERY,
        fingerprint="session-fingerprint",
        secret=SECRET,
        now=100,
        ttl_seconds=60,
    )
    checked: list[str] = []

    def caller(fingerprint: str) -> Caller:
        checked.append(fingerprint)
        return EDIT

    assert (
        check_reported_impact_context(
            context=context,
            host_id=str(HOST),
            secret=SECRET,
            now=159,
            resolve_caller=caller,
        )
        == QUERY
    )
    assert checked == ["session-fingerprint"]
    with pytest.raises(ReportedImpactAccessRefusedError) as expired:
        check_reported_impact_context(
            context=context,
            host_id=str(HOST),
            secret=SECRET,
            now=160,
            resolve_caller=caller,
        )
    assert (expired.value.status, expired.value.code) == (410, "context_expired")
    assert checked == ["session-fingerprint"]
    with pytest.raises(ReportedImpactAccessRefusedError, match="session_invalid"):
        check_reported_impact_context(
            context=context,
            host_id=str(HOST),
            secret=SECRET,
            now=159,
            resolve_caller=lambda _: None,
        )
    with pytest.raises(AuthorizationRefusedError):
        check_reported_impact_context(
            context=context,
            host_id=str(HOST),
            secret=SECRET,
            now=159,
            resolve_caller=lambda _: caller_holding(Permission.RETENTION_POLICY_VIEW),
        )


def test_context_cannot_cross_host_signature_or_media_scope() -> None:
    context = issue_reported_impact_context(
        EDIT,
        query=QUERY,
        fingerprint="session-fingerprint",
        secret=SECRET,
        now=100,
        ttl_seconds=60,
    )
    encoded, _ = context.split(".")
    media_signature = hmac.new(SECRET.encode(), b"evidence\0" + encoded.encode(), hashlib.sha256)
    for ticket, host in (
        (context, str(OTHER)),
        (context + "x", str(HOST)),
        (encoded + "." + media_signature.hexdigest(), str(HOST)),
    ):
        with pytest.raises(ReportedImpactAccessRefusedError) as denied:
            check_reported_impact_context(
                context=ticket,
                host_id=host,
                secret=SECRET,
                now=101,
                resolve_caller=lambda _: pytest.fail("invalid ticket reached the session owner"),
            )
        assert (denied.value.status, denied.value.code) == (403, "access_denied")


@pytest.fixture
def source_server() -> Iterator[tuple[str, dict[str, Any]]]:
    state: dict[str, Any] = {"requests": [], "status": [200], "mismatch": False, "invalid": False}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            context = parse_qs(urlsplit(self.path).query)["context"][0]
            query = check_reported_impact_context(
                context=context,
                host_id=str(HOST) if not state["requests"] else str(OTHER),
                secret=SECRET,
                now=datetime.now(UTC).timestamp(),
                resolve_caller=lambda _: EDIT,
            )
            state["requests"].append(query)
            statuses = state["status"]
            status = statuses.pop(0) if len(statuses) > 1 else statuses[0]
            query_wire = query.to_wire()
            if state["mismatch"]:
                query_wire["configuration_revision"] = 8
            at = (NOW - timedelta(days=2)).isoformat()
            payload: dict[str, Any] = {
                "query": query_wire,
                "bytes_basis": "serialized_rows",
                "exposed": {"objects": 1, "known_bytes": 17, "earliest": at, "latest": at},
                "protected": {"objects": 2, "known_bytes": 29, "earliest": at, "latest": at},
            }
            if state["invalid"]:
                payload["exposed"]["earliest"] = None
            encoded = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Location", "/must-not-follow")
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, message_format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def gateway(
    origin: str, *, copies: int = 1, ttl: int | None = 60, timeout: float | None = 5
) -> HttpEdgeImpactGateway:
    class Sources:
        def lock_configuration(self, *, host_id: UUID) -> bool:
            return True

        def sources(self, *, station_id: UUID | None) -> tuple[EdgeImpactSource, ...]:
            return (EdgeImpactSource(HOST, 7, "a" * 64), EdgeImpactSource(OTHER, 7, "a" * 64))[
                :copies
            ]

    return HttpEdgeImpactGateway(
        sources=Sources(),
        release_connection=lambda: None,
        refresh_caller=lambda: EDIT,
        origins={str(HOST): origin, str(OTHER): origin},
        caller=EDIT,
        fingerprint="session-fingerprint",
        secret=SECRET,
        ttl_seconds=ttl,
        timeout_seconds=timeout,
    )


def estimate(owner: HttpEdgeImpactGateway) -> EdgeImpactResult:
    return owner.reported_impact(
        station_id=None,
        excluded_stations=frozenset({OTHER}),
        baseline_seconds=14 * DAY,
        proposed_seconds=DAY,
        reference_time=NOW,
    )


def test_real_http_counts_are_bound_to_query_and_source_configuration(
    source_server: tuple[str, dict[str, Any]],
) -> None:
    origin, state = source_server
    result = estimate(gateway(origin))
    assert result.unavailable_reason is None
    assert result.breakdown is not None
    assert result.breakdown.exposed.objects == 1
    assert result.breakdown.exposed.known_bytes == 17
    assert result.breakdown.protected.objects == 2
    assert result.sources == (EdgeImpactSource(HOST, 7, "a" * 64),)
    assert state["requests"] == [QUERY]


@pytest.mark.parametrize(
    ("flag", "status", "reason"),
    [
        ("mismatch", 200, "edge_estimate_invalid"),
        ("invalid", 200, "edge_estimate_invalid"),
        (None, 302, "edge_estimate_refused"),
        (None, 409, "edge_estimate_refused"),
    ],
)
def test_invalid_or_refused_sources_are_not_zero_or_retried(
    source_server: tuple[str, dict[str, Any]],
    flag: str | None,
    status: int,
    reason: str,
) -> None:
    origin, state = source_server
    if flag:
        state[flag] = True
    state["status"] = [status]
    result = estimate(gateway(origin))
    assert result.breakdown is None
    assert result.unavailable_reason == reason
    assert len(state["requests"]) == 1


def test_partial_success_does_not_return_a_confirmable_subtotal(
    source_server: tuple[str, dict[str, Any]],
) -> None:
    origin, state = source_server
    state["status"] = [200, 503]
    result = estimate(gateway(origin, copies=2))
    assert result.breakdown is None
    assert result.unavailable_reason == "edge_estimate_refused"
    assert len(state["requests"]) == 2


@pytest.mark.parametrize(("ttl", "timeout"), [(None, 5), (60, None)])
def test_unconfigured_transport_never_invents_a_duration_or_calls_a_source(
    source_server: tuple[str, dict[str, Any]],
    ttl: int | None,
    timeout: float | None,
) -> None:
    origin, state = source_server
    result = estimate(gateway(origin, ttl=ttl, timeout=timeout))
    assert result.breakdown is None
    assert result.unavailable_reason == "edge_estimate_transport_unconfigured"
    assert state["requests"] == []


def test_an_empty_source_set_is_unavailable_not_a_confirmable_zero() -> None:
    result = estimate(gateway("http://127.0.0.1:1", copies=0))
    assert result.breakdown is None
    assert result.unavailable_reason == "edge_sources_unknown"
    assert result.sources == ()
