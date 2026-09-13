"""推理机边缘运行时的生产组成根。"""

from __future__ import annotations

import json
import os
import signal
import ssl
import threading
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from time import monotonic, sleep
from types import FrameType

from nvsop_contracts import ConnectionTestOutcome

from edge_runtime.configuration import (
    EdgeRuntimeConfiguration,
    LocalIsapiConnectorConfiguration,
    connector_configuration,
    load_configuration,
    safe_url,
)
from edge_runtime.connectors.hikvision import IsapiConnector
from edge_runtime.connectors.port import Reachability
from edge_runtime.connectors.transport import UrllibIsapiTransport
from edge_runtime.judgment.model import HostLiveness
from edge_runtime.local_state.store import LocalState, open_local_state
from edge_runtime.media import MediaRuntime, validate_sop_camera_bindings
from edge_runtime.station_runtime import (
    InputWaitExpired,
    SseStationInputSource,
    StationInputSource,
    StationRuntimeConfiguration,
)
from edge_runtime.stream_health import StreamFact, StreamHealthEvent
from edge_runtime.supervisor.delegated_commands import (
    ConnectionTestCommandRunner,
    ConnectionTestExecutor,
    LocalConnector,
    LocalConnectorRegistry,
    LocalProbeResult,
)
from edge_runtime.supervisor.delegated_transport import CommandTransportError, HttpCommandTransport
from edge_runtime.supervisor.inputs import StreamHealthObserved
from edge_runtime.supervisor.startup import resume_station
from edge_runtime.supervisor.station import StationSupervisor


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
            connectors[configuration.connector_id] = LocalConnector(
                revision=configuration.revision,
                connector_type=configuration.connector_type,
                configuration=connector_configuration(configuration.base_url),
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


class AutonomousStation:
    """把本地状态、恢复规则、supervisor 和实时输入组成一条自治链。"""

    def __init__(self, *, supervisor: StationSupervisor, source: StationInputSource) -> None:
        self._supervisor = supervisor
        self._source = source

    @property
    def supervisor(self) -> StationSupervisor:
        return self._supervisor

    def close(self) -> None:
        """关闭工位输入源。"""
        self._source.close()

    def run_forever(self, *, should_stop: Callable[[], bool]) -> None:
        """消费输入并在停机时结束当前实例; 持久化由 supervisor 负责。"""
        try:
            while not should_stop():
                arriving = self._source.next_input(timeout=self._supervisor.timeout())
                if isinstance(arriving, InputWaitExpired):
                    self._supervisor.wake(host=HostLiveness.ALIVE)
                elif arriving is None:
                    if self._source.ended:
                        ended = StreamHealthObserved(
                            event=StreamHealthEvent(
                                fact=StreamFact.STREAM_ENDED,
                                at_monotonic=monotonic(),
                            )
                        )
                        self._supervisor.receive(ended)
                        break
                    self._supervisor.wake(host=HostLiveness.ALIVE)
                else:
                    self._supervisor.receive(arriving)
            self._supervisor.interrupt()
        finally:
            self.close()


class AutonomousRuntime:
    """同时运行委托命令与各工位实时判定循环。"""

    def __init__(
        self,
        *,
        command_loop: ConnectionTestCommandLoop,
        stations: tuple[AutonomousStation, ...],
        state: LocalState,
        media: MediaRuntime | None = None,
    ) -> None:
        self._command_loop = command_loop
        self._stations = stations
        self._state = state
        self._media = media

    @property
    def stations(self) -> tuple[AutonomousStation, ...]:
        return self._stations

    def run_forever(self, *, should_stop: Callable[[], bool]) -> None:
        """让命令循环和工位循环并行运行, 任一线程出错都请求整体停机。"""
        stopped = threading.Event()
        errors: list[BaseException] = []
        threads: list[threading.Thread] = []

        def stop_requested() -> bool:
            return stopped.is_set() or should_stop()

        def run(target: Callable[[], None]) -> None:
            try:
                target()
            except BaseException as error:
                errors.append(error)
                stopped.set()

        def run_media() -> None:
            """让媒体故障重试, 不把媒体启动放到判定线程的前置路径。"""
            if self._media is None:
                return
            while not stop_requested():
                try:
                    self._media.start()
                    return
                except BaseException:
                    stopped.wait(0.5)

        try:
            threads = [
                *(
                    [
                        threading.Thread(
                            target=run_media,
                            name="edge-media-runtime",
                            daemon=True,
                        )
                    ]
                    if self._media is not None
                    else []
                ),
                threading.Thread(
                    target=run,
                    args=(lambda: self._command_loop.run_forever(should_stop=stop_requested),),
                    daemon=True,
                ),
                *[
                    threading.Thread(
                        target=run,
                        args=(
                            lambda station=station: station.run_forever(should_stop=stop_requested),
                        ),
                        daemon=True,
                    )
                    for station in self._stations
                ],
            ]
            for thread in threads:
                thread.start()
            while not stop_requested() and any(thread.is_alive() for thread in threads):
                sleep(0.05)
        finally:
            stopped.set()
            for station in self._stations:
                station.close()
            for thread in threads:
                thread.join()
            if self._media is not None:
                self._media.close()
            self._state.close()
        if errors:
            raise errors[0]

    def close(self) -> None:
        """关闭输入和本地状态, 供配置失败和进程退出路径共同调用。"""
        for station in self._stations:
            station.close()
        if self._media is not None:
            self._media.close()
        self._state.close()


def build_connection_test_runner(
    *,
    center_url: str,
    host_id: str,
    host_private_key: str,
    command_timeout: float,
    local_connectors: tuple[LocalIsapiConnectorConfiguration, ...],
    ssl_context: ssl.SSLContext | None = None,
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
            registry=ConfiguredLocalConnectorRegistry(local_connectors),
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
        ),
        poll_interval=command_poll_interval,
        sleep_fn=sleep_fn,
    )


def build_connection_test_loop_from_file(
    config_path: str | Path,
) -> ConnectionTestCommandLoop:
    """从推理机本地配置文件装配生产命令循环。"""
    config = load_configuration(config_path)
    return _build_connection_test_loop(config)


def _build_connection_test_loop(config: EdgeRuntimeConfiguration) -> ConnectionTestCommandLoop:
    return build_connection_test_loop(
        center_url=config.center_url,
        host_id=config.host_id,
        host_private_key=config.host_private_key,
        command_timeout=config.command_timeout,
        command_poll_interval=config.command_poll_interval,
        local_connectors=config.connectors,
        ssl_context=config.ssl_context,
    )


def build_autonomous_runtime_from_file(config_path: str | Path) -> AutonomousRuntime:
    """从本地配置装配命令循环、SQLite 状态和每条实时判定流。"""
    config = load_configuration(config_path, include_stations=True)
    command_loop = _build_connection_test_loop(config)
    if config.media is not None:
        if config.media.host_id != config.host_id:
            raise ValueError("media host_id must match edge host_id")
        validate_sop_camera_bindings(
            config.media, {station.station_id for station in config.stations}
        )
    if config.local_state_path is None:
        raise ValueError("local_state_path is required for autonomous runtime")
    state = open_local_state(str(config.local_state_path))
    stations: list[AutonomousStation] = []
    try:
        for station_config in config.stations:
            source = SseStationInputSource(
                inference_url=station_config.inference_url,
                request_body=station_config.request_body,
                timeout=config.command_timeout,
            )
            station_store = state.station(station_config.station_id)
            stations.append(
                AutonomousStation(
                    supervisor=resume_station(
                        station_store,
                        template=station_config.template,
                        parameters=station_config.parameters,
                        margins=station_config.margins,
                    ),
                    source=source,
                )
            )
    except Exception:
        for station in stations:
            station.close()
        state.close()
        raise
    return AutonomousRuntime(
        command_loop=command_loop,
        stations=tuple(stations),
        state=state,
        media=MediaRuntime(config.media) if config.media is not None else None,
    )


def main() -> int:
    """启动推理机的自治命令和实时判定循环。"""
    config_path = os.environ.get("NVSOP_EDGE_COMMAND_CONFIG_FILE")
    if not config_path:
        raise SystemExit("NVSOP_EDGE_COMMAND_CONFIG_FILE is required")

    try:
        runtime = build_autonomous_runtime_from_file(config_path)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise SystemExit(f"edge runtime configuration is invalid: {error}") from None

    stopping = False

    def request_stop(_signum: int, _frame: FrameType | None) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    runtime.run_forever(should_stop=lambda: stopping)
    return 0


__all__ = [
    "AutonomousRuntime",
    "AutonomousStation",
    "ConfiguredLocalConnectorRegistry",
    "ConnectionTestCommandLoop",
    "InputWaitExpired",
    "LocalIsapiConnectorConfiguration",
    "SseStationInputSource",
    "StationInputSource",
    "StationRuntimeConfiguration",
    "build_autonomous_runtime_from_file",
    "build_connection_test_loop",
    "build_connection_test_loop_from_file",
    "build_connection_test_runner",
    "main",
]
