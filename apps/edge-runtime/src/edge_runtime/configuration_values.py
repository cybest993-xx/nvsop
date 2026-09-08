"""边缘运行配置共用的 JSON 值校验。"""

from __future__ import annotations

import math
from typing import cast
from urllib.parse import urlsplit


def safe_url(value: object, name: str, *, schemes: set[str]) -> str:
    """校验 URL, 不允许凭据、查询串或片段进入运行配置。"""
    raw = _non_empty_string(value, name)
    try:
        parsed = urlsplit(raw)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        raise ValueError(f"{name} is invalid") from None
    if parsed.scheme.lower() not in schemes or hostname is None:
        raise ValueError(f"{name} must use a supported host URL")
    if port is not None and port <= 0:
        raise ValueError(f"{name} must use a supported host URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(f"{name} must not contain URL credentials")
    if "?" in raw or "#" in raw:
        raise ValueError(f"{name} must not contain a query or fragment")
    return raw


def _object(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"{name} must be a JSON object")
    return cast(dict[str, object], value)


def _array(value: object, name: str) -> list[object]:
    if not isinstance(value, list):
        raise ValueError(f"{name} must be a JSON array")
    return cast(list[object], value)


def _require_keys(
    value: dict[str, object],
    *,
    required: set[str] | frozenset[str],
    optional: set[str] | None = None,
) -> None:
    allowed = set(required) | (optional or set())
    missing = set(required) - set(value)
    unknown = set(value) - allowed
    if missing:
        raise ValueError(f"configuration is missing {sorted(missing)}")
    if unknown:
        raise ValueError(f"configuration has unsupported fields {sorted(unknown)}")


def _non_empty_string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _positive_number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        raise ValueError(f"{name} must be a positive number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} must be a finite number")
    return result


def _non_negative_number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
        raise ValueError(f"{name} must be a non-negative number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} must be a finite number")
    return result


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


__all__ = ["safe_url"]
