"""推理机边缘运行时的委托命令组成根。"""

from __future__ import annotations

import json
import os
import signal
import ssl
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from time import sleep
from types import FrameType
from typing import cast
from urllib.parse import urlsplit

from nvsop_contracts import Capability, ConnectionTestOutcome, capability_from_wire

from edge_runtime.connectors.hikvision import IsapiConnector, IsapiProfile
from edge_runtime.connectors.port import PointState, Reachability
from edge_runtime.connectors.transport import UrllibIsapiTransport
from edge_runtime.supervisor.delegated_commands import (
    ConnectionTestCommandRunner,
    ConnectionTestExecutor,
    LocalConnector,
    LocalConnectorRegistry,
    LocalProbeResult,
)
from edge_runtime.supervisor.delegated_transport import CommandTransportError, HttpCommandTransport


@dataclass(frozen=True, slots=True)
class LocalIsapiConnectorConfiguration:
    """推理机本地已确认的海康连接器配置; 凭据只在本机内存中流转。"""

    connector_id: str
    revision: int
    credentials_configured: bool
    base_url: str
    username: str
    password: str
    profile: IsapiProfile
    capability: Capability


class _IsapiConnectionTestProbe:
    """把连接器包的健康结果转换为 supervisor 的命令探测结果。"""

    def __init__(self, connector: IsapiConnector) -> None:
        self._connector = connector

    def probe(self, /, *, timeout: float) -> LocalProbeResult:
        health = self._connector.probe(timeout=timeout)
        if health.reachability is Reachability.REACHABLE:
            return LocalProbeResult(
                outcome=ConnectionTestOutcome.REACHABLE,
                detail=health.detail or None,
            )
        if health.reachability is Reachability.UNREACHABLE:
            return LocalProbeResult(
                outcome=ConnectionTestOutcome.UNREACHABLE,
                detail=health.detail or None,
            )
        return LocalProbeResult(
            outcome=ConnectionTestOutcome.REJECTED,
            detail="推理机连接器返回了未验证状态",
            failure_code="COMMAND_RESULT_INVALID",
        )


class ConfiguredLocalConnectorRegistry(LocalConnectorRegistry):
    """把本机配置装配成真实的 ISAPI 连接器注册表。"""

    def __init__(self, configurations: tuple[LocalIsapiConnectorConfiguration, ...]) -> None:
        connectors: dict[str, LocalConnector] = {}
        for configuration in configurations:
            if configuration.connector_id in connectors:
                raise ValueError(f"duplicate local connector {configuration.connector_id}")
            probe = IsapiConnector(
                transport=UrllibIsapiTransport(
                    base_url=_safe_url(
                        configuration.base_url,
                        "connector base_url",
                        schemes={"http", "https"},
                    ),
                    username=configuration.username,
                    password=configuration.password,
                ),
                profile=configuration.profile,
                capability=configuration.capability,
            )
            connectors[configuration.connector_id] = LocalConnector(
                revision=configuration.revision,
                credentials_configured=configuration.credentials_configured,
                probe=_IsapiConnectionTestProbe(probe),
            )
        self._connectors = connectors

    def resolve(self, connector_id: str) -> LocalConnector | None:
        """按中心命令的目标 ID 返回本机真实连接器。"""
        return self._connectors.get(connector_id)


class ConnectionTestCommandLoop:
    """持续驱动领取、真实本地探测和回报; 中心暂时不可达时安全重试。"""

    def __init__(
        self,
        *,
        runner: ConnectionTestCommandRunner,
        poll_interval: float,
        sleep_fn: Callable[[float], None] = sleep,
    ) -> None:
        if poll_interval <= 0:
            raise ValueError("command poll interval must be positive")
        self._runner = runner
        self._poll_interval = poll_interval
        self._sleep = sleep_fn

    def run_once(self) -> bool:
        """运行一轮真实命令处理, 供进程循环和系统接缝共同使用。"""
        return self._runner.run_once()

    def run_forever(self, *, should_stop: Callable[[], bool]) -> None:
        """持续运行, 传输失败只影响本轮并交给下一轮重新领取。"""
        while not should_stop():
            with suppress(CommandTransportError):
                self.run_once()
            self._sleep(self._poll_interval)


def build_connection_test_runner(
    *,
    center_url: str,
    host_id: str,
    host_token: str,
    command_timeout: float,
    local_connectors: tuple[LocalIsapiConnectorConfiguration, ...],
    ssl_context: ssl.SSLContext | None = None,
) -> ConnectionTestCommandRunner:
    """装配生产委托命令执行器及本机真实连接器适配器。"""
    return ConnectionTestCommandRunner(
        transport=HttpCommandTransport(
            center_url=center_url,
            host_id=host_id,
            host_token=host_token,
            timeout=command_timeout,
            ssl_context=ssl_context,
        ),
        executor=ConnectionTestExecutor(
            registry=ConfiguredLocalConnectorRegistry(local_connectors),
            timeout=command_timeout,
        ),
    )


def build_connection_test_loop(
    *,
    center_url: str,
    host_id: str,
    host_token: str,
    command_timeout: float,
    command_poll_interval: float,
    local_connectors: tuple[LocalIsapiConnectorConfiguration, ...],
    ssl_context: ssl.SSLContext | None = None,
    sleep_fn: Callable[[float], None] = sleep,
) -> ConnectionTestCommandLoop:
    """装配生产委托命令循环及本机真实连接器适配器。"""
    return ConnectionTestCommandLoop(
        runner=build_connection_test_runner(
            center_url=center_url,
            host_id=host_id,
            host_token=host_token,
            command_timeout=command_timeout,
            local_connectors=local_connectors,
            ssl_context=ssl_context,
        ),
        poll_interval=command_poll_interval,
        sleep_fn=sleep_fn,
    )


_CONFIG_KEYS = frozenset(
    {
        "center_url",
        "host_id",
        "host_token_file",
        "command_timeout_seconds",
        "command_poll_interval_seconds",
        "connectors",
    }
)
_CONNECTOR_KEYS = frozenset(
    {
        "connector_id",
        "revision",
        "credentials_configured",
        "base_url",
        "username_file",
        "password_file",
        "profile",
        "capability",
    }
)
_PROFILE_KEYS = frozenset(
    {
        "input_status_path",
        "output_trigger_path",
        "output_body",
        "device_info_path",
        "input_state_element",
        "input_tokens",
        "output_tokens",
    }
)


def build_connection_test_loop_from_file(config_path: str | Path) -> ConnectionTestCommandLoop:
    """从推理机本地配置文件装配生产命令循环。"""
    path = Path(config_path)
    raw: object = json.loads(path.read_text(encoding="utf-8"))
    config = _object(raw, "command loop configuration")
    _require_keys(config, required=_CONFIG_KEYS, optional={"center_ca_file"})

    center_url = _safe_url(config["center_url"], "center_url", schemes={"https"})
    ssl_context = _center_ssl_context(config.get("center_ca_file"))
    host_token = _read_secret(_path(config["host_token_file"], "host_token_file"), "host token")
    connectors_value = config["connectors"]
    if not isinstance(connectors_value, list):
        raise ValueError("connectors must be a JSON array")

    connectors = tuple(_local_connector(item) for item in connectors_value)
    return build_connection_test_loop(
        center_url=center_url,
        host_id=_non_empty_string(config["host_id"], "host_id"),
        host_token=host_token,
        command_timeout=_positive_number(
            config["command_timeout_seconds"], "command_timeout_seconds"
        ),
        command_poll_interval=_positive_number(
            config["command_poll_interval_seconds"], "command_poll_interval_seconds"
        ),
        local_connectors=connectors,
        ssl_context=ssl_context,
    )


def main() -> int:
    """启动推理机的委托命令循环进程。"""
    config_path = os.environ.get("NVSOP_EDGE_COMMAND_CONFIG_FILE")
    if not config_path:
        raise SystemExit("NVSOP_EDGE_COMMAND_CONFIG_FILE is required")

    try:
        loop = build_connection_test_loop_from_file(config_path)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise SystemExit(f"edge command loop configuration is invalid: {error}") from None

    stopping = False

    def request_stop(_signum: int, _frame: FrameType | None) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    loop.run_forever(should_stop=lambda: stopping)
    return 0


def _local_connector(value: object) -> LocalIsapiConnectorConfiguration:
    config = _object(value, "local connector configuration")
    _require_keys(
        config,
        required={
            "connector_id",
            "revision",
            "credentials_configured",
            "base_url",
            "profile",
            "capability",
        },
        optional={"username_file", "password_file"},
    )
    credentials_configured = _boolean(config["credentials_configured"], "credentials_configured")
    if credentials_configured:
        username = _read_secret(
            _path(config["username_file"], "username_file"), "connector username"
        )
        password = _read_secret(
            _path(config["password_file"], "password_file"), "connector password"
        )
    else:
        username = ""
        password = ""
    return LocalIsapiConnectorConfiguration(
        connector_id=_non_empty_string(config["connector_id"], "connector_id"),
        revision=_positive_integer(config["revision"], "revision"),
        credentials_configured=credentials_configured,
        base_url=_safe_url(config["base_url"], "connector base_url", schemes={"http", "https"}),
        username=username,
        password=password,
        profile=_profile(config["profile"]),
        capability=capability_from_wire(_object(config["capability"], "capability")),
    )


def _profile(value: object) -> IsapiProfile:
    config = _object(value, "ISAPI profile")
    _require_keys(config, required=_PROFILE_KEYS)
    input_tokens: list[tuple[str, PointState]] = []
    for item in _array(config["input_tokens"], "input_tokens"):
        token = _object(item, "input token")
        _require_keys(token, required={"token", "state"})
        input_tokens.append(
            (
                _non_empty_string(token["token"], "input token"),
                _point_state(token["state"], "input state"),
            )
        )
    output_tokens: list[tuple[PointState, str]] = []
    for item in _array(config["output_tokens"], "output_tokens"):
        token = _object(item, "output token")
        _require_keys(token, required={"state", "token"})
        output_tokens.append(
            (
                _point_state(token["state"], "output state"),
                _non_empty_string(token["token"], "output token"),
            )
        )
    return IsapiProfile(
        input_status_path=_non_empty_string(config["input_status_path"], "input_status_path"),
        output_trigger_path=_non_empty_string(config["output_trigger_path"], "output_trigger_path"),
        output_body=_non_empty_string(config["output_body"], "output_body"),
        device_info_path=_non_empty_string(config["device_info_path"], "device_info_path"),
        input_state_element=_non_empty_string(config["input_state_element"], "input_state_element"),
        input_tokens=tuple(input_tokens),
        output_tokens=tuple(output_tokens),
    )


def _safe_url(value: object, name: str, *, schemes: set[str]) -> str:
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


def _center_ssl_context(value: object) -> ssl.SSLContext | None:
    if value is None:
        return None
    return ssl.create_default_context(cafile=str(_path(value, "center_ca_file")))


def _read_secret(path: Path, name: str) -> str:
    value = path.read_text(encoding="utf-8").strip()
    if not value:
        raise ValueError(f"{name} file must not be empty")
    return value


def _path(value: object, name: str) -> Path:
    return Path(_non_empty_string(value, name))


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


def _boolean(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a boolean")
    return value


def _positive_integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _positive_number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        raise ValueError(f"{name} must be a positive number")
    return float(value)


def _point_state(value: object, name: str) -> PointState:
    try:
        return PointState(_non_empty_string(value, name))
    except ValueError as error:
        raise ValueError(f"{name} is unsupported") from error


__all__ = [
    "ConfiguredLocalConnectorRegistry",
    "ConnectionTestCommandLoop",
    "LocalIsapiConnectorConfiguration",
    "build_connection_test_loop",
    "build_connection_test_loop_from_file",
    "build_connection_test_runner",
    "main",
]
