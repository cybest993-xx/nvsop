"""把中心确认 bundle 解析为 edge 运行时输入。

中心拥有拓扑、模板版本和生效运行参数;部署文件只拥有不能越过配置契约的主机本地连接信息,
包括本地进程 URL、适配器 profile 和设备凭据。本模块是两种视图在创建运行时前汇合的唯一位置。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from urllib.parse import urlsplit

from nvsop_contracts import (
    ConfigurationBundle,
    ConfiguredConnector,
    ConfiguredStation,
    Measured,
    Pushed,
)

from edge_runtime.configuration import LocalIsapiConnectorConfiguration
from edge_runtime.configuration_values import _non_empty_string, _object, _require_keys
from edge_runtime.connectors.port import InputPoint, OutputPoint
from edge_runtime.judgment.model import Ordering, RuntimeParameters, Template
from edge_runtime.station_runtime import StationRuntimeConfiguration


class RuntimeConfigurationError(ValueError):
    """中心确认视图无法与本机配置安全组合。"""


@dataclass(frozen=True, slots=True)
class StationRuntimeBinding:
    """一个工位的单一判定运行时及其多个后端输入切片。"""

    configuration: StationRuntimeConfiguration
    input_points: tuple[tuple[str, InputPoint], ...]
    output_points: tuple[tuple[str, OutputPoint], ...]
    connector_ids: tuple[str, ...]
    configurations: tuple[StationRuntimeConfiguration, ...] = ()

    def input_points_for(self, connector_id: str) -> tuple[InputPoint, ...]:
        return tuple(point for owner, point in self.input_points if owner == connector_id)

    def output_points_for(self, connector_id: str) -> tuple[OutputPoint, ...]:
        return tuple(point for owner, point in self.output_points if owner == connector_id)


@dataclass(frozen=True, slots=True)
class RuntimeConfiguration:
    """供 edge 组合根使用的完整运行时视图。"""

    stations: tuple[StationRuntimeBinding, ...]
    connectors: tuple[LocalIsapiConnectorConfiguration, ...]
    confirmed: ConfigurationBundle | None

    def station(self, station_id: str) -> StationRuntimeBinding:
        for value in self.stations:
            if value.configuration.station_id == station_id:
                return value
        raise KeyError(f"station runtime is not configured: {station_id}")


def bootstrap_runtime_configuration(
    *,
    stations: tuple[StationRuntimeConfiguration, ...],
    connectors: tuple[LocalIsapiConnectorConfiguration, ...],
) -> RuntimeConfiguration:
    """首次确认拉取成功前,保留本机部署骨架作为安全起点。"""
    _unique_connector_ids(connectors)
    grouped: dict[str, list[StationRuntimeBinding]] = {}
    for station in stations:
        bootstrap = replace(station, model_ids=())
        grouped.setdefault(station.station_id, []).append(
            StationRuntimeBinding(
                configuration=bootstrap,
                input_points=(),
                output_points=(),
                connector_ids=tuple(connector.connector_id for connector in connectors),
                configurations=(bootstrap,),
            )
        )
    return RuntimeConfiguration(
        stations=tuple(_merge_station_bindings(values) for values in grouped.values()),
        connectors=connectors,
        confirmed=None,
    )


def confirmed_runtime_configuration(
    *,
    bundle: ConfigurationBundle,
    bootstrap_stations: tuple[StationRuntimeConfiguration, ...],
    local_connectors: tuple[LocalIsapiConnectorConfiguration, ...],
) -> RuntimeConfiguration:
    """解析最后确认的 bundle,不允许本机值覆盖中心拥有的配置。

    一个工位可以对应多个推理后端切片,但只能创建一个 supervisor 和一个本地状态作用域。
    本机配置只提供后端端点、请求体和凭据;模板、运行参数、连接器能力及点位拓扑均来自中心。
    """
    _unique_connector_ids(local_connectors)
    station_slices: dict[str, list[StationRuntimeBinding]] = {}
    connectors_by_id: dict[str, LocalIsapiConnectorConfiguration] = {}
    for configured in bundle.stations:
        binding = _confirmed_station(
            configured=configured,
            bootstrap_stations=bootstrap_stations,
        )
        station_slices.setdefault(configured.station_id, []).append(binding)
        for connector in _effective_connectors(
            configured=configured.connectors,
            local_connectors=local_connectors,
        ):
            previous = connectors_by_id.get(connector.connector_id)
            if previous is not None and previous != connector:
                raise RuntimeConfigurationError(f"connector {connector.connector_id} 被重复配置")
            connectors_by_id[connector.connector_id] = connector
    return RuntimeConfiguration(
        stations=tuple(_merge_station_bindings(values) for values in station_slices.values()),
        connectors=tuple(connectors_by_id.values()),
        confirmed=bundle,
    )


def validate_confirmed_runtime_configuration(
    *,
    bundle: ConfigurationBundle,
    bootstrap_stations: tuple[StationRuntimeConfiguration, ...],
    local_connectors: tuple[LocalIsapiConnectorConfiguration, ...],
) -> None:
    """在新拉取可以替换本地确认视图前,先验证完整运行时组合。"""
    confirmed_runtime_configuration(
        bundle=bundle,
        bootstrap_stations=bootstrap_stations,
        local_connectors=local_connectors,
    )


def _confirmed_station(
    *,
    configured: ConfiguredStation,
    bootstrap_stations: tuple[StationRuntimeConfiguration, ...],
) -> StationRuntimeBinding:
    local = _local_station(configured, bootstrap_stations)
    if configured.template is None:
        raise RuntimeConfigurationError(f"station {configured.station_id} 没有完整的已确认模板")
    template = _template_from_artifact(configured)
    parameters = RuntimeParameters(
        idle_timeout=configured.runtime_parameters.idle_timeout_seconds,
        step_deadline=configured.runtime_parameters.step_deadline_seconds,
    )
    effective = replace(
        local,
        template=template,
        parameters=parameters,
        backend_id=configured.backend_id,
        template_version_id=configured.template.version_id,
        template_sha256=configured.template.version_sha256,
        model_ids=configured.model_ids,
        disposition_policy=configured.runtime_parameters.disposition_policy,
    )
    connector_ids = tuple(connector.connector_id for connector in configured.connectors)
    input_points: list[tuple[str, InputPoint]] = []
    output_points: list[tuple[str, OutputPoint]] = []
    for point in configured.points:
        address = point.address
        if point.direction == "input":
            input_points.append((point.connector_id, InputPoint(label=point.name, address=address)))
        elif point.direction == "output":
            output_points.append(
                (point.connector_id, OutputPoint(label=point.name, address=address))
            )
        else:
            raise RuntimeConfigurationError(
                f"point {point.point_id} has unsupported direction {point.direction!r}"
            )
    return StationRuntimeBinding(
        configuration=effective,
        input_points=tuple(input_points),
        output_points=tuple(output_points),
        connector_ids=connector_ids,
        configurations=(effective,),
    )


def _merge_station_bindings(
    bindings: Sequence[StationRuntimeBinding],
) -> StationRuntimeBinding:
    """将同一工位的后端切片合并为一个 supervisor 作用域。"""
    if not bindings:
        raise RuntimeConfigurationError("station runtime binding 不能为空")
    first = bindings[0]
    configurations = tuple(
        configuration
        for binding in bindings
        for configuration in (binding.configurations or (binding.configuration,))
    )
    if any(not _same_sop_view(first.configuration, value) for value in configurations[1:]):
        raise RuntimeConfigurationError(
            f"station {first.configuration.station_id} 的 backend SOP 视图不兼容"
        )
    if len(configurations) > 1 and any(value.backend_id is None for value in configurations):
        raise RuntimeConfigurationError(
            f"station {first.configuration.station_id} 的多 backend 运行时必须声明 backend_id"
        )
    input_points: list[tuple[str, InputPoint]] = []
    output_points: list[tuple[str, OutputPoint]] = []
    connector_ids: list[str] = []
    for binding in bindings:
        for input_point in binding.input_points:
            if input_point not in input_points:
                input_points.append(input_point)
        for output_point in binding.output_points:
            if output_point not in output_points:
                output_points.append(output_point)
        for connector_id in binding.connector_ids:
            if connector_id not in connector_ids:
                connector_ids.append(connector_id)
    return StationRuntimeBinding(
        configuration=first.configuration,
        input_points=tuple(input_points),
        output_points=tuple(output_points),
        connector_ids=tuple(connector_ids),
        configurations=configurations,
    )


def _same_sop_view(left: StationRuntimeConfiguration, right: StationRuntimeConfiguration) -> bool:
    """判断多个后端是否能共享一个 SOP supervisor。"""
    return (
        left.station_id == right.station_id
        and left.template == right.template
        and left.parameters == right.parameters
        and left.margins == right.margins
        and left.template_version_id == right.template_version_id
        and left.template_sha256 == right.template_sha256
        and left.disposition_policy == right.disposition_policy
    )


def _local_station(
    configured: ConfiguredStation,
    bootstrap_stations: tuple[StationRuntimeConfiguration, ...],
) -> StationRuntimeConfiguration:
    exact = tuple(
        station
        for station in bootstrap_stations
        if station.station_id == configured.station_id
        and station.backend_id == configured.backend_id
    )
    candidates = exact or tuple(
        station
        for station in bootstrap_stations
        if station.station_id == configured.station_id and station.backend_id is None
    )
    if len(candidates) != 1:
        raise RuntimeConfigurationError(
            f"station {configured.station_id} 必须有且只有一个本地运行时端点"
        )
    return candidates[0]


def _effective_connectors(
    *,
    configured: Sequence[ConfiguredConnector],
    local_connectors: tuple[LocalIsapiConnectorConfiguration, ...],
) -> tuple[LocalIsapiConnectorConfiguration, ...]:
    local_by_id = {connector.connector_id: connector for connector in local_connectors}
    result: list[LocalIsapiConnectorConfiguration] = []
    for connector in configured:
        connector_id = _non_empty_string(connector.connector_id, "connector_id")
        local = local_by_id.get(connector_id)
        if local is None:
            raise RuntimeConfigurationError(
                f"connector {connector_id} has no local credential/profile configuration"
            )
        connector_type = _non_empty_string(connector.connector_type, "connector_type")
        if connector_type != local.connector_type:
            raise RuntimeConfigurationError(
                f"connector {connector_id} local type does not match confirmed topology"
            )
        _validate_connector_address(connector, local)
        if isinstance(connector.capability, Measured) and isinstance(
            connector.capability.delivery, Pushed
        ):
            raise RuntimeConfigurationError(
                f"connector {connector_id} declares pushed delivery without a push runtime"
            )
        result.append(
            replace(
                local,
                revision=connector.revision,
                capability=connector.capability,
                connector_type=connector_type,
            )
        )
    if len({connector.connector_id for connector in result}) != len(result):
        raise RuntimeConfigurationError("confirmed station connector ids must be unique")
    return tuple(result)


def _validate_connector_address(
    configured: ConfiguredConnector, local: LocalIsapiConnectorConfiguration
) -> None:
    address = _non_empty_string(configured.address, "connector address")
    parsed = urlsplit(local.base_url)
    if parsed.hostname is None or parsed.hostname.lower() != address.lower():
        raise RuntimeConfigurationError(
            f"connector {local.connector_id} local address does not match confirmed topology"
        )
    configured_port = configured.port
    if configured_port is not None and parsed.port != configured_port:
        raise RuntimeConfigurationError(
            f"connector {local.connector_id} local port does not match confirmed topology"
        )


def _template_from_artifact(configured: ConfiguredStation) -> Template:
    assert configured.template is not None
    artifact = next(
        (value for value in configured.template.artifacts if value.name == "template.json"),
        None,
    )
    if artifact is None:
        raise RuntimeConfigurationError(
            f"station {configured.station_id} confirmed template has no template.json artifact"
        )
    manifest = next(
        (value for value in configured.template.artifacts if value.name == "manifest.json"),
        None,
    )
    if manifest is not None and manifest.sha256 != configured.template.version_sha256:
        raise RuntimeConfigurationError(
            f"station {configured.station_id} confirmed template digest does not match manifest"
        )
    try:
        raw = json.loads(artifact.content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeConfigurationError("confirmed template.json is not valid JSON") from error
    document = _object(raw, "confirmed template")
    _require_keys(
        document,
        required={"boundary", "format_version", "ordering", "runtime_defaults", "steps"},
    )
    if document["format_version"] != 1:
        raise RuntimeConfigurationError("confirmed template format is unsupported")
    steps = _steps(document["steps"])
    by_number = dict(steps)
    try:
        ordering = {"strict": Ordering.ORDERED, "unordered": Ordering.UNORDERED}[
            _non_empty_string(document["ordering"], "template ordering")
        ]
    except KeyError as error:
        raise RuntimeConfigurationError("confirmed template ordering is unsupported") from error
    boundary = _object(document["boundary"], "confirmed template boundary")
    _require_keys(boundary, required={"start_signal", "end_signals"})
    start_signal = _signal(boundary["start_signal"], by_number, "start_signal")
    if start_signal is None:
        raise RuntimeConfigurationError("confirmed template must declare a start signal")
    raw_end_signals = boundary["end_signals"]
    if not isinstance(raw_end_signals, list):
        raise RuntimeConfigurationError("confirmed template end_signals must be an array")
    end_signals = tuple(
        signal
        for raw_signal in raw_end_signals
        if (signal := _signal(raw_signal, by_number, "end_signal")) is not None
    )
    try:
        return Template(
            steps=tuple(description for _, description in steps),
            ordering=ordering,
            start_signal=start_signal,
            end_signals=end_signals,
        )
    except ValueError as error:
        raise RuntimeConfigurationError(str(error)) from error


def _steps(value: object) -> tuple[tuple[int, str], ...]:
    if not isinstance(value, list) or not value:
        raise RuntimeConfigurationError("confirmed template steps must be a non-empty array")
    result: list[tuple[int, str]] = []
    for raw in value:
        step = _object(raw, "confirmed template step")
        _require_keys(step, required={"number", "name", "description"})
        number = step["number"]
        if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
            raise RuntimeConfigurationError("confirmed template step number is invalid")
        result.append((number, _non_empty_string(step["description"], "step description")))
    numbers = tuple(number for number, _ in result)
    if len(set(numbers)) != len(numbers):
        raise RuntimeConfigurationError("confirmed template step numbers must be unique")
    if numbers != tuple(range(1, len(numbers) + 1)):
        raise RuntimeConfigurationError("confirmed template step numbers must be consecutive")
    return tuple(result)


def _signal(value: object, steps: Mapping[int, str], label: str) -> str | None:
    if value is None:
        return None
    signal = _object(value, f"confirmed template {label}")
    kind = _non_empty_string(signal.get("kind"), f"{label} kind")
    if kind == "action":
        _require_keys(signal, required={"kind", "action_number"})
        number = signal["action_number"]
        if isinstance(number, bool) or not isinstance(number, int) or number not in steps:
            raise RuntimeConfigurationError(f"confirmed template {label} action is unknown")
        return steps[number]
    if kind == "external":
        _require_keys(signal, required={"kind", "semantic_label"})
        return _non_empty_string(signal["semantic_label"], f"{label} semantic_label")
    raise RuntimeConfigurationError(f"confirmed template {label} kind is unsupported")


def _unique_connector_ids(connectors: tuple[LocalIsapiConnectorConfiguration, ...]) -> None:
    ids = [connector.connector_id for connector in connectors]
    if len(set(ids)) != len(ids):
        raise RuntimeConfigurationError("local connector ids must be unique")


__all__ = [
    "RuntimeConfiguration",
    "RuntimeConfigurationError",
    "StationRuntimeBinding",
    "bootstrap_runtime_configuration",
    "confirmed_runtime_configuration",
    "validate_confirmed_runtime_configuration",
]
