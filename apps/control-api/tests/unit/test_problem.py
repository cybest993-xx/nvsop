from __future__ import annotations

import json

from factory_sop.problem import PROBLEM_MEDIA_TYPE, FieldError, problem_response


def body(
    status: int = 400,
    *,
    detail: str | None = None,
    field_errors: list[FieldError] | None = None,
) -> dict[str, object]:
    """The decoded document, so each test asserts on members rather than on a response object.

    The optional members are named explicitly rather than passed through as `**kwargs`: the
    signature is then the same one under test, and a member renamed on `problem_response`
    fails the type check here instead of silently arriving as an extra key.
    """
    response = problem_response(
        status=status,
        title="标题",
        error_code="SOMETHING_REFUSED",
        detail=detail,
        field_errors=field_errors,
    )
    decoded: dict[str, object] = json.loads(bytes(response.body))
    return decoded


def test_the_response_is_served_as_problem_json() -> None:
    # RFC 9457's media type, not `application/json`. A client that branches on the content
    # type has to be able to tell a problem from a successful payload.
    response = problem_response(status=403, title="没有权限", error_code="PERMISSION_DENIED")

    assert response.media_type == PROBLEM_MEDIA_TYPE
    assert response.status_code == 403


def test_the_body_carries_the_stable_error_code_and_the_http_status() -> None:
    # §5.15: `error_code` is the stable SCREAMING_SNAKE enumeration a client branches on, and
    # it is a different type from `reason_code`. The status is in the body as well as the
    # response line, which RFC 9457 requires so a proxy rewriting one is detectable.
    assert body(status=409) == {
        "type": "about:blank",
        "title": "标题",
        "status": 409,
        "error_code": "SOMETHING_REFUSED",
    }


def test_a_detail_is_included_when_there_is_one_to_give() -> None:
    assert body(detail="模板版本已被绑定")["detail"] == "模板版本已被绑定"


def test_field_errors_name_the_field_and_say_what_is_wrong_with_it() -> None:
    # The Web form puts each message next to its own input, which needs the field name as
    # data rather than inside a sentence.
    reported = body(
        field_errors=[
            FieldError(field="login_name", message="必填"),
            FieldError(field="password", message="太短"),
        ]
    )

    assert reported["field_errors"] == [
        {"field": "login_name", "message": "必填"},
        {"field": "password", "message": "太短"},
    ]


def test_absent_members_are_left_out_rather_than_sent_as_null() -> None:
    # RFC 9457 members are optional. A `null` would make a client distinguish "no detail"
    # from "detail is null", which is a difference with no meaning.
    assert "detail" not in body()
    assert "field_errors" not in body()
