from __future__ import annotations

import io
import json
from datetime import timedelta

from fastapi.testclient import TestClient
from pydantic import SecretStr

from factory_sop.app import CORRELATION_ID_HEADER, create_app
from factory_sop.auth.model import SessionPolicy
from factory_sop.observability import configure_logging
from factory_sop.problem import PROBLEM_MEDIA_TYPE
from factory_sop.settings import Settings


def settings() -> Settings:
    return Settings(
        log_level="info",
        database_host="postgres.internal",
        database_port=5432,
        database_name="factory_sop",
        database_user="factory_sop",
        database_password=SecretStr("hunter2"),
        session_idle_timeout_minutes=720,
        session_absolute_lifetime_minutes=43200,
        session_cookie_transport="require_https",
        csrf_secret=SecretStr("csrf-secret"),
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


def test_the_openapi_contract_names_operations_and_the_problem_shape() -> None:
    schema = create_app(settings()).openapi()
    session = schema["paths"]["/api/v1/auth/session"]

    assert {method: session[method]["operationId"] for method in ("post", "get", "delete")} == {
        "post": "openSession",
        "get": "readSession",
        "delete": "endSession",
    }
    assert "ProblemDocument" in schema["components"]["schemas"]
    error_code = schema["components"]["schemas"]["ProblemDocument"]["properties"]["error_code"]
    assert error_code["$ref"] == "#/components/schemas/ApiErrorCode"
    assert "SESSION_INVALID" in schema["components"]["schemas"]["ApiErrorCode"]["enum"]
    for method, status in (("post", "401"), ("post", "422"), ("get", "401"), ("delete", "403")):
        content = session[method]["responses"][status]["content"]
        assert set(content) == {PROBLEM_MEDIA_TYPE}
        problem = content[PROBLEM_MEDIA_TYPE]
        assert problem["schema"]["$ref"] == "#/components/schemas/ProblemDocument"


def test_every_documented_422_response_uses_only_the_problem_document() -> None:
    app = create_app(settings())

    def route_without_explicit_responses(value: int) -> dict[str, int]:
        return {"value": value}

    # FastAPI adds its default HTTPValidationError response when a route omits `responses`.
    app.add_api_route(
        "/api/v1/openapi-contract-probe",
        route_without_explicit_responses,
        methods=["GET"],
    )
    schema = app.openapi()

    documented_422 = [
        (method, path, operation["responses"]["422"])
        for path, path_item in schema["paths"].items()
        for method, operation in path_item.items()
        if method in {"get", "post", "put", "patch", "delete"}
        and "422" in operation.get("responses", {})
    ]

    assert documented_422
    for method, path, response in documented_422:
        assert set(response["content"]) == {PROBLEM_MEDIA_TYPE}, (method, path)
        assert response["content"][PROBLEM_MEDIA_TYPE]["schema"] == {
            "$ref": "#/components/schemas/ProblemDocument"
        }, (method, path)
    assert "HTTPValidationError" not in schema["components"]["schemas"]


def test_the_settings_object_is_reachable_from_the_application() -> None:
    # The composition root holds the resolved settings, so an adapter reads them from the
    # application rather than from the process environment.
    configured = settings()

    assert create_app(configured).state.settings == configured


def test_the_session_policy_is_built_from_the_configured_minutes() -> None:
    # `Settings` sits below the domain in the layering, so it carries the configured numbers
    # and the composition root builds the domain type from them.
    app = create_app(settings())

    assert app.state.session_policy == SessionPolicy(
        idle_timeout=timedelta(minutes=720),
        absolute_lifetime=timedelta(minutes=43200),
    )
