"""推理机边缘运行时的生产组成根。"""

from __future__ import annotations

import json
import logging
import os
import signal
import sqlite3
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic, sleep, time
from types import FrameType

from nvsop_contracts import ConfigurationBundle

from edge_runtime.command_runtime import (
    ConfiguredLocalConnectorRegistry,
    ConnectionTestCommandLoop,
    ConnectorFactory,
    build_connection_test_loop,
    build_connection_test_loop_from_configuration,
    build_connection_test_loop_from_file,
    build_connection_test_runner,
    build_isapi_connector,
)
from edge_runtime.configuration import (
    EdgeRuntimeConfiguration,
    LocalIsapiConnectorConfiguration,
    load_autonomous_configuration,
)
from edge_runtime.configuration_sync import ConfigurationSynchronizer, HttpConfigurationPuller
from edge_runtime.connectors.port import (
    InputPoint,
    OutputPoint,
    Refused,
    WriteOutcome,
    WriteRefusal,
)
from edge_runtime.connectors.runtime import ConnectorRuntime, ConnectorRuntimeSet
from edge_runtime.connectors.writes import (
    OutputDispatcher,
    SQLiteWriteLedger,
    WriteAttempted,
    WriteRequest,
    diagnostic_event,
)
from edge_runtime.judgment.model import HostInstant, HostLiveness
from edge_runtime.local_state.store import LocalState, open_local_state
from edge_runtime.media import MediaRuntime, validate_sop_camera_bindings
from edge_runtime.reporting import DecisionReporter, ReportContext
from edge_runtime.reporting_transport import HttpDecisionReportTransport
from edge_runtime.runtime_configuration import (
    RuntimeConfiguration,
    StationRuntimeBinding,
    bootstrap_runtime_configuration,
    confirmed_runtime_configuration,
    validate_confirmed_runtime_configuration,
)
from edge_runtime.station_runtime import (
    InputWaitExpired,
    MultiplexedStationInputSource,
    SseStationInputSource,
    StationInputSource,
    StationRuntimeConfiguration,
)
from edge_runtime.stream_health import StreamFact, StreamHealthEvent
from edge_runtime.supervisor.inputs import StreamHealthObserved
from edge_runtime.supervisor.startup import resume_station
from edge_runtime.supervisor.station import StationSupervisor

_logger = logging.getLogger("edge_runtime")


def _log_write_attempt(event: WriteAttempted) -> None:
    """把连接器写入事件作为结构化字段交给边缘日志适配器。"""
    _logger.info(event.event, extra=event.as_dict())


class AutonomousStation:
    """把本地状态、连接器运行时、supervisor 和实时输入组成一条自治链。"""

    def __init__(
        self,
        *,
        supervisor: StationSupervisor,
        source: StationInputSource,
        station_id: str | None = None,
        connector_runtimes: tuple[ConnectorRuntime, ...] = (),
        output_dispatchers: Mapping[str, OutputDispatcher] | None = None,
        output_points: Mapping[str, tuple[OutputPoint, ...]] | None = None,
        diagnostics: Callable[[WriteAttempted], None] | None = None,
    ) -> None:
        self._supervisor = supervisor
        self._source = source
        self._station_id = station_id
        self._connector_runtimes = connector_runtimes
        self._output_dispatchers = dict(output_dispatchers or {})
        self._output_points = dict(output_points or {})
        self._diagnostics = diagnostics or _log_write_attempt

    @property
    def station_id(self) -> str | None:
        return self._station_id

    @property
    def supervisor(self) -> StationSupervisor:
        return self._supervisor

    @property
    def connector_runtimes(self) -> tuple[ConnectorRuntime, ...]:
        return self._connector_runtimes

    @property
    def output_dispatchers(self) -> Mapping[str, OutputDispatcher]:
        return dict(self._output_dispatchers)

    @property
    def output_points(self) -> Mapping[str, tuple[OutputPoint, ...]]:
        return {connector_id: tuple(points) for connector_id, points in self._output_points.items()}

    def write_output(self, request: WriteRequest) -> WriteOutcome:
        """只向当前工位已确认输出拓扑中的点位派发写入。"""
        if request.station_id != self._station_id:
            return self._refuse_topology(
                request,
                "输出请求不属于当前工位的已确认 topology",
            )
        configured_points = self._output_points.get(request.connector_id)
        if configured_points is None or request.point not in configured_points:
            return self._refuse_topology(
                request,
                "输出点不属于当前工位的已确认 output topology",
            )
        dispatcher = self._output_dispatchers.get(request.connector_id)
        if dispatcher is None:
            return self._refuse_topology(
                request,
                "输出连接器不属于当前工位的已确认 output topology",
            )
        return dispatcher.write(request)

    def _refuse_topology(self, request: WriteRequest, detail: str) -> Refused:
        """在物理接缝之前拒绝未确认目标, 并发出同一格式的诊断事件。"""
        outcome = Refused(reason=WriteRefusal.OUTPUT_NOT_CONFIGURED, detail=detail)
        self._diagnostics(diagnostic_event(request, outcome, replayed=False))
        return outcome

    def close(self) -> None:
        """关闭当前工位输入源。"""
        self._source.close()

    def run_forever(self, *, should_stop: Callable[[], bool]) -> None:
        """消费输入、按能力声明轮询连接器, 并在切换或停机时结束当前实例。"""
        try:
            while not should_stop():
                supervisor = self._supervisor
                source = self._source
                connector_runtimes = self._connector_runtimes
                self._poll_due(
                    supervisor=supervisor,
                    connector_runtimes=connector_runtimes,
                )
                arriving = source.next_input(
                    timeout=self._next_input_timeout(
                        supervisor=supervisor,
                        connector_runtimes=connector_runtimes,
                    )
                )
                if isinstance(arriving, InputWaitExpired):
                    supervisor.wake(host=HostLiveness.ALIVE)
                elif arriving is None:
                    if source.ended:
                        ended = StreamHealthObserved(
                            event=StreamHealthEvent(
                                fact=StreamFact.STREAM_ENDED,
                                at_monotonic=monotonic(),
                            )
                        )
                        supervisor.receive(ended)
                        break
                    supervisor.wake(host=HostLiveness.ALIVE)
                else:
                    supervisor.receive(arriving)
                self._poll_due(
                    supervisor=supervisor,
                    connector_runtimes=connector_runtimes,
                )
            self._supervisor.interrupt()
        finally:
            self.close()

    @staticmethod
    def _next_input_timeout(
        *,
        supervisor: StationSupervisor,
        connector_runtimes: tuple[ConnectorRuntime, ...],
    ) -> float | None:
        timeout = supervisor.timeout()
        now = monotonic()
        for runtime in connector_runtimes:
            due = runtime.next_due(now=now)
            if due is None:
                continue
            connector_timeout = max(0.0, due - now)
            timeout = connector_timeout if timeout is None else min(timeout, connector_timeout)
        return timeout

    @staticmethod
    def _poll_due(
        *,
        supervisor: StationSupervisor,
        connector_runtimes: tuple[ConnectorRuntime, ...],
    ) -> None:
        now = monotonic()
        for runtime in connector_runtimes:
            due = runtime.next_due(now=now)
            if due is None or now < due:
                continue
            for arriving in runtime.poll(now=now):
                supervisor.receive(arriving)


@dataclass(frozen=True, slots=True)
class RuntimeComposition:
    """由一份活动的本地或已确认配置视图构造的对象集合。"""

    configuration: RuntimeConfiguration
    stations: tuple[AutonomousStation, ...]
    connector_runtimes: ConnectorRuntimeSet
    output_dispatchers: Mapping[str, OutputDispatcher]
    reporters: tuple[DecisionReporter, ...] = ()


class AutonomousRuntime:
    """同时运行委托命令与各工位实时判定循环。"""

    def __init__(
        self,
        *,
        command_loop: ConnectionTestCommandLoop,
        stations: tuple[AutonomousStation, ...],
        state: LocalState,
        media: MediaRuntime | None = None,
        reporters: tuple[DecisionReporter, ...] = (),
        configuration_sync: ConfigurationSynchronizer | None = None,
        maintenance_interval: float = 30.0,
        configuration: RuntimeConfiguration | None = None,
        configuration_factory: Callable[[ConfigurationBundle], RuntimeComposition] | None = None,
        connector_runtimes: ConnectorRuntimeSet | None = None,
        output_dispatchers: Mapping[str, OutputDispatcher] | None = None,
    ) -> None:
        if maintenance_interval <= 0:
            raise ValueError("maintenance_interval must be positive")
        self._command_loop = command_loop
        self._stations = list(stations)
        self._state = state
        self._media = media
        self._reporters = reporters
        self._configuration_sync = configuration_sync
        self._maintenance_interval = maintenance_interval
        self._configuration = configuration
        self._configuration_factory = configuration_factory
        self._connector_runtimes = connector_runtimes
        self._output_dispatchers = dict(output_dispatchers or {})
        self._lock = threading.RLock()

    @property
    def stations(self) -> tuple[AutonomousStation, ...]:
        with self._lock:
            return tuple(self._stations)

    @property
    def connector_runtimes(self) -> ConnectorRuntimeSet | None:
        with self._lock:
            return self._connector_runtimes

    @property
    def output_dispatchers(self) -> Mapping[str, OutputDispatcher]:
        with self._lock:
            return dict(self._output_dispatchers)

    @property
    def configuration(self) -> RuntimeConfiguration | None:
        with self._lock:
            return self._configuration

    def _apply_composition(self, composition: RuntimeComposition) -> None:
        with self._lock:
            old_stations = tuple(self._stations)
            self._stations = list(composition.stations)
            self._configuration = composition.configuration
            self._connector_runtimes = composition.connector_runtimes
            self._output_dispatchers = dict(composition.output_dispatchers)
            self._reporters = composition.reporters
        for station in old_stations:
            station.close()

    def apply_configuration(self, bundle: ConfigurationBundle) -> None:
        """当前工位循环停止后,构造并激活已确认视图。"""
        if self._configuration_factory is None:
            raise RuntimeError("runtime configuration replacement is not configured")
        self._apply_composition(self._configuration_factory(bundle))

    def run_forever(self, *, should_stop: Callable[[], bool]) -> None:
        """让命令循环和工位循环并行运行, 配置确认后安全重启本机运行周期。"""
        pending_bundle: ConfigurationBundle | None = None
        while not should_stop():
            cycle_stop = threading.Event()
            errors: list[BaseException] = []
            threads: list[threading.Thread] = []

            def stop_requested(current_stop: threading.Event = cycle_stop) -> bool:
                return current_stop.is_set() or should_stop()

            def run(
                target: Callable[[], None],
                *,
                current_errors: list[BaseException] = errors,
                current_stop: threading.Event = cycle_stop,
            ) -> None:
                try:
                    target()
                except BaseException as error:
                    current_errors.append(error)
                    current_stop.set()

            def run_media(current_stop: threading.Event = cycle_stop) -> None:
                """让媒体故障重试, 不把媒体启动放到判定线程的前置路径。"""
                if self._media is None:
                    return
                while not stop_requested():
                    try:
                        self._media.start()
                        return
                    except BaseException:
                        current_stop.wait(0.5)

            def run_maintenance(current_stop: threading.Event = cycle_stop) -> None:
                """重试中心辅助工作,但不把它放进判定进度。"""
                nonlocal pending_bundle
                while not stop_requested():
                    now = HostInstant(monotonic())
                    reported_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
                    for reporter in self._reporters:
                        # 上报失败不能阻塞判定,但必须留下可检索的诊断并等待下一轮重试。
                        try:
                            attempts = reporter.flush(now=now, reported_at=reported_at)
                        except (OSError, sqlite3.Error, ValueError) as error:
                            _logger.warning(
                                "edge.report_flush.failed error_type=%s",
                                type(error).__name__,
                            )
                        else:
                            failed_attempts = tuple(
                                attempt for attempt in attempts if not attempt.sent
                            )
                            if failed_attempts:
                                _logger.warning(
                                    "edge.report_flush.retry_pending failed_count=%s",
                                    len(failed_attempts),
                                )
                    if self._configuration_sync is not None:
                        try:
                            result = self._configuration_sync.synchronize(observed_at=now.seconds)
                        except (OSError, sqlite3.Error, ValueError) as error:
                            _logger.warning(
                                "edge.configuration_sync.failed error_type=%s",
                                type(error).__name__,
                            )
                        else:
                            if result.failure is not None:
                                _logger.warning(
                                    "edge.configuration_sync.rejected failure_code=%s",
                                    result.failure.code,
                                )
                            active = result.active
                            current = self.configuration
                            if (
                                result.applied
                                and active is not None
                                and (
                                    current is None
                                    or current.confirmed is None
                                    or current.confirmed.effective_sha256 != active.effective_sha256
                                )
                            ):
                                pending_bundle = active
                                current_stop.set()
                    current_stop.wait(self._maintenance_interval)

            with self._lock:
                stations = tuple(self._stations)
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
                    for station in stations
                ],
                *(
                    [
                        threading.Thread(
                            target=run,
                            args=(run_maintenance,),
                            name="edge-maintenance",
                            daemon=True,
                        )
                    ]
                    if self._reporters or self._configuration_sync is not None
                    else []
                ),
            ]
            for thread in threads:
                thread.start()
            while not stop_requested() and any(thread.is_alive() for thread in threads):
                sleep(0.05)
            cycle_stop.set()
            for station in stations:
                station.close()
            for thread in threads:
                thread.join()
            if errors:
                raise errors[0]
            if pending_bundle is not None:
                bundle, pending_bundle = pending_bundle, None
                self.apply_configuration(bundle)

        with self._lock:
            stations = tuple(self._stations)
        for station in stations:
            station.close()
        if self._media is not None:
            self._media.close()
        self._state.close()

    def close(self) -> None:
        """关闭输入和本地状态, 供配置失败和进程退出路径共同调用。"""
        with self._lock:
            stations = tuple(self._stations)
        for station in stations:
            station.close()
        if self._media is not None:
            self._media.close()
        self._state.close()


def _validate_media_for_runtime(
    *,
    config: EdgeRuntimeConfiguration,
    runtime_configuration: RuntimeConfiguration,
) -> None:
    if config.media is None:
        return
    if config.media.host_id != config.host_id:
        raise ValueError("media host_id must match edge host_id")
    confirmed_camera_ids = {
        camera.camera_id
        for station in (
            runtime_configuration.confirmed.stations
            if runtime_configuration.confirmed is not None
            else ()
        )
        for camera in station.cameras
    }
    validate_sop_camera_bindings(
        config.media,
        {station.configuration.station_id for station in runtime_configuration.stations},
        confirmed_camera_ids,
    )


def _build_runtime_composition(
    *,
    config: EdgeRuntimeConfiguration,
    state: LocalState,
    runtime_configuration: RuntimeConfiguration,
    report_transport: HttpDecisionReportTransport | None,
    connector_factory: ConnectorFactory,
) -> RuntimeComposition:
    """组合已确认拓扑、真实适配器、轮询运行时和持久账本。"""
    active_connectors = (
        runtime_configuration.connectors if runtime_configuration.confirmed is not None else ()
    )
    registry = ConfiguredLocalConnectorRegistry(
        active_connectors,
        adapter_factory=connector_factory,
    )
    adapters = registry.adapters
    points_by_connector: dict[str, list[InputPoint]] = {
        connector_id: [] for connector_id in adapters
    }
    connector_owners: dict[str, str] = {}
    for station in runtime_configuration.stations:
        station_id = station.configuration.station_id
        for connector_id in station.connector_ids:
            previous_owner = connector_owners.get(connector_id)
            if previous_owner is not None and previous_owner != station_id:
                raise ValueError(
                    f"connector {connector_id} is attached to multiple station runtimes"
                )
            connector_owners[connector_id] = station_id
        for connector_id, point in station.input_points:
            points = points_by_connector.setdefault(connector_id, [])
            if point not in points:
                points.append(point)
    connector_runtimes = ConnectorRuntimeSet(
        connectors=adapters,
        input_points={
            connector_id: tuple(points) for connector_id, points in points_by_connector.items()
        },
        timeout=config.command_timeout,
    )
    disposal_ledger = state.disposal()
    output_dispatchers = {
        connector_id: OutputDispatcher(
            connector=adapter,
            ledger=SQLiteWriteLedger(disposal_ledger),
            diagnostics=_log_write_attempt,
        )
        for connector_id, adapter in adapters.items()
    }
    reporters: list[DecisionReporter] = []
    stations: list[AutonomousStation] = []
    try:
        for station_binding in runtime_configuration.stations:
            station_config = station_binding.configuration
            station_store = state.station(station_config.station_id)
            sources = tuple(
                SseStationInputSource(
                    inference_url=station_configuration.inference_url,
                    request_body=station_configuration.request_body,
                    timeout=config.command_timeout,
                )
                for station_configuration in (station_binding.configurations or (station_config,))
            )
            source: StationInputSource = (
                sources[0] if len(sources) == 1 else MultiplexedStationInputSource(sources=sources)
            )
            runtimes = tuple(
                connector_runtimes.runtime(connector_id)
                for connector_id in station_binding.connector_ids
            )
            station_dispatchers = {
                connector_id: output_dispatchers[connector_id]
                for connector_id in station_binding.connector_ids
                if connector_id in output_dispatchers
            }
            if station_config.backend_id is not None and report_transport is not None:
                reporters.append(
                    DecisionReporter(
                        queues=station_store,
                        context=ReportContext(
                            host_id=config.host_id,
                            station_id=station_config.station_id,
                            backend_id=station_config.backend_id,
                            template_version_id=station_config.template_version_id,
                            template_sha256=station_config.template_sha256,
                            model_ids=station_config.model_ids,
                        ),
                        transport=report_transport,
                    )
                )
            stations.append(
                AutonomousStation(
                    station_id=station_config.station_id,
                    supervisor=resume_station(
                        station_store,
                        template=station_config.template,
                        parameters=station_config.parameters,
                        margins=station_config.margins,
                    ),
                    source=source,
                    connector_runtimes=runtimes,
                    output_dispatchers=station_dispatchers,
                    output_points={
                        connector_id: station_binding.output_points_for(connector_id)
                        for connector_id in station_binding.connector_ids
                    },
                    diagnostics=_log_write_attempt,
                )
            )
    except Exception:
        for built_station in stations:
            built_station.close()
        raise
    return RuntimeComposition(
        configuration=runtime_configuration,
        stations=tuple(stations),
        connector_runtimes=connector_runtimes,
        output_dispatchers=output_dispatchers,
        reporters=tuple(reporters),
    )


def build_autonomous_runtime_from_file(
    config_path: str | Path,
    *,
    connector_factory: ConnectorFactory | None = None,
) -> AutonomousRuntime:
    """从已确认的本地配置、真实连接器和 SQLite 状态装配自治运行时。"""
    config = load_autonomous_configuration(config_path)
    resolved_connector_factory = connector_factory or build_isapi_connector
    if config.local_state_path is None:
        raise ValueError("local_state_path is required for autonomous runtime")
    state = open_local_state(str(config.local_state_path))
    composition: RuntimeComposition | None = None
    try:
        confirmed = state.configuration().confirmed()
        if confirmed is not None and confirmed.host_id != config.host_id:
            raise ValueError("confirmed configuration belongs to another inference host")
        active_configuration = (
            bootstrap_runtime_configuration(
                stations=config.stations,
                connectors=config.connectors,
            )
            if confirmed is None
            else confirmed_runtime_configuration(
                bundle=confirmed,
                bootstrap_stations=config.stations,
                local_connectors=config.connectors,
            )
        )
        _validate_media_for_runtime(
            config=config,
            runtime_configuration=active_configuration,
        )
        report_transport = HttpDecisionReportTransport(
            center_url=config.center_url,
            host_id=config.host_id,
            host_private_key=config.host_private_key,
            timeout=config.command_timeout,
            ssl_context=config.ssl_context,
        )
        composition = _build_runtime_composition(
            config=config,
            state=state,
            runtime_configuration=active_configuration,
            report_transport=report_transport,
            connector_factory=resolved_connector_factory,
        )
        command_loop = build_connection_test_loop_from_configuration(
            config,
            adapter_factory=resolved_connector_factory,
        )

        def compose_confirmed(bundle: ConfigurationBundle) -> RuntimeComposition:
            runtime_configuration = confirmed_runtime_configuration(
                bundle=bundle,
                bootstrap_stations=config.stations,
                local_connectors=config.connectors,
            )
            _validate_media_for_runtime(
                config=config,
                runtime_configuration=runtime_configuration,
            )
            return _build_runtime_composition(
                config=config,
                state=state,
                runtime_configuration=runtime_configuration,
                report_transport=report_transport,
                connector_factory=resolved_connector_factory,
            )

        def validate_confirmed(bundle: ConfigurationBundle) -> None:
            runtime_configuration = confirmed_runtime_configuration(
                bundle=bundle,
                bootstrap_stations=config.stations,
                local_connectors=config.connectors,
            )
            _validate_media_for_runtime(
                config=config,
                runtime_configuration=runtime_configuration,
            )

        configuration_sync = ConfigurationSynchronizer(
            puller=HttpConfigurationPuller(
                center_url=config.center_url,
                host_id=config.host_id,
                host_private_key=config.host_private_key,
                timeout=config.command_timeout,
                ssl_context=config.ssl_context,
            ),
            store=state.configuration(),
            expected_host_id=config.host_id,
            validator=validate_confirmed,
        )
        return AutonomousRuntime(
            command_loop=command_loop,
            stations=composition.stations,
            state=state,
            media=MediaRuntime(config.media) if config.media is not None else None,
            reporters=composition.reporters,
            configuration_sync=configuration_sync,
            maintenance_interval=config.command_poll_interval,
            configuration=composition.configuration,
            configuration_factory=compose_confirmed,
            connector_runtimes=composition.connector_runtimes,
            output_dispatchers=composition.output_dispatchers,
        )
    except Exception:
        if composition is not None:
            for station in composition.stations:
                station.close()
        state.close()
        raise


def _station_input_source(binding: StationRuntimeBinding, *, timeout: float) -> StationInputSource:
    """按工位的一个或多个 backend 输入配置构造输入接缝。"""
    source_configurations = binding.configurations or (binding.configuration,)
    sources = tuple(
        SseStationInputSource(
            inference_url=source_configuration.inference_url,
            request_body=source_configuration.request_body,
            timeout=timeout,
        )
        for source_configuration in source_configurations
    )
    return sources[0] if len(sources) == 1 else MultiplexedStationInputSource(sources=sources)


def _synchronize_runtime_configuration(
    config: EdgeRuntimeConfiguration, state: LocalState
) -> RuntimeConfiguration:
    """主动拉取一次; 失败时使用最后确认 bundle, 首次失败才使用 bootstrap。"""
    synchronizer = ConfigurationSynchronizer(
        puller=HttpConfigurationPuller(
            center_url=config.center_url,
            host_id=config.host_id,
            host_private_key=config.host_private_key,
            timeout=config.command_timeout,
            ssl_context=config.ssl_context,
        ),
        store=state.configuration(),
        expected_host_id=config.host_id,
        validator=lambda bundle: validate_confirmed_runtime_configuration(
            bundle=bundle,
            bootstrap_stations=config.stations,
            local_connectors=config.connectors,
        ),
    )
    result = synchronizer.synchronize(observed_at=time())
    if result.active is None:
        return bootstrap_runtime_configuration(
            stations=config.stations,
            connectors=config.connectors,
        )
    return confirmed_runtime_configuration(
        bundle=result.active,
        bootstrap_stations=config.stations,
        local_connectors=config.connectors,
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
    "ConnectorFactory",
    "InputWaitExpired",
    "LocalIsapiConnectorConfiguration",
    "RuntimeComposition",
    "SseStationInputSource",
    "StationInputSource",
    "StationRuntimeConfiguration",
    "build_autonomous_runtime_from_file",
    "build_connection_test_loop",
    "build_connection_test_loop_from_file",
    "build_connection_test_runner",
    "main",
]
