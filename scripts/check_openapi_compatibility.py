"""Reject the control plane's recorded breaking OpenAPI changes.

ADR-0003 has no `/api/v2` escape hatch. This checker therefore protects the compatibility
surface the decision names: paths, operations, requests, responses and parameters cannot lose
accepted wire shapes, and inline or component schemas cannot be narrowed.

ADR-0003 同时允许“所有受影响客户端协同发布”的显式破坏性变更；声明文件就是它的机械落点：
只有逐条登记（含原因与 issue）的破坏项才被接受，未登记的破坏项仍然失败。
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path

HTTP_METHODS = frozenset({"get", "put", "post", "delete", "options", "head", "patch", "trace"})
JsonObject = Mapping[str, object]
ParameterKey = tuple[str, str]


def compatibility_errors(previous: JsonObject, current: JsonObject) -> list[str]:
    """Return deterministic descriptions of breaking changes from ``previous`` to ``current``."""
    errors: list[str] = []
    _check_paths(_objects(previous.get("paths")), _objects(current.get("paths")), errors)

    previous_components = _object(previous.get("components"))
    current_components = _object(current.get("components"))
    previous_schemas = _objects(previous_components.get("schemas"))
    current_schemas = _objects(current_components.get("schemas"))
    for name in sorted(previous_schemas):
        old_schema = previous_schemas[name]
        new_schema = current_schemas.get(name)
        if new_schema is None:
            errors.append(f"schema removed: {name}")
            continue
        _check_schema(old_schema, new_schema, f"schema {name}", errors)
    return errors


def _check_paths(
    previous: Mapping[str, JsonObject],
    current: Mapping[str, JsonObject],
    errors: list[str],
) -> None:
    for path in sorted(previous):
        old_item = previous[path]
        new_item = current.get(path)
        if new_item is None:
            errors.append(f"path removed: {path}")
            continue
        for method in sorted(HTTP_METHODS & set(old_item)):
            old_operation = _object(old_item.get(method))
            new_operation = new_item.get(method)
            if not isinstance(new_operation, Mapping):
                errors.append(f"operation removed: {method.upper()} {path}")
                continue
            _check_operation(
                path,
                method,
                old_item,
                new_item,
                old_operation,
                new_operation,
                errors,
            )


def _check_operation(
    path: str,
    method: str,
    old_path_item: JsonObject,
    new_path_item: JsonObject,
    old_operation: JsonObject,
    new_operation: JsonObject,
    errors: list[str],
) -> None:
    operation_location = f"{method.upper()} {path}"
    old_operation_id = old_operation.get("operationId")
    if isinstance(old_operation_id, str) and old_operation_id != new_operation.get("operationId"):
        errors.append(
            f"operationId changed: {operation_location}: "
            f"{old_operation_id} -> {new_operation.get('operationId')}"
        )

    _check_parameters(
        operation_location,
        _effective_parameters(old_path_item, old_operation),
        _effective_parameters(new_path_item, new_operation),
        errors,
    )
    _check_request_body(
        operation_location,
        old_operation.get("requestBody"),
        new_operation.get("requestBody"),
        errors,
    )
    _check_responses(
        operation_location,
        _objects(old_operation.get("responses")),
        _objects(new_operation.get("responses")),
        errors,
    )


def _effective_parameters(
    path_item: JsonObject, operation: JsonObject
) -> dict[ParameterKey, JsonObject]:
    """Merge path- and operation-level parameters using OpenAPI's override rule."""
    result: dict[ParameterKey, JsonObject] = {}
    for parameter in [
        *_objects_list(path_item.get("parameters")),
        *_objects_list(operation.get("parameters")),
    ]:
        key = _parameter_key(parameter)
        if key is not None:
            result[key] = parameter
    return result


def _parameter_key(parameter: JsonObject) -> ParameterKey | None:
    reference = parameter.get("$ref")
    if isinstance(reference, str):
        return ("$ref", reference)
    name = parameter.get("name")
    location = parameter.get("in")
    if isinstance(name, str) and isinstance(location, str):
        return (location, name)
    return None


def _check_parameters(
    operation_location: str,
    previous: Mapping[ParameterKey, JsonObject],
    current: Mapping[ParameterKey, JsonObject],
    errors: list[str],
) -> None:
    for key in sorted(previous):
        old_parameter = previous[key]
        new_parameter = current.get(key)
        display_location = _parameter_location(operation_location, old_parameter, key)
        schema_location = f"parameter {display_location}"
        if new_parameter is None:
            errors.append(f"parameter removed: {display_location}")
            continue
        if not _required(old_parameter) and _required(new_parameter):
            errors.append(f"parameter became required: {display_location}")
        _check_parameter_schema(
            old_parameter,
            new_parameter,
            schema_location,
            display_location,
            errors,
        )

    for key in sorted(set(current) - set(previous)):
        parameter = current[key]
        if _required(parameter):
            location = _parameter_location(operation_location, parameter, key)
            errors.append(f"required parameter added: {location}")


def _parameter_location(operation_location: str, parameter: JsonObject, key: ParameterKey) -> str:
    location, name = key
    if location == "$ref":
        return f"{operation_location} {name}"
    return f"{operation_location} {location}.{name}"


def _required(parameter: JsonObject) -> bool:
    # OpenAPI path parameters are required by definition, even if a malformed document says
    # otherwise. Treating them as required prevents a new path placeholder from slipping past
    # the checker.
    return parameter.get("in") == "path" or parameter.get("required") is True


def _check_parameter_schema(
    old_parameter: JsonObject,
    new_parameter: JsonObject,
    schema_location: str,
    display_location: str,
    errors: list[str],
) -> None:
    old_content = _objects(old_parameter.get("content"))
    new_content = _objects(new_parameter.get("content"))
    if old_content:
        _check_content(
            display_location,
            old_content,
            new_content,
            "parameter media type removed",
            errors,
            schema_location=schema_location,
        )
        return

    if "schema" in old_parameter or "schema" in new_parameter:
        _check_schema(
            old_parameter.get("schema", {}),
            new_parameter.get("schema", {}),
            schema_location,
            errors,
        )


def _check_request_body(
    operation_location: str,
    old_body: object,
    new_body: object,
    errors: list[str],
) -> None:
    location = f"request {operation_location}"
    old = _object_or_none(old_body)
    new = _object_or_none(new_body)
    if old is not None and new is None:
        errors.append(f"request body removed: {operation_location}")
        return
    if old is None:
        if new is not None and new.get("required") is True:
            errors.append(f"request body became required: {operation_location}")
        return

    if old.get("required") is not True and new.get("required") is True:
        errors.append(f"request body became required: {operation_location}")
    _check_content(
        operation_location,
        _objects(old.get("content")),
        _objects(new.get("content")),
        "request media type removed",
        errors,
        schema_location=location,
    )


def _check_responses(
    operation_location: str,
    previous: Mapping[str, JsonObject],
    current: Mapping[str, JsonObject],
    errors: list[str],
) -> None:
    for status in sorted(previous):
        old_response = previous[status]
        new_response = current.get(status)
        location = f"response {operation_location} {status}"
        if new_response is None:
            errors.append(f"response removed: {operation_location} {status}")
            continue
        _check_content(
            f"{operation_location} {status}",
            _objects(old_response.get("content")),
            _objects(new_response.get("content")),
            "response media type removed",
            errors,
            schema_location=location,
        )
        _check_response_headers(location, old_response, new_response, errors)


def _check_response_headers(
    response_location: str,
    old_response: JsonObject,
    new_response: JsonObject,
    errors: list[str],
) -> None:
    old_headers = _objects(old_response.get("headers"))
    new_headers = _objects(new_response.get("headers"))
    for name in sorted(old_headers):
        old_header = old_headers[name]
        new_header = new_headers.get(name)
        display_location = f"{response_location.removeprefix('response ')} header.{name}"
        schema_location = f"{response_location} header.{name}"
        if new_header is None:
            errors.append(f"response header removed: {display_location}")
            continue
        _check_parameter_schema(
            old_header,
            new_header,
            schema_location,
            display_location,
            errors,
        )


def _check_content(
    location: str,
    previous: Mapping[str, JsonObject],
    current: Mapping[str, JsonObject],
    removed_message: str,
    errors: list[str],
    *,
    schema_location: str | None = None,
) -> None:
    for media_type in sorted(previous):
        old_media = previous[media_type]
        new_media = current.get(media_type)
        if new_media is None:
            errors.append(f"{removed_message}: {location} {media_type}")
            continue
        if "schema" in old_media or "schema" in new_media:
            _check_schema(
                old_media.get("schema", {}),
                new_media.get("schema", {}),
                f"{schema_location or location} {media_type}",
                errors,
            )


def _check_schema(old: object, new: object, location: str, errors: list[str]) -> None:
    if not isinstance(old, Mapping) or not isinstance(new, Mapping):
        return

    old_reference = old.get("$ref")
    new_reference = new.get("$ref")
    if old_reference is not None and old_reference != new_reference:
        errors.append(
            f"schema reference narrowed at {location}: {old_reference} -> {new_reference}"
        )

    old_types = _types(old.get("type"))
    new_types = _types(new.get("type"))
    if old_types and new_types and not old_types <= new_types:
        errors.append(f"type narrowed at {location}: {sorted(old_types)} -> {sorted(new_types)}")
    elif not old_types and new_types:
        errors.append(f"type narrowed at {location}: unconstrained -> {sorted(new_types)}")

    old_enum = _enum_values(old.get("enum"))
    new_enum = _enum_values(new.get("enum"))
    if old_enum is not None and new_enum is not None and not old_enum <= new_enum:
        errors.append(f"enum values removed at {location}: {sorted(old_enum - new_enum)!r}")
    elif old_enum is None and new_enum is not None:
        errors.append(f"enum narrowed at {location}: unconstrained -> {sorted(new_enum)!r}")

    old_properties = _objects(old.get("properties"))
    new_properties = _objects(new.get("properties"))
    for field in sorted(old_properties):
        field_location = f"{location}.{field}"
        if field not in new_properties:
            errors.append(f"field removed: {field_location}")
            continue
        _check_schema(old_properties[field], new_properties[field], field_location, errors)

    old_required = _string_set(old.get("required"))
    new_required = _string_set(new.get("required"))
    for field in sorted(new_required - old_required):
        errors.append(f"required field added: {location}.{field}")

    old_items = old.get("items")
    new_items = new.get("items")
    if old_items is not None and new_items is not None:
        _check_schema(old_items, new_items, f"{location}[]", errors)

    old_additional = old.get("additionalProperties")
    new_additional = new.get("additionalProperties")
    if old_additional is True and new_additional is False:
        errors.append(f"additionalProperties narrowed at {location}: true -> false")

    for keyword in ("allOf", "anyOf", "oneOf"):
        old_members = _objects_list(old.get(keyword))
        new_members = _objects_list(new.get(keyword))
        for index, old_member in enumerate(old_members):
            if index >= len(new_members):
                errors.append(f"schema alternative removed: {location}.{keyword}[{index}]")
                continue
            _check_schema(
                old_member,
                new_members[index],
                f"{location}.{keyword}[{index}]",
                errors,
            )


def _types(value: object) -> set[str]:
    if isinstance(value, str):
        return {value}
    if isinstance(value, list):
        return {item for item in value if isinstance(item, str)}
    return set()


def _enum_values(value: object) -> set[object] | None:
    if not isinstance(value, list):
        return None
    try:
        return set(value)
    except TypeError:
        return None


def _string_set(value: object) -> set[str]:
    if not isinstance(value, list):
        return set()
    return {item for item in value if isinstance(item, str)}


def _object(value: object) -> JsonObject:
    return value if isinstance(value, Mapping) else {}


def _object_or_none(value: object) -> JsonObject | None:
    return value if isinstance(value, Mapping) else None


def _objects(value: object) -> dict[str, JsonObject]:
    if not isinstance(value, Mapping):
        return {}
    return {
        key: item
        for key, item in value.items()
        if isinstance(key, str) and isinstance(item, Mapping)
    }


def _objects_list(value: object) -> list[JsonObject]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def previous_contract(reference: str, path: Path) -> JsonObject | None:
    result = subprocess.run(
        ["git", "show", f"{reference}:{path.as_posix()}"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    return json.loads(result.stdout)


def load_declarations(path: Path | None) -> dict[str, Mapping[str, object]]:
    """读取已登记的破坏性契约变化；没有声明文件时视为没有声明。"""
    if path is None or not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    changes = payload.get("changes") if isinstance(payload, Mapping) else None
    if not isinstance(changes, list):
        raise SystemExit(f"{path} 必须包含 changes 列表")
    declared: dict[str, Mapping[str, object]] = {}
    for entry in changes:
        if not isinstance(entry, Mapping) or not isinstance(entry.get("change"), str):
            raise SystemExit(f"{path} 的每条变化都必须带 change 字段")
        reason = entry.get("reason")
        issue = entry.get("issue")
        if not isinstance(reason, str) or not reason.strip():
            raise SystemExit(f"{path} 的每条变化都必须带非空字符串 reason")
        if not isinstance(issue, str) or not issue.strip():
            raise SystemExit(f"{path} 的每条变化都必须带非空字符串 issue")
        declared[str(entry["change"])] = entry
    return declared


def undeclared_errors(errors: list[str], declared: Mapping[str, Mapping[str, object]]) -> list[str]:
    """返回未登记的破坏项；已登记的破坏项是显式协同发布的一部分。"""
    return [error for error in errors if error not in declared]


def main(
    argv: list[str],
    *,
    previous_loader: Callable[[str, Path], JsonObject | None] = previous_contract,
) -> int:
    """门禁入口；`previous_loader` 是给测试注入基线契约的窄 seam。"""
    if len(argv) not in (3, 4):
        raise SystemExit(
            "usage: check_openapi_compatibility.py BASE_REF CURRENT_OPENAPI "
            "[DECLARED_BREAKING_CHANGES]"
        )
    reference = argv[1]
    path = Path(argv[2])
    previous = previous_loader(reference, path)
    if previous is None:
        print(f"No OpenAPI contract at {reference}:{path}; accepting the initial contract.")
        return 0
    current = json.loads(path.read_text(encoding="utf-8"))
    errors = compatibility_errors(previous, current)
    declarations_path = Path(argv[3]) if len(argv) > 3 else None
    declared = load_declarations(declarations_path)
    acknowledged = [error for error in errors if error in declared]
    if acknowledged:
        print(f"Acknowledged breaking changes ({declarations_path}):")
        for error in acknowledged:
            entry = declared[error]
            print(
                f"- {error} "
                f"[{entry.get('issue', '?')} {entry.get('recorded', '?')}]: "
                f"{entry.get('reason', '')}"
            )
    undeclared = undeclared_errors(errors, declared)
    if undeclared:
        print("OpenAPI compatibility failed:", file=sys.stderr)
        for error in undeclared:
            print(f"- {error}", file=sys.stderr)
        return 1
    print("OpenAPI compatibility passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
