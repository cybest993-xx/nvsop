"""点位、能力声明及其 PostgreSQL 并发拓扑约束。"""

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
    PostgresConnectorRepository,
    PostgresInferenceHostRepository,
    PostgresPointRepository,
    PostgresStationRepository,
)
from factory_sop.device.adapters.tables import ConnectorRow, InferenceHostRow, PointRow, StationRow
from factory_sop.device.errors import DeviceRefusalCode, DeviceRefusedError
from factory_sop.device.model import (
    Connector,
    ConnectorConfiguration,
    ConnectorReachability,
    ConnectorType,
    DeviceStatus,
    InferenceHost,
    Point,
    PointDirection,
    Station,
)
from factory_sop.device.usecases.connectors import update_connector_capability
from factory_sop.device.usecases.points import (
    create_point,
    edit_point,
    set_point_status,
)
from factory_sop.identifiers import new_id
from nvsop_contracts import (
    EdgePreservation,
    Measured,
    Polled,
    Sequencing,
    TimestampSource,
    Unverified,
)

NOW = datetime(2026, 9, 8, 1, 0, tzinfo=UTC)
CONTROL_API = Path(__file__).resolve().parents[2]
POINT_PERMISSIONS = {
    "device.point.view",
    "device.point.edit",
    "device.point.delete",
}


def actor() -> Caller:
    user_id = new_id()
    return Caller(
        user=User(
            id=user_id,
            login_name="point-admin",
            display_name="点位管理员",
            password_hash="unused",  # pragma: allowlist secret
            status=UserStatus.ACTIVE,
        ),
        granted=frozenset(
            {
                Permission.POINT_VIEW,
                Permission.POINT_EDIT,
                Permission.POINT_DELETE,
                Permission.CONNECTOR_EDIT,
            }
        ),
    )


def a_station(*, code: str = "A-101", status: DeviceStatus = DeviceStatus.ACTIVE) -> Station:
    return Station(
        id=new_id(),
        code=code,
        name=code,
        tags=(),
        status=status,
        revision=1,
        created_by=new_id(),
        updated_by=new_id(),
        created_at=NOW,
        updated_at=NOW,
    )


def a_host(*, name: str = "点位推理机") -> InferenceHost:
    return InferenceHost(
        id=new_id(),
        name=name,
        address="10.2.0.11",
        mediamtx_address=None,
        recording_window_seconds=604800,
        disk_watermark_percent=85,
        status=DeviceStatus.ACTIVE,
        revision=1,
        created_by=new_id(),
        updated_by=new_id(),
        created_at=NOW,
        updated_at=NOW,
    )


def a_connector(station_id: UUID, host_id: UUID, *, name: str = "点位连接器") -> Connector:
    return Connector(
        id=new_id(),
        station_id=station_id,
        host_id=host_id,
        name=name,
        connector_type=ConnectorType.HIKVISION_ISAPI,
        configuration=ConnectorConfiguration(address="10.2.0.21", port=80),
        credentials_configured=False,
        reachability=ConnectorReachability.UNVERIFIED,
        health_detail=None,
        capability=Unverified(),
        status=DeviceStatus.ACTIVE,
        revision=1,
        created_by=new_id(),
        updated_by=new_id(),
        created_at=NOW,
        updated_at=NOW,
    )


def a_point(
    station_id: UUID,
    connector_id: UUID,
    *,
    identifier: str = "DI-01",
    semantic_label: str = "工件到位",
    direction: PointDirection = PointDirection.INPUT,
) -> Point:
    return Point(
        id=new_id(),
        station_id=station_id,
        connector_id=connector_id,
        direction=direction,
        identifier=identifier,
        semantic_label=semantic_label,
        status=DeviceStatus.ACTIVE,
        revision=1,
        created_by=new_id(),
        updated_by=new_id(),
        created_at=NOW,
        updated_at=NOW,
    )


def measured() -> Measured:
    return Measured(
        delivery=Polled(interval=0.05),
        max_delivery_delay=0.08,
        sequencing=Sequencing.SEQUENCED,
        edges=EdgePreservation.PRESERVED,
        timestamps=TimestampSource.HOST_RECEIPT,
    )


def test_point_and_capability_commit_then_reload_as_whole_records(engine: Engine) -> None:
    station = a_station()
    host = a_host()
    connector = a_connector(station.id, host.id)
    caller = actor()
    point_id: UUID | None = None
    try:
        with DatabaseSession(engine) as writing:
            stations = PostgresStationRepository(writing)
            hosts = PostgresInferenceHostRepository(writing)
            connectors = PostgresConnectorRepository(writing)
            points = PostgresPointRepository(writing)
            stations.add(station)
            hosts.add(host)
            connectors.add(connector)
            point = create_point(
                station_id=station.id,
                connector_id=connector.id,
                direction=PointDirection.INPUT,
                identifier="DI-01",
                semantic_label="工件到位",
                caller=caller,
                now=NOW,
                stations=stations,
                connectors=connectors,
                points=points,
            )
            point_id = point.id
            edit_point(
                point_id=point.id,
                station_id=station.id,
                connector_id=connector.id,
                direction=PointDirection.INPUT,
                identifier="DI-001",
                semantic_label="上料工件到位",
                expected_revision=1,
                caller=caller,
                now=NOW,
                stations=stations,
                connectors=connectors,
                points=points,
            )
            set_point_status(
                point_id=point.id,
                requested_status=DeviceStatus.DEACTIVATED,
                expected_revision=2,
                caller=caller,
                now=NOW,
                points=points,
            )
            restored = set_point_status(
                point_id=point.id,
                requested_status=DeviceStatus.ACTIVE,
                expected_revision=3,
                caller=caller,
                now=NOW,
                points=points,
            )
            capable = update_connector_capability(
                connector_id=connector.id,
                capability=measured(),
                expected_revision=1,
                caller=caller,
                now=NOW,
                connectors=connectors,
            )
            assert restored == replace(
                point,
                identifier="DI-001",
                semantic_label="上料工件到位",
                status=DeviceStatus.ACTIVE,
                revision=4,
                updated_by=caller.user.id,
                updated_at=NOW,
            )
            assert capable == replace(
                connector,
                capability=measured(),
                revision=2,
                updated_by=caller.user.id,
                updated_at=NOW,
            )
            writing.commit()

        with DatabaseSession(engine) as reading:
            assert PostgresPointRepository(reading).by_id(point_id) == restored
            assert PostgresConnectorRepository(reading).by_id(connector.id) == capable
    finally:
        with engine.begin() as cleanup:
            cleanup.execute(delete(PointRow).where(PointRow.id == point_id))
            cleanup.execute(delete(ConnectorRow).where(ConnectorRow.id == connector.id))
            cleanup.execute(delete(StationRow).where(StationRow.id == station.id))
            cleanup.execute(delete(InferenceHostRow).where(InferenceHostRow.id == host.id))


def test_unique_identity_topology_and_revision_are_database_backed(
    session: DatabaseSession,
) -> None:
    station = a_station()
    other_station = a_station(code="B-101")
    host = a_host()
    connector = a_connector(station.id, host.id)
    stations = PostgresStationRepository(session)
    hosts = PostgresInferenceHostRepository(session)
    connectors = PostgresConnectorRepository(session)
    points = PostgresPointRepository(session)
    stations.add(station)
    stations.add(other_station)
    hosts.add(host)
    connectors.add(connector)
    first = a_point(station.id, connector.id)
    points.add(first)

    with pytest.raises(DeviceRefusedError) as semantic, session.begin_nested():
        points.add(
            a_point(
                station.id,
                connector.id,
                identifier="DI-02",
                semantic_label=first.semantic_label,
            )
        )
    assert semantic.value.code is DeviceRefusalCode.POINT_SEMANTIC_LABEL_TAKEN

    with pytest.raises(DeviceRefusedError) as physical, session.begin_nested():
        points.add(
            a_point(
                station.id,
                connector.id,
                identifier=first.identifier,
                semantic_label="另一个信号",
            )
        )
    assert physical.value.code is DeviceRefusalCode.POINT_IDENTITY_TAKEN

    opposite = a_point(
        station.id,
        connector.id,
        identifier=first.identifier,
        semantic_label="安全输出",
        direction=PointDirection.OUTPUT,
    )
    points.add(opposite)
    with pytest.raises(DeviceRefusedError) as topology, session.begin_nested():
        points.add(
            a_point(
                other_station.id,
                connector.id,
                identifier="DI-03",
                semantic_label="跨工位",
            )
        )
    assert topology.value.code is DeviceRefusalCode.POINT_CONNECTOR_STATION_MISMATCH

    changed = replace(first, semantic_label="工件已到位", revision=2)
    points.save(changed, expected_revision=1)
    with pytest.raises(DeviceRefusedError) as stale, session.begin_nested():
        points.save(replace(first, semantic_label="旧写入", revision=2), expected_revision=1)
    assert stale.value.code is DeviceRefusalCode.STALE_REVISION

    with pytest.raises(DeviceRefusedError) as move, session.begin_nested():
        connectors.save(
            replace(connector, station_id=other_station.id, revision=2),
            expected_revision=1,
        )
    assert move.value.code is DeviceRefusalCode.CONNECTOR_HAS_POINTS


def _wait_until_blocked_by(engine: Engine, *, waiter_pid: int, blocker_pid: int) -> None:
    with engine.connect() as observer:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            row = observer.execute(
                text(
                    """
                    SELECT wait_event_type, pg_blocking_pids(pid),
                           EXISTS (SELECT 1 FROM pg_locks WHERE pid = activity.pid AND NOT granted)
                      FROM pg_stat_activity AS activity WHERE pid = :pid
                    """
                ),
                {"pid": waiter_pid},
            ).one_or_none()
            if row is not None and row[0] == "Lock" and blocker_pid in row[1] and row[2]:
                return
            time.sleep(0.01)
    pytest.fail(f"pid {waiter_pid} did not block on pid {blocker_pid}")


def test_concurrent_point_insert_and_connector_move_cannot_split_station_topology(
    engine: Engine,
) -> None:
    station_a = a_station(code="RACE-A")
    station_b = a_station(code="RACE-B")
    host = a_host(name="并发推理机")
    connector = a_connector(station_a.id, host.id, name="并发连接器")
    point = a_point(station_a.id, connector.id, semantic_label="并发点位")
    setup = DatabaseSession(engine)
    point_flushed = threading.Event()
    release_point = threading.Event()
    mover_ready = threading.Event()
    outcomes: dict[str, BaseException | None] = {}
    pids: dict[str, int] = {}

    try:
        stations = PostgresStationRepository(setup)
        stations.add(station_a)
        stations.add(station_b)
        PostgresInferenceHostRepository(setup).add(host)
        PostgresConnectorRepository(setup).add(connector)
        setup.commit()

        def insert_point() -> None:
            local = DatabaseSession(engine)
            try:
                pids["point"] = local.scalar(text("SELECT pg_backend_pid()"))
                PostgresPointRepository(local).add(point)
                point_flushed.set()
                release_point.wait(10)
                local.commit()
                outcomes["point"] = None
            except BaseException as error:
                local.rollback()
                outcomes["point"] = error
            finally:
                local.close()

        def move_connector() -> None:
            local = DatabaseSession(engine)
            try:
                pids["move"] = local.scalar(text("SELECT pg_backend_pid()"))
                mover_ready.set()
                PostgresConnectorRepository(local).save(
                    replace(connector, station_id=station_b.id, revision=2),
                    expected_revision=1,
                )
                local.commit()
                outcomes["move"] = None
            except BaseException as error:
                local.rollback()
                outcomes["move"] = error
            finally:
                local.close()

        point_thread = threading.Thread(target=insert_point)
        move_thread = threading.Thread(target=move_connector)
        point_thread.start()
        assert point_flushed.wait(10)
        move_thread.start()
        assert mover_ready.wait(10)
        _wait_until_blocked_by(
            engine,
            waiter_pid=pids["move"],
            blocker_pid=pids["point"],
        )
        release_point.set()
        point_thread.join(10)
        move_thread.join(10)
        assert not point_thread.is_alive()
        assert not move_thread.is_alive()

        assert outcomes["point"] is None
        assert isinstance(outcomes["move"], DeviceRefusedError)
        assert outcomes["move"].code is DeviceRefusalCode.CONNECTOR_HAS_POINTS
        with DatabaseSession(engine) as reading:
            assert PostgresConnectorRepository(reading).by_id(connector.id) == connector
            assert PostgresPointRepository(reading).by_id(point.id) == point
    finally:
        release_point.set()
        if "point_thread" in locals():
            point_thread.join(10)
        if "move_thread" in locals():
            move_thread.join(10)
        setup.close()
        with engine.begin() as cleanup:
            cleanup.execute(delete(PointRow).where(PointRow.id == point.id))
            cleanup.execute(delete(ConnectorRow).where(ConnectorRow.id == connector.id))
            cleanup.execute(
                delete(StationRow).where(StationRow.id.in_([station_a.id, station_b.id]))
            )
            cleanup.execute(delete(InferenceHostRow).where(InferenceHostRow.id == host.id))


@pytest.fixture
def point_migration_database(engine: Engine) -> Iterator[Engine]:
    installer = create_engine(
        engine.url._replace(database="postgres"), isolation_level="AUTOCOMMIT"
    )
    database = f"nvsop_point_migration_{uuid4().hex[:12]}"
    with installer.connect() as connection:
        connection.exec_driver_sql(f'CREATE DATABASE "{database}"')
    installer.dispose()
    migrated = create_engine(engine.url._replace(database=database))
    try:
        yield migrated
    finally:
        migrated.dispose()
        with create_engine(
            engine.url._replace(database="postgres"), isolation_level="AUTOCOMMIT"
        ).connect() as connection:
            connection.exec_driver_sql(f'DROP DATABASE "{database}"')


def test_point_migrations_downgrade_reupgrade_and_backfill_unverified_capability(
    point_migration_database: Engine,
) -> None:
    configuration = Config(str(CONTROL_API / "alembic.ini"))
    configuration.set_main_option("script_location", str(CONTROL_API / "migrations"))
    configuration.set_main_option(
        "sqlalchemy.url", point_migration_database.url.render_as_string(hide_password=False)
    )
    command.upgrade(configuration, "0010")
    station_id = new_id()
    host_id = new_id()
    connector_id = new_id()
    with point_migration_database.begin() as connection:
        assert not inspect(connection).has_table("device_point")
        assert "capability" not in {
            column["name"] for column in inspect(connection).get_columns("device_connector")
        }
        assert not POINT_PERMISSIONS & set(
            connection.execute(text("SELECT code FROM auth_permission")).scalars()
        )
        common = {
            "actor": new_id(),
            "now": NOW,
        }
        connection.execute(
            text(
                """
                INSERT INTO device_inference_host
                    (id, name, address, mediamtx_address, recording_window_seconds,
                     disk_watermark_percent, status, revision, created_by, updated_by,
                     created_at, updated_at)
                VALUES (:id, 'migration-host', '10.9.0.1', NULL, 604800, 85,
                        'active', 1, :actor, :actor, :now, :now)
                """
            ),
            {**common, "id": host_id},
        )
        connection.execute(
            text(
                """
                INSERT INTO device_station
                    (id, code, name, tags, status, revision, created_by, updated_by,
                     created_at, updated_at)
                VALUES (:id, 'MIGRATION', 'migration-station', '[]'::jsonb,
                        'active', 1, :actor, :actor, :now, :now)
                """
            ),
            {**common, "id": station_id},
        )
        connection.execute(
            text(
                """
                INSERT INTO device_connector
                    (id, station_id, host_id, name, connector_type, configuration,
                     credentials_configured, reachability, health_detail, status, revision,
                     created_by, updated_by, created_at, updated_at)
                VALUES (:id, :station_id, :host_id, 'migration-connector',
                        'hikvision_isapi', '{"address":"10.9.0.2"}'::jsonb,
                        false, 'unverified', NULL, 'active', 1,
                        :actor, :actor, :now, :now)
                """
            ),
            {**common, "id": connector_id, "station_id": station_id, "host_id": host_id},
        )

    command.upgrade(configuration, "head")
    with point_migration_database.connect() as connection:
        assert inspect(connection).has_table("device_point")
        assert "capability" in {
            column["name"] for column in inspect(connection).get_columns("device_connector")
        }
        assert (
            set(connection.execute(text("SELECT code FROM auth_permission")).scalars())
            >= POINT_PERMISSIONS
        )
        assert connection.execute(
            text("SELECT capability FROM device_connector WHERE id = :id"),
            {"id": connector_id},
        ).scalar_one() == {"verification": "unverified"}

    command.downgrade(configuration, "0010")
    with point_migration_database.connect() as connection:
        assert not inspect(connection).has_table("device_point")
        assert "capability" not in {
            column["name"] for column in inspect(connection).get_columns("device_connector")
        }
        assert not POINT_PERMISSIONS & set(
            connection.execute(text("SELECT code FROM auth_permission")).scalars()
        )

    command.upgrade(configuration, "head")
    with point_migration_database.connect() as connection:
        assert inspect(connection).has_table("device_point")
        assert (
            set(connection.execute(text("SELECT code FROM auth_permission")).scalars())
            >= POINT_PERMISSIONS
        )
        assert connection.execute(
            text("SELECT capability FROM device_connector WHERE id = :id"),
            {"id": connector_id},
        ).scalar_one() == {"verification": "unverified"}
