from __future__ import annotations

import io
import json

from fastapi.testclient import TestClient
from pydantic import SecretStr

from factory_sop.app import CORRELATION_ID_HEADER, create_app
from factory_sop.observability import configure_logging
from factory_sop.settings import Settings


def settings() -> Settings:
    return Settings(
        log_level="info",
        database_host="postgres.internal",
        database_port=5432,
        database_name="factory_sop",
        database_user="factory_sop",
        database_password=SecretStr("hunter2"),
    )


def client() -> TestClient:
    # TestClient is httpx over an in-process ASGI transport: no port, no server.
    return TestClient(create_app(settings()))


def test_the_liveness_route_lives_under_the_fixed_api_prefix() -> None:
    # `/api/v1` is a fixed literal prefix, not a version axis (ADR-0003).
    response = client().get("/api/v1/liveness")

    assert response.status_code == 200
    assert response.json() == {"status": "alive"}


def test_an_unprefixed_path_is_not_served() -> None:
    assert client().get("/liveness").status_code == 404


def test_the_response_carries_the_correlation_id_of_its_request() -> None:
    response = client().get("/api/v1/liveness")

    assert response.headers[CORRELATION_ID_HEADER]


def test_an_inbound_correlation_id_is_carried_rather_than_replaced() -> None:
    # Nginx and the inference host both forward one. Replacing it would break the join
    # between the caller's line and ours, which is the whole point of the field.
    response = client().get("/api/v1/liveness", headers={CORRELATION_ID_HEADER: "0191aaaa"})

    assert response.headers[CORRELATION_ID_HEADER] == "0191aaaa"


def test_two_requests_get_distinct_correlation_ids() -> None:
    served = client()
    first = served.get("/api/v1/liveness")
    second = served.get("/api/v1/liveness")

    assert first.headers[CORRELATION_ID_HEADER] != second.headers[CORRELATION_ID_HEADER]


def test_a_request_logs_one_line_under_its_own_correlation_id() -> None:
    stream = io.StringIO()
    configure_logging(log_level="info", stream=stream)

    response = client().get("/api/v1/liveness", headers={CORRELATION_ID_HEADER: "0191bbbb"})

    logged = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert response.status_code == 200
    assert [(line["event"], line["correlation_id"]) for line in logged] == [
        ("http.request.completed", "0191bbbb")
    ]


def test_the_settings_object_is_reachable_from_the_application() -> None:
    # The composition root holds the resolved settings, so an adapter reads them from the
    # application rather than from the process environment.
    configured = settings()

    assert create_app(configured).state.settings == configured
