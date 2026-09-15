"""严格的中心到推理机配置拉取契约。

中心在越过此边界前解析运行参数。edge 只接收一组完整的生效值，不接收需要自行解释的默认值和覆盖组。
本模块的连接器 wire 类型故意无法表示凭据。
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import cast

from nvsop_contracts.capability import Capability, capability_from_wire, capability_to_wire

LEGACY_CONFIGURATION_CONTRACT_VERSION = 1
CONFIGURATION_CONTRACT_VERSION = 2


@dataclass(frozen=True, slots=True)
class ResolvedRuntimeParameters:
    """一组完整解析的工位运行参数。"""

    idle_timeout_seconds: float
    step_deadline_seconds: float
    disposition_policy: str

    def __post_init__(self) -> None:
        for name, value in (
            ("idle_timeout_seconds", self.idle_timeout_seconds),
            ("step_deadline_seconds", self.step_deadline_seconds),
        ):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{name} must be numeric")
            if not math.isfinite(float(value)) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        if not isinstance(self.disposition_policy, str) or not self.disposition_policy.strip():
            raise ValueError("disposition_policy must not be empty")

    def to_wire(self) -> dict[str, object]:
        return {
            "idle_timeout_seconds": float(self.idle_timeout_seconds),
            "step_deadline_seconds": float(self.step_deadline_seconds),
            "disposition_policy": self.disposition_policy,
        }

    @classmethod
    def from_wire(cls, value: Mapping[str, object]) -> ResolvedRuntimeParameters:
        _require_keys(
            value,
            {"idle_timeout_seconds", "step_deadline_seconds", "disposition_policy"},
            "runtime parameters",
        )
        idle = _number(value["idle_timeout_seconds"], "idle_timeout_seconds")
        deadline = _number(value["step_deadline_seconds"], "step_deadline_seconds")
        policy = _string(value["disposition_policy"], "disposition_policy")
        return cls(idle, deadline, policy)


@dataclass(frozen=True, slots=True)
class ConfigurationArtifact:
    """字节可独立校验的模板制品。"""

    name: str
    media_type: str
    content: bytes
    sha256: str

    def __post_init__(self) -> None:
        if not self.name or not self.media_type:
            raise ValueError("configuration artifacts need a name and media type")
        if hashlib.sha256(self.content).hexdigest() != self.sha256:
            raise ValueError("configuration artifact digest does not match its content")
        _validate_digest(self.sha256, "artifact sha256")

    def to_wire(self) -> dict[str, object]:
        return {
            "name": self.name,
            "media_type": self.media_type,
            "content_base64": base64.b64encode(self.content).decode("ascii"),
            "sha256": self.sha256,
        }

    @classmethod
    def from_wire(cls, value: Mapping[str, object]) -> ConfigurationArtifact:
        _require_keys(value, {"name", "media_type", "content_base64", "sha256"}, "artifact")
        encoded = _string(value["content_base64"], "artifact content_base64")
        try:
            content = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError) as error:
            raise ValueError("artifact content_base64 is invalid") from error
        return cls(
            name=_string(value["name"], "artifact name"),
            media_type=_string(value["media_type"], "artifact media_type"),
            content=content,
            sha256=_string(value["sha256"], "artifact sha256"),
        )


@dataclass(frozen=True, slots=True)
class ConfigurationTemplate:
    """不可变的已发布模板版本及 edge 所需的全部字节。"""

    version_id: str
    version_sha256: str
    artifacts: tuple[ConfigurationArtifact, ...]

    def __post_init__(self) -> None:
        if not self.version_id:
            raise ValueError("template version_id must not be empty")
        _validate_digest(self.version_sha256, "template version_sha256")
        if not self.artifacts:
            raise ValueError("a template must carry at least one artifact")
        names = tuple(artifact.name for artifact in self.artifacts)
        if len(set(names)) != len(names):
            raise ValueError("template artifact names must be unique")
        manifest = next(
            (artifact for artifact in self.artifacts if artifact.name == "manifest.json"),
            None,
        )
        if manifest is not None:
            if manifest.sha256 != self.version_sha256:
                raise ValueError("template version sha256 does not match manifest")
            try:
                manifest_document = json.loads(manifest.content.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise ValueError("template manifest is not valid JSON") from error
            expected_artifacts = [
                {
                    "byte_length": len(artifact.content),
                    "media_type": artifact.media_type,
                    "name": artifact.name,
                    "sha256": artifact.sha256,
                }
                for artifact in self.artifacts
                if artifact.name != "manifest.json"
            ]
            if manifest_document != {
                "artifacts": expected_artifacts,
                "format_version": 1,
            }:
                raise ValueError("template manifest does not match its artifacts")

    def to_wire(self) -> dict[str, object]:
        return {
            "version_id": self.version_id,
            "version_sha256": self.version_sha256,
            "artifacts": [artifact.to_wire() for artifact in self.artifacts],
        }

    @classmethod
    def from_wire(cls, value: Mapping[str, object]) -> ConfigurationTemplate:
        _require_keys(value, {"version_id", "version_sha256", "artifacts"}, "template")
        raw_artifacts = value["artifacts"]
        if not isinstance(raw_artifacts, Sequence) or isinstance(raw_artifacts, str):
            raise ValueError("template artifacts must be an array")
        return cls(
            version_id=_string(value["version_id"], "template version_id"),
            version_sha256=_string(value["version_sha256"], "template version_sha256"),
            artifacts=tuple(
                ConfigurationArtifact.from_wire(_object(item, "template artifact"))
                for item in raw_artifacts
            ),
        )


@dataclass(frozen=True, slots=True)
class ConfiguredConnector:
    """属于主机且不带凭据字段的连接器。"""

    connector_id: str
    name: str
    connector_type: str
    revision: int
    address: str
    port: int | None
    capability: Capability

    def __post_init__(self) -> None:
        if not self.connector_id or not self.name or not self.connector_type or not self.address:
            raise ValueError("connector identity and address must not be empty")
        if self.revision < 1:
            raise ValueError("connector revision must be positive")
        if self.port is not None and not 1 <= self.port <= 65535:
            raise ValueError("connector port must be between 1 and 65535")
        if _looks_like_secret(self.address):
            raise ValueError("connector address cannot carry credentials")

    def to_wire(self) -> dict[str, object]:
        return {
            "connector_id": self.connector_id,
            "name": self.name,
            "connector_type": self.connector_type,
            "revision": self.revision,
            "address": self.address,
            "port": self.port,
            "capability": capability_to_wire(self.capability),
        }

    @classmethod
    def from_wire(cls, value: Mapping[str, object]) -> ConfiguredConnector:
        _require_keys(
            value,
            {"connector_id", "name", "connector_type", "revision", "address", "port", "capability"},
            "connector",
        )
        raw_port = value["port"]
        if raw_port is not None and (isinstance(raw_port, bool) or not isinstance(raw_port, int)):
            raise ValueError("connector port is invalid")
        return cls(
            connector_id=_string(value["connector_id"], "connector_id"),
            name=_string(value["name"], "connector name"),
            connector_type=_string(value["connector_type"], "connector_type"),
            revision=_positive_int(value["revision"], "connector revision"),
            address=_string(value["address"], "connector address"),
            port=raw_port,
            capability=capability_from_wire(_object(value["capability"], "connector capability")),
        )


@dataclass(frozen=True, slots=True)
class ConfiguredPoint:
    """主机裁剪拓扑中的一个点位。"""

    point_id: str
    name: str
    direction: str
    connector_id: str
    role: str
    address: str

    def __post_init__(self) -> None:
        if any(
            not value
            for value in (self.point_id, self.name, self.direction, self.connector_id, self.role)
        ):
            raise ValueError("point fields must not be empty")
        if not self.address:
            raise ValueError("point address must not be empty")

    def to_wire(self) -> dict[str, object]:
        return {
            "point_id": self.point_id,
            "name": self.name,
            "direction": self.direction,
            "connector_id": self.connector_id,
            "role": self.role,
            "address": self.address,
        }

    @classmethod
    def from_wire(cls, value: Mapping[str, object]) -> ConfiguredPoint:
        _require_keys(
            value,
            {"point_id", "name", "direction", "connector_id", "role", "address"},
            "point",
        )
        address = _string(value["address"], "point address")
        return cls(
            point_id=_string(value["point_id"], "point_id"),
            name=_string(value["name"], "point name"),
            direction=_string(value["direction"], "point direction"),
            connector_id=_string(value["connector_id"], "point connector_id"),
            role=_string(value["role"], "point role"),
            address=address,
        )


@dataclass(frozen=True, slots=True)
class ConfiguredStation:
    """一台推理机可见的一个工位/backend 切片。"""

    station_id: str
    backend_id: str
    code: str
    name: str
    revision: int
    runtime_parameters: ResolvedRuntimeParameters
    connectors: tuple[ConfiguredConnector, ...]
    points: tuple[ConfiguredPoint, ...]
    template: ConfigurationTemplate | None
    model_ids: tuple[str, ...] = ()
    _model_ids_present: bool = field(default=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not self.station_id or not self.backend_id or not self.code or not self.name:
            raise ValueError("station identity must not be empty")
        if self.revision < 1:
            raise ValueError("station revision must be positive")
        connector_ids = {connector.connector_id for connector in self.connectors}
        if len(connector_ids) != len(self.connectors):
            raise ValueError("station connector ids must be unique")
        point_ids = {point.point_id for point in self.points}
        if len(point_ids) != len(self.points):
            raise ValueError("station point ids must be unique")
        if any(point.connector_id not in connector_ids for point in self.points):
            raise ValueError("a point must refer to a connector in the same station")
        if any(not model_id for model_id in self.model_ids):
            raise ValueError("station model ids must not be empty")

    def to_wire(self) -> dict[str, object]:
        return {
            "station_id": self.station_id,
            "backend_id": self.backend_id,
            "code": self.code,
            "name": self.name,
            "revision": self.revision,
            "runtime_parameters": self.runtime_parameters.to_wire(),
            "connectors": [connector.to_wire() for connector in self.connectors],
            "points": [point.to_wire() for point in self.points],
            "template": None if self.template is None else self.template.to_wire(),
            "model_ids": list(self.model_ids),
        }

    @classmethod
    def from_wire(
        cls,
        value: Mapping[str, object],
        *,
        allow_missing_model_ids: bool = False,
    ) -> ConfiguredStation:
        _require_keys(
            value,
            {
                "station_id",
                "backend_id",
                "code",
                "name",
                "revision",
                "runtime_parameters",
                "connectors",
                "points",
                "template",
                "model_ids",
            }
            if not allow_missing_model_ids
            else {
                "station_id",
                "backend_id",
                "code",
                "name",
                "revision",
                "runtime_parameters",
                "connectors",
                "points",
                "template",
            },
            "station",
            optional={"model_ids"} if allow_missing_model_ids else set(),
        )
        raw_connectors = _array(value["connectors"], "station connectors")
        raw_points = _array(value["points"], "station points")
        raw_template = value["template"]
        if raw_template is not None and not isinstance(raw_template, Mapping):
            raise ValueError("station template is invalid")
        return cls(
            station_id=_string(value["station_id"], "station_id"),
            backend_id=_string(value["backend_id"], "backend_id"),
            code=_string(value["code"], "station code"),
            name=_string(value["name"], "station name"),
            revision=_positive_int(value["revision"], "station revision"),
            runtime_parameters=ResolvedRuntimeParameters.from_wire(
                _object(value["runtime_parameters"], "station runtime_parameters")
            ),
            connectors=tuple(
                ConfiguredConnector.from_wire(_object(item, "station connector"))
                for item in raw_connectors
            ),
            points=tuple(
                ConfiguredPoint.from_wire(_object(item, "station point")) for item in raw_points
            ),
            template=(
                None
                if raw_template is None
                else ConfigurationTemplate.from_wire(cast(Mapping[str, object], raw_template))
            ),
            model_ids=_strings(value.get("model_ids", ()), "station model_ids"),
            _model_ids_present="model_ids" in value,
        )


@dataclass(frozen=True, slots=True)
class ConfigurationBundle:
    """完整且按主机裁剪的配置确认信封。"""

    host_id: str
    config_revision: int
    generated_at: str
    stations: tuple[ConfiguredStation, ...]
    contract_version: int = CONFIGURATION_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.contract_version not in {
            LEGACY_CONFIGURATION_CONTRACT_VERSION,
            CONFIGURATION_CONTRACT_VERSION,
        }:
            raise ValueError("configuration contract version is unsupported")
        if not self.host_id or not self.generated_at:
            raise ValueError("configuration bundle identity must not be empty")
        if self.config_revision < 1:
            raise ValueError("configuration revision must be positive")
        station_keys = {(station.station_id, station.backend_id) for station in self.stations}
        if len(station_keys) != len(self.stations):
            raise ValueError("configuration station/backend slices must be unique")

    def content_wire(self) -> dict[str, object]:
        stations = [station.to_wire() for station in self.stations]
        if self.contract_version == LEGACY_CONFIGURATION_CONTRACT_VERSION:
            for wire_station, station in zip(stations, self.stations, strict=True):
                if not wire_station["model_ids"] and not station._model_ids_present:
                    wire_station.pop("model_ids")
        return {
            "contract_version": self.contract_version,
            "host_id": self.host_id,
            "config_revision": self.config_revision,
            "generated_at": self.generated_at,
            "stations": stations,
        }

    @property
    def sha256(self) -> str:
        """返回覆盖整个传输信封的摘要。"""
        return hashlib.sha256(_canonical_json(self.content_wire()).encode("utf-8")).hexdigest()

    @property
    def effective_sha256(self) -> str:
        """返回不包含生成时刻和版本信封字段的有效配置摘要。"""
        content = self.content_wire()
        content.pop("config_revision")
        content.pop("generated_at")
        return hashlib.sha256(_canonical_json(content).encode("utf-8")).hexdigest()

    def to_wire(self) -> dict[str, object]:
        value = self.content_wire()
        value["sha256"] = self.sha256
        return value

    @classmethod
    def from_wire(cls, value: Mapping[str, object]) -> ConfigurationBundle:
        _require_keys(
            value,
            {
                "contract_version",
                "host_id",
                "config_revision",
                "generated_at",
                "stations",
                "sha256",
            },
            "configuration bundle",
        )
        supplied_digest = _string(value["sha256"], "configuration sha256")
        content = {key: value[key] for key in value if key != "sha256"}
        actual_digest = hashlib.sha256(_canonical_json(content).encode("utf-8")).hexdigest()
        if not _constant_time_equal(supplied_digest, actual_digest):
            raise ValueError("configuration bundle digest does not match its content")
        raw_stations = _array(value["stations"], "configuration stations")
        contract_version = _positive_int(
            value["contract_version"], "configuration contract version"
        )
        return cls(
            host_id=_string(value["host_id"], "configuration host_id"),
            config_revision=_positive_int(value["config_revision"], "configuration revision"),
            generated_at=_string(value["generated_at"], "configuration generated_at"),
            stations=tuple(
                ConfiguredStation.from_wire(
                    _object(item, "configuration station"),
                    allow_missing_model_ids=(
                        contract_version == LEGACY_CONFIGURATION_CONTRACT_VERSION
                    ),
                )
                for item in raw_stations
            ),
            contract_version=contract_version,
        )


def configuration_to_wire(bundle: ConfigurationBundle) -> dict[str, object]:
    """把 bundle 编码为 JSON 传输值。"""
    return bundle.to_wire()


def configuration_from_wire(value: Mapping[str, object]) -> ConfigurationBundle:
    """在 bundle 替换本地确认状态前解码并校验它。"""
    return ConfigurationBundle.from_wire(value)


def canonical_json(value: Mapping[str, object]) -> str:
    """暴露摘要和请求签名共用的规范化方法。"""
    return _canonical_json(value)


def _canonical_json(value: Mapping[str, object]) -> str:
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
    except (TypeError, ValueError) as error:
        raise ValueError("configuration content must be canonical JSON") from error


def _require_keys(
    value: Mapping[str, object],
    expected: set[str],
    label: str,
    *,
    optional: set[str] | frozenset[str] = frozenset(),
) -> None:
    actual = set(value)
    allowed = expected | set(optional)
    if not expected.issubset(actual) or not actual.issubset(allowed):
        raise ValueError(f"{label} has unsupported or missing fields")
    if any(_looks_like_secret(key) for key in value):
        raise ValueError(f"{label} contains a credential-bearing field")


def _looks_like_secret(value: str) -> bool:
    lowered = value.replace("-", "_").lower()
    return any(
        marker in lowered
        for marker in (
            "password",
            "passwd",
            "secret",
            "token",
            "credential",
            "private_key",
            "bearer",
        )
    )


def _object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"{label} must be an object")
    return dict(cast(Mapping[str, object], value))


def _array(value: object, label: str) -> tuple[object, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ValueError(f"{label} must be an array")
    return tuple(value)


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _strings(value: object, label: str) -> tuple[str, ...]:
    raw = _array(value, label)
    if any(not isinstance(item, str) or not item for item in raw):
        raise ValueError(f"{label} contains an invalid string")
    return tuple(cast(str, item) for item in raw)


def _number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a number")
    return float(value)


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _validate_digest(value: str, label: str) -> None:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value.lower()):
        raise ValueError(f"{label} must be a SHA-256 hexadecimal digest")


def _constant_time_equal(left: str, right: str) -> bool:
    return hashlib.sha256(left.encode()).digest() == hashlib.sha256(right.encode()).digest()


__all__ = [
    "CONFIGURATION_CONTRACT_VERSION",
    "ConfigurationArtifact",
    "ConfigurationBundle",
    "ConfigurationTemplate",
    "ConfiguredConnector",
    "ConfiguredPoint",
    "ConfiguredStation",
    "ResolvedRuntimeParameters",
    "canonical_json",
    "configuration_from_wire",
    "configuration_to_wire",
]
