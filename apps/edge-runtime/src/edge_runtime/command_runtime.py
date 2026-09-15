"""推理机委托命令循环和本地连接器组合。"""

from __future__ import annotations

import ssl
from collections.abc import Callable, Mapping
from contextlib import suppress
from pathlib import Path
from time import sleep

from nvsop_contracts import ConnectionTestOutcome

from edge_runtime.configuration import (
    EdgeRuntimeConfiguration,
    LocalIsapiConnectorConfiguration,
    connector_configuration,
    load_configuration,
    safe_url,
)
from edge_runtime.connectors.hikvision import IsapiConnector
from edge_runtime.connectors.port import Connector, Reachability
from edge_runtime.connectors.transport import UrllibIsapiTransport
from edge_runtime.supervisor.delegated_commands import (
    ConnectionTestCommandRunner,
    ConnectionTestExecutor,
    LocalConnector,
    LocalConnectorRegistry,
    LocalProbeResult,
)
from edge_runtime.supervisor.delegated_transport import CommandTransportError, HttpCommandTransport

ConnectorFactory = Callable[[LocalIsapiConnectorConfiguration], Connector]


def build_isapi_connector(configuration: LocalIsapiConnectorConfiguration) -> Connector:
    """从本机配置构造生产 ISAPI 适配器。"""
    return IsapiConnector(
        transport=UrllibIsapiTransport(
            base_url=safe_url(
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


class _IsapiConnectionTestProbe:
    """把连接器包的健康结果转换为 supervisor 的命令探测结果。"""

    def __init__(self, connector: Connector) -> None:
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

    def __init__(
        self,
        configurations: tuple[LocalIsapiConnectorConfiguration, ...],
        adapter_factory: ConnectorFactory = build_isapi_connector,
    ) -> None:
        connectors: dict[str, LocalConnector] = {}
        adapters: dict[str, Connector] = {}
        for configuration in configurations:
            if configuration.connector_id in connectors:
                raise ValueError(f"duplicate local connector {configuration.connector_id}")
            adapter = adapter_factory(configuration)
            adapters[configuration.connector_id] = adapter
            connectors[configuration.connector_id] = LocalConnector(
                revision=configuration.revision,
                connector_type=configuration.connector_type,
                configuration=connector_configuration(configuration.base_url),
                credentials_configured=configuration.credentials_configured,
                probe=_IsapiConnectionTestProbe(adapter),
            )
        self._connectors = connectors
        self._adapters = adapters

    @property
    def adapters(self) -> Mapping[str, Connector]:
        """真实适配器,供实时连接器运行时与命令探测共用同一实例。"""
        return dict(self._adapters)

    def resolve(self, connector_id: str) -> LocalConnector | None:
        """按中心命令的目标 ID 返回本机真实连接器。"""
        return self._connectors.get(connector_id)


class ConnectionTestCommandLoop:
    """持续驱动领取、真实本地探测和回报;中心暂时不可达时安全重试。"""

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
        """运行一轮真实命令处理,供进程循环和系统接缝共同使用。"""
        return self._runner.run_once()

    def run_forever(self, *, should_stop: Callable[[], bool]) -> None:
        """持续运行;传输失败只影响本轮并交给下一轮重新领取。"""
        while not should_stop():
            with suppress(CommandTransportError):
                self.run_once()
            self._sleep(self._poll_interval)


def build_connection_test_runner(
    *,
    center_url: str,
    host_id: str,
    host_private_key: str,
    command_timeout: float,
    local_connectors: tuple[LocalIsapiConnectorConfiguration, ...],
    ssl_context: ssl.SSLContext | None = None,
    adapter_factory: ConnectorFactory = build_isapi_connector,
) -> ConnectionTestCommandRunner:
    """装配生产委托命令执行器及本机真实连接器适配器。"""
    return ConnectionTestCommandRunner(
        transport=HttpCommandTransport(
            center_url=center_url,
            host_id=host_id,
            host_private_key=host_private_key,
            timeout=command_timeout,
            ssl_context=ssl_context,
        ),
        executor=ConnectionTestExecutor(
            registry=ConfiguredLocalConnectorRegistry(
                local_connectors,
                adapter_factory=adapter_factory,
            ),
            timeout=command_timeout,
        ),
    )


def build_connection_test_loop(
    *,
    center_url: str,
    host_id: str,
    host_private_key: str,
    command_timeout: float,
    command_poll_interval: float,
    local_connectors: tuple[LocalIsapiConnectorConfiguration, ...],
    ssl_context: ssl.SSLContext | None = None,
    sleep_fn: Callable[[float], None] = sleep,
    adapter_factory: ConnectorFactory = build_isapi_connector,
) -> ConnectionTestCommandLoop:
    """装配生产委托命令循环及本机真实连接器适配器。"""
    return ConnectionTestCommandLoop(
        runner=build_connection_test_runner(
            center_url=center_url,
            host_id=host_id,
            host_private_key=host_private_key,
            command_timeout=command_timeout,
            local_connectors=local_connectors,
            ssl_context=ssl_context,
            adapter_factory=adapter_factory,
        ),
        poll_interval=command_poll_interval,
        sleep_fn=sleep_fn,
    )


def build_connection_test_loop_from_configuration(
    config: EdgeRuntimeConfiguration,
    *,
    adapter_factory: ConnectorFactory = build_isapi_connector,
) -> ConnectionTestCommandLoop:
    """从已解析的推理机配置装配生产命令循环。"""
    return build_connection_test_loop(
        center_url=config.center_url,
        host_id=config.host_id,
        host_private_key=config.host_private_key,
        command_timeout=config.command_timeout,
        command_poll_interval=config.command_poll_interval,
        local_connectors=config.connectors,
        ssl_context=config.ssl_context,
        adapter_factory=adapter_factory,
    )


def build_connection_test_loop_from_file(
    config_path: str | Path,
    *,
    adapter_factory: ConnectorFactory = build_isapi_connector,
) -> ConnectionTestCommandLoop:
    """从推理机本地配置文件装配生产命令循环。"""
    return build_connection_test_loop_from_configuration(
        load_configuration(config_path), adapter_factory=adapter_factory
    )


__all__ = [
    "ConfiguredLocalConnectorRegistry",
    "ConnectionTestCommandLoop",
    "ConnectorFactory",
    "build_connection_test_loop",
    "build_connection_test_loop_from_configuration",
    "build_connection_test_loop_from_file",
    "build_connection_test_runner",
    "build_isapi_connector",
]
