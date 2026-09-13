from typing import Any

from factory_sop.problem import problem_openapi_response

ProblemResponses = dict[int | str, dict[str, Any]]

_UNAUTHORIZED: ProblemResponses = {
    401: problem_openapi_response("Authentication required or session invalid"),
    403: problem_openapi_response("Permission denied or CSRF token invalid"),
}
_NOT_FOUND_RESPONSES: ProblemResponses = {
    404: problem_openapi_response("Device record not found"),
}
_CONFLICT_RESPONSES: ProblemResponses = {
    409: problem_openapi_response("Device configuration conflict"),
}
_ITEM_RESPONSES = _NOT_FOUND_RESPONSES | _CONFLICT_RESPONSES
