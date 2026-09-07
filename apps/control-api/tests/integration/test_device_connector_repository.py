"""连接器配置与真实 PostgreSQL 迁移、事务及拓扑触发器。"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, delete, inspect, text
from sqlalchemy.orm import Session as DatabaseSession

from factory_sop.auth.api import Caller, Permission
from factory_sop.auth.model import User, UserStatus
from factory_sop.device.adapters.repository import (
    PostgresCameraRepository,
    PostgresConnectorRepository,
    PostgresInferenceBackendRepository,
    PostgresInferenceHostRepository,
    PostgresStationRepository,
)
from factory_sop.device.adapters.tables import (
    CameraRow,
    ConnectorRow,
    InferenceBackendRow,
    InferenceHostRow,
    StationRow,
)
from factory_sop.device.errors import DeviceRefusalCode, DeviceRefusedError
from factory_sop.device.model import (
    Camera,
    ConnectionState,
    Connector,
    ConnectorConfiguration,
    ConnectorReachability,
    ConnectorType,
    DeviceStatus,
    InferenceBackend,
    InferenceHost,
    Station,
)
from factory_sop.device.usecases.connectors import (
    create_connector,
    delete_connector,
    edit_connector,
    set_connector_status,
)
from factory_sop.device.usecases.hosts import delete_host
from factory_sop.device.usecases.stations import delete_station
from factory_sop.identifiers import new_id
from factory_sop.persistence import Table as DeclarativeTable

NOW = datetime(2026, 9, 7, 1, 0, tzinfo=UTC)
CONTROL_API = Path(__file__).resolve().parents[2]
CONNECTOR_PERMISSIONS = {
    "device.connector.view",
    "device.connector.edit",
    "device.connector.delete",
}


def _wait_until_blocked_by(engine: Engine, *, waiter_pid: int, blocker_pid: int) -> None:
    """证明两个事务真实重叠：等待方必须在 PostgreSQL 锁表中被指定事务阻塞。"""
    with engine.connect() as observer:
        deadline = time.monotonic() + 5
        last_row: object = None
        while time.monotonic() < deadline:
            row = observer.execute(
                text(
                    """
                    SELECT activity.wait_event_type,
                           pg_blocking_pids(activity.pid),
                           EXISTS (
                               SELECT 1
                                 FROM pg_locks AS waiting
                                WHERE waiting.pid = activity.pid
                                  AND NOT waiting.granted
                           )
                      FROM pg_stat_activity AS activity
                     WHERE activity.pid = :waiter_pid
                    """
                ),
                {"waiter_pid": waiter_pid},
            ).one_or_none()
            last_row = row
            if row is not None and row[0] == "Lock" and blocker_pid in row[1] and row[2]:
                return
            time.sleep(0.01)
    pytest.fail(
        f"pid {waiter_pid} did not wait on a PostgreSQL lock held by pid {blocker_pid}; "
        f"last_observation={last_row}"
    )


def a_host(*, name: str = "推理机-1", status: DeviceStatus = DeviceStatus.ACTIVE) -> InferenceHost:
    return InferenceHost(
        id=new_id(),
        name=name,
        address="10.0.8.11",
        mediamtx_address=None,
        recording_window_seconds=604800,
        disk_watermark_percent=85,
        status=status,
        revision=1,
        created_by=new_id(),
        updated_by=new_id(),
        created_at=NOW,
        updated_at=NOW,
    )


def a_station(*, code: str = "A-001", status: DeviceStatus = DeviceStatus.ACTIVE) -> Station:
    return Station(
        id=new_id(),
        code=code,
        name="装配一号工位",
        tags=(),
        status=status,
        revision=1,
        created_by=new_id(),
        updated_by=new_id(),
        created_at=NOW,
        updated_at=NOW,
    )


def a_connector(
    station_id: UUID,
    host_id: UUID,
    *,
    name: str = "一号连接器",
    revision: int = 1,
) -> Connector:
    return Connector(
        id=new_id(),
        station_id=station_id,
        host_id=host_id,
        name=name,
        connector_type=ConnectorType.HIKVISION_ISAPI,
        configuration=ConnectorConfiguration(address="10.0.8.21", port=80),
        credentials_configured=False,
        reachability=ConnectorReachability.UNVERIFIED,
        health_detail=None,
        status=DeviceStatus.ACTIVE,
        revision=revision,
        created_by=new_id(),
        updated_by=new_id(),
        created_at=NOW,
        updated_at=NOW,
    )


def a_backend(host_id: UUID) -> InferenceBackend:
    return InferenceBackend(
        id=new_id(),
        host_id=host_id,
        base_url="http://10.0.8.11:8000",
        template_version_id=None,
        status=DeviceStatus.ACTIVE,
        connection_state=ConnectionState.UNVERIFIED,
        connection_checked_at=None,
        connection_detail=None,
        self_reported_model_ids=(),
        self_reported_at=None,
        revision=1,
        created_by=new_id(),
        updated_by=new_id(),
        created_at=NOW,
        updated_at=NOW,
    )


def a_camera(station_id: UUID, host_id: UUID, backend_id: UUID) -> Camera:
    return Camera(
        id=new_id(),
        name="一号相机",
        address="10.0.8.21",
        main_stream_path="/Streaming/Channels/101",
        sub_stream_path="/Streaming/Channels/102",
        credentials_configured=False,
        station_id=station_id,
        host_id=host_id,
        backend_id=backend_id,
        status=DeviceStatus.ACTIVE,
        revision=1,
        created_by=new_id(),
        updated_by=new_id(),
        created_at=NOW,
        updated_at=NOW,
    )


def test_connector_crud_commits_and_a_new_session_reloads_it(engine: Engine) -> None:
    actor_id = new_id()
    actor = Caller(
        user=User(
            id=actor_id,
            login_name="device-admin",
            display_name="设备管理员",
            password_hash="not-used",  # pragma: allowlist secret
            status=UserStatus.ACTIVE,
        ),
        granted=frozenset(
            {
                Permission.CONNECTOR_VIEW,
                Permission.CONNECTOR_EDIT,
                Permission.CONNECTOR_DELETE,
                Permission.STATION_DELETE,
                Permission.INFERENCE_HOST_DELETE,
            }
        ),
    )
    station = a_station()
    host = a_host()
    connector_id: UUID | None = None
    try:
        with DatabaseSession(engine) as writing:
            stations = PostgresStationRepository(writing)
            hosts = PostgresInferenceHostRepository(writing)
            connectors = PostgresConnectorRepository(writing)
            cameras = PostgresCameraRepository(writing)
            backends = PostgresInferenceBackendRepository(writing)
            stations.add(station)
            hosts.add(host)
            writing.flush()

            created = create_connector(
                station_id=station.id,
                host_id=host.id,
                name="一号连接器",
                connector_type=ConnectorType.HIKVISION_ISAPI,
                configuration={"address": "10.0.8.21", "port": 80},
                caller=actor,
                now=NOW,
                stations=stations,
                hosts=hosts,
                cameras=cameras,
                connectors=connectors,
            )
            connector_id = created.id
            assert created.reachability is ConnectorReachability.UNVERIFIED

            with pytest.raises(DeviceRefusedError) as station_refused:
                delete_station(
                    station_id=station.id,
                    expected_revision=station.revision,
                    caller=actor,
                    stations=stations,
                    cameras=cameras,
                    connectors=connectors,
                )
            assert station_refused.value.code is DeviceRefusalCode.STATION_HAS_CONNECTORS

            with pytest.raises(DeviceRefusedError) as host_refused:
                delete_host(
                    host_id=host.id,
                    expected_revision=host.revision,
                    caller=actor,
                    hosts=hosts,
                    backends=backends,
                    connectors=connectors,
                )
            assert host_refused.value.code is DeviceRefusalCode.INFERENCE_HOST_HAS_CONNECTORS

            edited = edit_connector(
                connector_id=created.id,
                station_id=station.id,
                host_id=host.id,
                name="板卡连接器",
                connector_type=ConnectorType.BOARD_CARD,
                configuration={"address": "/dev/board0"},
                expected_revision=created.revision,
                caller=actor,
                now=NOW,
                stations=stations,
                hosts=hosts,
                connectors=connectors,
                cameras=cameras,
            )
            deactivated = set_connector_status(
                connector_id=edited.id,
                requested_status=DeviceStatus.DEACTIVATED,
                expected_revision=edited.revision,
                caller=actor,
                now=NOW,
                connectors=connectors,
            )
            restored = set_connector_status(
                connector_id=deactivated.id,
                requested_status=DeviceStatus.ACTIVE,
                expected_revision=deactivated.revision,
                caller=actor,
                now=NOW,
                connectors=connectors,
            )
            assert restored.revision == 4
            writing.commit()

        assert connector_id is not None
        with DatabaseSession(engine) as reading:
            reloaded = PostgresConnectorRepository(reading).by_id(connector_id)
            assert reloaded == restored
            assert reloaded is not None
            assert reloaded.configuration == ConnectorConfiguration(address="/dev/board0")

        with DatabaseSession(engine) as deleting:
            stations = PostgresStationRepository(deleting)
            hosts = PostgresInferenceHostRepository(deleting)
            connectors = PostgresConnectorRepository(deleting)
            backends = PostgresInferenceBackendRepository(deleting)
            delete_connector(
                connector_id=connector_id,
                expected_revision=restored.revision,
                caller=actor,
                connectors=connectors,
            )
            delete_station(
                station_id=station.id,
                expected_revision=station.revision,
                caller=actor,
                stations=stations,
                cameras=PostgresCameraRepository(deleting),
                connectors=connectors,
            )
            delete_host(
                host_id=host.id,
                expected_revision=host.revision,
                caller=actor,
                hosts=hosts,
                backends=backends,
                connectors=connectors,
            )
            deleting.commit()

        with DatabaseSession(engine) as reading:
            assert PostgresConnectorRepository(reading).by_id(connector_id) is None
            assert PostgresStationRepository(reading).by_id(station.id) is None
            assert PostgresInferenceHostRepository(reading).by_id(host.id) is None
    finally:
        with engine.begin() as cleanup:
            tables = DeclarativeTable.metadata.tables
            cleanup.execute(
                tables["device_connector"]
                .delete()
                .where(tables["device_connector"].c.id == connector_id)
            )
            cleanup.execute(
                tables["device_camera"]
                .delete()
                .where(tables["device_camera"].c.station_id == station.id)
            )
            cleanup.execute(
                tables["device_inference_backend"]
                .delete()
                .where(tables["device_inference_backend"].c.host_id == host.id)
            )
            cleanup.execute(
                tables["device_station"].delete().where(tables["device_station"].c.id == station.id)
            )
            cleanup.execute(
                tables["device_inference_host"]
                .delete()
                .where(tables["device_inference_host"].c.id == host.id)
            )


def test_connector_triggers_reject_deactivated_parents_and_cross_host_topology(
    session: DatabaseSession,
) -> None:
    stations = PostgresStationRepository(session)
    hosts = PostgresInferenceHostRepository(session)
    connectors = PostgresConnectorRepository(session)

    deactivated_station = a_station(status=DeviceStatus.DEACTIVATED)
    active_host = a_host()
    stations.add(deactivated_station)
    hosts.add(active_host)
    with session.begin_nested(), pytest.raises(DeviceRefusedError) as station_refused:
        connectors.add(a_connector(deactivated_station.id, active_host.id))
    assert station_refused.value.code is DeviceRefusalCode.STATION_DEACTIVATED

    active_station = a_station(code="A-002")
    deactivated_host = a_host(name="推理机-2", status=DeviceStatus.DEACTIVATED)
    stations.add(active_station)
    hosts.add(deactivated_host)
    with session.begin_nested(), pytest.raises(DeviceRefusedError) as host_refused:
        connectors.add(a_connector(active_station.id, deactivated_host.id))
    assert host_refused.value.code is DeviceRefusalCode.INFERENCE_HOST_DEACTIVATED

    host_a = a_host(name="推理机-3")
    host_b = a_host(name="推理机-4")
    station = a_station(code="A-003")
    stations.add(station)
    hosts.add(host_a)
    hosts.add(host_b)
    first = a_connector(station.id, host_a.id)
    connectors.add(first)
    session.flush()

    with session.begin_nested(), pytest.raises(DeviceRefusedError) as conflict:
        connectors.add(a_connector(station.id, host_b.id, name="二号连接器"))
    assert conflict.value.code is DeviceRefusalCode.CONNECTOR_STATION_HOST_CONFLICT


def test_camera_trigger_rejects_cross_host_binding_to_an_existing_connector(
    session: DatabaseSession,
) -> None:
    stations = PostgresStationRepository(session)
    hosts = PostgresInferenceHostRepository(session)
    backends = PostgresInferenceBackendRepository(session)
    connectors = PostgresConnectorRepository(session)
    cameras = PostgresCameraRepository(session)
    station = a_station()
    host_a = a_host()
    host_b = a_host(name="推理机-2")
    backend_b = a_backend(host_b.id)
    stations.add(station)
    hosts.add(host_a)
    hosts.add(host_b)
    backends.add(backend_b)
    connectors.add(a_connector(station.id, host_a.id))
    session.flush()

    with session.begin_nested(), pytest.raises(DeviceRefusedError) as refused:
        cameras.add(a_camera(station.id, host_b.id, backend_b.id))
    assert refused.value.code is DeviceRefusalCode.CONNECTOR_STATION_HOST_CONFLICT


@pytest.fixture
def migration_database(engine: Engine) -> Iterator[Engine]:
    """在独立数据库中完整走一遍连接器迁移的升级与回退。"""
    installer = create_engine(
        engine.url._replace(database="postgres"), isolation_level="AUTOCOMMIT"
    )
    database = f"nvsop_connector_migration_{uuid4().hex[:12]}"
    with installer.connect() as connection:
        connection.exec_driver_sql(f'DROP DATABASE IF EXISTS "{database}"')
        connection.exec_driver_sql(f'CREATE DATABASE "{database}"')
    installer.dispose()

    migrated = create_engine(engine.url._replace(database=database))
    configuration = Config(str(CONTROL_API / "alembic.ini"))
    configuration.set_main_option("script_location", str(CONTROL_API / "migrations"))
    configuration.set_main_option(
        "sqlalchemy.url", migrated.url.render_as_string(hide_password=False)
    )
    try:
        yield migrated
    finally:
        migrated.dispose()
        with create_engine(
            engine.url._replace(database="postgres"), isolation_level="AUTOCOMMIT"
        ).connect() as connection:
            connection.exec_driver_sql(f'DROP DATABASE "{database}"')


def test_connector_permission_migration_can_upgrade_downgrade_and_reload(
    migration_database: Engine,
) -> None:
    configuration = Config(str(CONTROL_API / "alembic.ini"))
    configuration.set_main_option("script_location", str(CONTROL_API / "migrations"))
    configuration.set_main_option(
        "sqlalchemy.url", migration_database.url.render_as_string(hide_password=False)
    )

    command.upgrade(configuration, "0008")
    with migration_database.connect() as connection:
        assert not inspect(connection).has_table("device_connector")
        before = set(connection.execute(text("SELECT code FROM auth_permission")).scalars())
        assert not CONNECTOR_PERMISSIONS & before

    command.upgrade(configuration, "head")
    with migration_database.connect() as connection:
        permissions = set(connection.execute(text("SELECT code FROM auth_permission")).scalars())
        assert permissions >= CONNECTOR_PERMISSIONS
        assert inspect(connection).has_table("device_connector")

    command.downgrade(configuration, "0009")
    with migration_database.connect() as connection:
        permissions = set(connection.execute(text("SELECT code FROM auth_permission")).scalars())
        assert not CONNECTOR_PERMISSIONS & permissions
        assert inspect(connection).has_table("device_connector")

    command.upgrade(configuration, "head")
    with migration_database.connect() as connection:
        permissions = set(connection.execute(text("SELECT code FROM auth_permission")).scalars())
        assert permissions >= CONNECTOR_PERMISSIONS


def test_device_migration_0009_rollback_reupgrade_restores_prior_topology_behavior(
    migration_database: Engine,
) -> None:
    configuration = Config(str(CONTROL_API / "alembic.ini"))
    configuration.set_main_option("script_location", str(CONTROL_API / "migrations"))
    configuration.set_main_option(
        "sqlalchemy.url", migration_database.url.render_as_string(hide_password=False)
    )
    station = a_station(code="A-009")
    host = a_host(name="推理机-9")
    backend = a_backend(host.id)
    camera = a_camera(station.id, host.id, backend.id)

    command.upgrade(configuration, "0009")
    with DatabaseSession(migration_database) as writing:
        PostgresStationRepository(writing).add(station)
        PostgresInferenceHostRepository(writing).add(host)
        PostgresInferenceBackendRepository(writing).add(backend)
        PostgresCameraRepository(writing).add(camera)
        writing.commit()

    command.downgrade(configuration, "0008")
    with migration_database.connect() as connection:
        inspector = inspect(connection)
        assert not inspector.has_table("device_connector")
        assert inspector.has_table("device_station")
        assert inspector.has_table("device_camera")
        assert inspector.has_table("device_inference_backend")
        triggers = set(
            connection.execute(
                text(
                    """
                    SELECT event_object_table, trigger_name
                      FROM information_schema.triggers
                     WHERE event_object_schema = current_schema()
                       AND event_object_table IN (
                           'device_camera', 'device_inference_backend', 'device_connector'
                       )
                    """
                )
            ).all()
        )
        assert ("device_camera", "device_camera_topology_must_be_consistent") in triggers
        assert (
            "device_inference_backend",
            "device_backend_topology_must_be_consistent",
        ) in triggers
        assert not any(table == "device_connector" for table, _ in triggers)

    command.upgrade(configuration, "0009")
    with migration_database.connect() as connection:
        inspector = inspect(connection)
        assert inspector.has_table("device_connector")
        assert inspector.has_table("device_station")
        assert inspector.has_table("device_camera")
        assert inspector.has_table("device_inference_backend")
        triggers = set(
            connection.execute(
                text(
                    """
                    SELECT event_object_table, trigger_name
                      FROM information_schema.triggers
                     WHERE event_object_schema = current_schema()
                       AND event_object_table IN (
                           'device_camera', 'device_inference_backend', 'device_connector'
                       )
                    """
                )
            ).all()
        )
        assert {
            ("device_camera", "device_camera_topology_must_be_consistent"),
            ("device_camera", "device_camera_station_topology_must_be_consistent"),
            ("device_camera", "device_camera_connector_topology_must_be_consistent"),
            ("device_inference_backend", "device_backend_topology_must_be_consistent"),
            ("device_connector", "device_connector_topology_must_be_consistent"),
        } <= triggers

    with DatabaseSession(migration_database) as restored:
        stations = PostgresStationRepository(restored)
        hosts = PostgresInferenceHostRepository(restored)
        backends = PostgresInferenceBackendRepository(restored)
        cameras = PostgresCameraRepository(restored)
        assert stations.by_id(station.id) == station
        assert hosts.by_id(host.id) == host
        loaded_backend = backends.by_id(backend.id)
        loaded_camera = cameras.by_id(camera.id)
        assert loaded_backend == backend
        assert loaded_camera == camera
        assert loaded_backend is not None
        assert loaded_camera is not None

        updated_backend = replace(
            loaded_backend,
            base_url="http://10.0.8.11:8001",
            revision=loaded_backend.revision + 1,
        )
        updated_camera = replace(
            loaded_camera,
            main_stream_path="/Streaming/Channels/201",
            sub_stream_path="/Streaming/Channels/202",
            revision=loaded_camera.revision + 1,
        )
        backends.save(updated_backend, expected_revision=loaded_backend.revision)
        cameras.save(updated_camera, expected_revision=loaded_camera.revision)
        restored.commit()

    with DatabaseSession(migration_database) as reading:
        assert PostgresInferenceBackendRepository(reading).by_id(backend.id) == updated_backend
        assert PostgresCameraRepository(reading).by_id(camera.id) == updated_camera


@pytest.mark.parametrize(
    ("cross_host", "expected_error"),
    [
        (True, DeviceRefusalCode.CONNECTOR_STATION_HOST_CONFLICT),
        (False, None),
    ],
    ids=["cross-host-conflict", "same-host-valid"],
)
def test_concurrent_connector_and_camera_writes_preserve_station_host_topology(
    engine: Engine,
    cross_host: bool,
    expected_error: DeviceRefusalCode | None,
) -> None:
    station = a_station(code="A-004")
    host_a = a_host(name="推理机-5")
    host_b = a_host(name="推理机-6") if cross_host else host_a
    backend_b = a_backend(host_b.id)
    connector = a_connector(station.id, host_a.id)
    camera = a_camera(station.id, host_b.id, backend_b.id)
    setup = DatabaseSession(engine)
    connector_flushed = threading.Event()
    connector_commit = threading.Event()
    camera_pid_ready = threading.Event()
    outcomes: dict[str, tuple[str, BaseException | None]] = {}
    pids: dict[str, int] = {}
    outcomes_lock = threading.Lock()

    try:
        stations = PostgresStationRepository(setup)
        hosts = PostgresInferenceHostRepository(setup)
        backends = PostgresInferenceBackendRepository(setup)
        stations.add(station)
        hosts.add(host_a)
        if host_b.id != host_a.id:
            hosts.add(host_b)
        backends.add(backend_b)
        setup.commit()

        def insert_connector() -> None:
            local = DatabaseSession(engine)
            try:
                pids["connector"] = local.scalar(text("SELECT pg_backend_pid()"))
                PostgresConnectorRepository(local).add(connector)
                connector_flushed.set()
                connector_commit.wait(10)
                local.commit()
                with outcomes_lock:
                    outcomes["connector"] = ("committed", None)
            except BaseException as error:
                local.rollback()
                with outcomes_lock:
                    outcomes["connector"] = ("error", error)
            finally:
                local.close()

        def insert_camera() -> None:
            local = DatabaseSession(engine)
            try:
                pids["camera"] = local.scalar(text("SELECT pg_backend_pid()"))
                camera_pid_ready.set()
                PostgresCameraRepository(local).add(camera)
                local.commit()
                with outcomes_lock:
                    outcomes["camera"] = ("committed", None)
            except BaseException as error:
                local.rollback()
                with outcomes_lock:
                    outcomes["camera"] = ("error", error)
            finally:
                local.close()

        connector_thread = threading.Thread(target=insert_connector)
        camera_thread = threading.Thread(target=insert_camera)
        connector_thread.start()
        assert connector_flushed.wait(10)
        camera_thread.start()
        assert camera_pid_ready.wait(10)

        camera_pid = pids["camera"]
        connector_pid = pids["connector"]
        with engine.connect() as observer:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                blocked_by_connector = observer.scalar(
                    text(
                        """
                        SELECT wait_event_type = 'Lock'
                           AND :connector_pid = ANY(pg_blocking_pids(pid))
                          FROM pg_stat_activity
                         WHERE pid = :camera_pid
                        """
                    ),
                    {"camera_pid": camera_pid, "connector_pid": connector_pid},
                )
                if blocked_by_connector:
                    break
                time.sleep(0.01)
            else:
                pytest.fail(
                    "camera write did not overlap while waiting on the connector lock: "
                    f"outcomes={outcomes}, pids={pids}"
                )

        connector_commit.set()
        connector_thread.join(10)
        camera_thread.join(10)
        assert not connector_thread.is_alive()
        assert not camera_thread.is_alive()

        assert outcomes["connector"] == ("committed", None)
        if expected_error is None:
            assert outcomes["camera"] == ("committed", None)
        else:
            camera_error = outcomes["camera"][1]
            assert isinstance(camera_error, DeviceRefusedError)
            assert camera_error.code is expected_error
    finally:
        connector_commit.set()
        if "connector_thread" in locals():
            connector_thread.join(10)
        if "camera_thread" in locals():
            camera_thread.join(10)
        setup.close()
        with engine.begin() as connection:
            connection.execute(delete(CameraRow).where(CameraRow.id == camera.id))
            connection.execute(delete(ConnectorRow).where(ConnectorRow.id == connector.id))
            connection.execute(
                delete(InferenceBackendRow).where(InferenceBackendRow.id == backend_b.id)
            )
            connection.execute(
                delete(InferenceHostRow).where(InferenceHostRow.id.in_([host_a.id, host_b.id]))
            )
            connection.execute(delete(StationRow).where(StationRow.id == station.id))


def test_backend_mutation_and_camera_change_follow_the_existing_lock_order(
    engine: Engine,
) -> None:
    station_a = a_station(code="A-005")
    station_b = a_station(code="A-006")
    host_a = a_host(name="推理机-7")
    host_b = a_host(name="推理机-8")
    backend_a = a_backend(host_a.id)
    backend_b = a_backend(host_b.id)
    camera_a = a_camera(station_a.id, host_a.id, backend_a.id)
    camera_b = a_camera(station_b.id, host_b.id, backend_b.id)
    setup = DatabaseSession(engine)
    station_gate = DatabaseSession(engine)
    gate_pid: int | None = None
    camera_pid_ready = threading.Event()
    backend_pid_ready = threading.Event()
    outcomes: dict[str, tuple[str, BaseException | None]] = {}
    pids: dict[str, int] = {}
    outcomes_lock = threading.Lock()

    try:
        stations = PostgresStationRepository(setup)
        hosts = PostgresInferenceHostRepository(setup)
        backends = PostgresInferenceBackendRepository(setup)
        cameras = PostgresCameraRepository(setup)
        stations.add(station_a)
        stations.add(station_b)
        hosts.add(host_a)
        hosts.add(host_b)
        backends.add(backend_a)
        backends.add(backend_b)
        cameras.add(camera_a)
        cameras.add(camera_b)
        setup.commit()

        gate_pid = station_gate.scalar(text("SELECT pg_backend_pid()"))
        station_gate.execute(
            text("SELECT id FROM device_station WHERE id = :station_id FOR UPDATE"),
            {"station_id": station_b.id},
        )

        def move_camera() -> None:
            local = DatabaseSession(engine)
            try:
                local.execute(text("SET deadlock_timeout = '100ms'"))
                pids["camera"] = local.scalar(text("SELECT pg_backend_pid()"))
                camera_pid_ready.set()
                cameras_local = PostgresCameraRepository(local)
                cameras_local.save(
                    replace(
                        camera_a,
                        station_id=station_b.id,
                        host_id=host_b.id,
                        backend_id=backend_b.id,
                        revision=2,
                    ),
                    expected_revision=camera_a.revision,
                )
                local.commit()
                with outcomes_lock:
                    outcomes["camera"] = ("committed", None)
            except BaseException as error:
                local.rollback()
                with outcomes_lock:
                    outcomes["camera"] = ("error", error)
            finally:
                local.close()

        def change_backend() -> None:
            local = DatabaseSession(engine)
            try:
                local.execute(text("SET deadlock_timeout = '100ms'"))
                pids["backend"] = local.scalar(text("SELECT pg_backend_pid()"))
                backend_pid_ready.set()
                PostgresInferenceBackendRepository(local).save(
                    replace(backend_b, template_version_id=new_id(), revision=2),
                    expected_revision=backend_b.revision,
                )
                local.commit()
                with outcomes_lock:
                    outcomes["backend"] = ("committed", None)
            except BaseException as error:
                local.rollback()
                with outcomes_lock:
                    outcomes["backend"] = ("error", error)
            finally:
                local.close()

        camera_thread = threading.Thread(target=move_camera)
        backend_thread = threading.Thread(target=change_backend)
        camera_thread.start()
        assert camera_pid_ready.wait(10)
        _wait_until_blocked_by(engine, waiter_pid=pids["camera"], blocker_pid=gate_pid)
        backend_thread.start()
        assert backend_pid_ready.wait(10)
        _wait_until_blocked_by(engine, waiter_pid=pids["backend"], blocker_pid=pids["camera"])
        station_gate.rollback()
        camera_thread.join(10)
        backend_thread.join(10)
        assert not camera_thread.is_alive()
        assert not backend_thread.is_alive()

        assert outcomes == {"camera": ("committed", None), "backend": ("committed", None)}
    finally:
        station_gate.rollback()
        if "camera_thread" in locals():
            camera_thread.join(10)
        if "backend_thread" in locals():
            backend_thread.join(10)
        station_gate.close()
        setup.close()
        with engine.begin() as connection:
            connection.execute(
                delete(CameraRow).where(CameraRow.id.in_([camera_a.id, camera_b.id]))
            )
            connection.execute(
                delete(InferenceBackendRow).where(
                    InferenceBackendRow.id.in_([backend_a.id, backend_b.id])
                )
            )
            connection.execute(
                delete(InferenceHostRow).where(InferenceHostRow.id.in_([host_a.id, host_b.id]))
            )
            connection.execute(
                delete(StationRow).where(StationRow.id.in_([station_a.id, station_b.id]))
            )
