"""推理机生产配置的读取、校验和秘密文件边界。"""

from __future__ import annotations

import json
import ssl
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from nvsop_contracts import (
    Capability,
    capability_from_wire,
    validate_host_identity_private_key,
)

from edge_runtime.configuration_values import (
    _array,
    _non_empty_string,
    _object,
    _positive_number,
    _require_keys,
    safe_url,
)
from edge_runtime.connectors.hikvision import IsapiProfile
from edge_runtime.connectors.port import PointState
from edge_runtime.media import MediaRuntimeConfiguration, load_media_runtime_configuration
from edge_runtime.station_runtime import (
    StationRuntimeConfiguration,
    station_configuration,
)

_SUPPORTED_CONNECTOR_TYPES = frozenset({"hikvision_isapi"})


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
    connector_type: str = "hikvision_isapi"

    def __post_init__(self) -> None:
        if self.connector_type not in _SUPPORTED_CONNECTOR_TYPES:
            raise ValueError(f"connector_type is unsupported: {self.connector_type}")
        safe_url(self.base_url, "connector base_url", schemes={"http", "https"})
        connector_configuration(self.base_url)


@dataclass(frozen=True, slots=True)
class EdgeRuntimeConfiguration:
    """已校验的推理机命令和工位运行配置。"""

    center_url: str
    host_id: str
    host_private_key: str
    command_timeout: float
    command_poll_interval: float
    connectors: tuple[LocalIsapiConnectorConfiguration, ...]
    ssl_context: ssl.SSLContext | None
    local_state_path: Path | None
    stations: tuple[StationRuntimeConfiguration, ...]
    media: MediaRuntimeConfiguration | None = None


_CONFIG_KEYS = frozenset(
    {
        "center_url",
        "host_id",
        "host_private_key_file",
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
        "connector_type",
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


def load_configuration(config_path: str | Path) -> EdgeRuntimeConfiguration:
    """读取本机委托命令配置。"""
    return _load_configuration(config_path, stations_required=False)


def load_autonomous_configuration(config_path: str | Path) -> EdgeRuntimeConfiguration:
    """读取包含自治工位和本地状态路径的完整配置。"""
    return _load_configuration(config_path, stations_required=True)


def _load_configuration(
    config_path: str | Path, *, stations_required: bool
) -> EdgeRuntimeConfiguration:
    raw: object = json.loads(Path(config_path).read_text(encoding="utf-8"))
    config = _object(raw, "edge runtime configuration")
    required = _CONFIG_KEYS | ({"local_state_path", "stations"} if stations_required else set())
    _require_keys(config, required=required, optional={"center_ca_file", "media"})
    connectors_value = _array(config["connectors"], "connectors")
    connectors = tuple(_local_connector(item) for item in connectors_value)
    stations: tuple[StationRuntimeConfiguration, ...] = ()
    local_state_path: Path | None = None
    if stations_required:
        station_values = _array(config["stations"], "stations")
        if not station_values:
            raise ValueError("stations must be a non-empty JSON array")
        stations = tuple(station_configuration(item) for item in station_values)
        station_keys = [(station.station_id, station.backend_id) for station in stations]
        if len(set(station_keys)) != len(station_keys):
            raise ValueError("stations must have unique station_id/backend_id pairs")
        local_state_path = _path(config["local_state_path"], "local_state_path")
    host_private_key = _read_secret(
        _path(config["host_private_key_file"], "host_private_key_file"),
        "host private key",
    )
    validate_host_identity_private_key(host_private_key)
    media = None if "media" not in config else load_media_runtime_configuration(config["media"])
    return EdgeRuntimeConfiguration(
        center_url=safe_url(config["center_url"], "center_url", schemes={"https"}),
        host_id=_non_empty_string(config["host_id"], "host_id"),
        host_private_key=host_private_key,
        command_timeout=_positive_number(
            config["command_timeout_seconds"], "command_timeout_seconds"
        ),
        command_poll_interval=_positive_number(
            config["command_poll_interval_seconds"], "command_poll_interval_seconds"
        ),
        connectors=connectors,
        ssl_context=_center_ssl_context(config.get("center_ca_file")),
        local_state_path=local_state_path,
        stations=stations,
        media=media,
    )


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
        optional={"connector_type", "username_file", "password_file"},
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
    connector_type = _non_empty_string(
        config.get("connector_type", "hikvision_isapi"), "connector_type"
    )
    if connector_type not in _SUPPORTED_CONNECTOR_TYPES:
        raise ValueError(f"connector_type is unsupported: {connector_type}")
    return LocalIsapiConnectorConfiguration(
        connector_id=_non_empty_string(config["connector_id"], "connector_id"),
        revision=_positive_integer(config["revision"], "revision"),
        credentials_configured=credentials_configured,
        base_url=safe_url(config["base_url"], "connector base_url", schemes={"http", "https"}),
        username=username,
        password=password,
        profile=_profile(config["profile"]),
        capability=capability_from_wire(_object(config["capability"], "capability")),
        connector_type=connector_type,
    )


def _profile(value: object) -> IsapiProfile:
    config = _object(value, "ISAPI profile")
    _require_keys(config, required=_PROFILE_KEYS)
    input_tokens: list[tuple[str, PointState]] = []
    for item in _array(config["input_tokens"], "input tokens"):
        token = _object(item, "input token")
        _require_keys(token, required={"token", "state"})
        input_tokens.append(
            (
                _non_empty_string(token["token"], "input token"),
                _point_state(token["state"], "input state"),
            )
        )
    output_tokens: list[tuple[PointState, str]] = []
    for item in _array(config["output_tokens"], "output tokens"):
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


def connector_configuration(base_url: str) -> dict[str, str | int]:
    """从本地连接器 URL 生成不含凭据的中心配置快照。"""
    safe_url(base_url, "connector base_url", schemes={"http", "https"})
    parsed = urlsplit(base_url)
    if parsed.hostname is None or parsed.path not in {"", "/"}:
        raise ValueError("connector base_url must not contain a path")
    configuration: dict[str, str | int] = {"address": parsed.hostname}
    if parsed.port is not None:
        configuration["port"] = parsed.port
    return configuration


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


def _boolean(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a boolean")
    return value


def _positive_integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _point_state(value: object, name: str) -> PointState:
    try:
        return PointState(_non_empty_string(value, name))
    except ValueError as error:
        raise ValueError(f"{name} is unsupported") from error


__all__ = [
    "EdgeRuntimeConfiguration",
    "LocalIsapiConnectorConfiguration",
    "connector_configuration",
    "load_autonomous_configuration",
    "load_configuration",
    "safe_url",
]
