"""通过真实 PostgreSQL 适配器持久化工位和相机。"""

from __future__ import annotations

import threading
import time
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

import pytest
from sqlalchemy import Engine, delete, text
from sqlalchemy.orm import Session as DatabaseSession

from factory_sop.auth.api import Caller, Permission
from factory_sop.auth.model import User, UserStatus
from factory_sop.device.adapters.repository import (
    PostgresCameraRepository,
    PostgresInferenceBackendRepository,
    PostgresInferenceHostRepository,
    PostgresStationRepository,
)
from factory_sop.device.adapters.tables import (
    CameraRow,
    InferenceBackendRow,
    InferenceHostRow,
    StationRow,
)
from factory_sop.device.errors import DeviceRefusalCode, DeviceRefusedError
from factory_sop.device.model import (
    Camera,
    ConnectionState,
    DeviceStatus,
    InferenceBackend,
    InferenceHost,
    Station,
)
from factory_sop.device.usecases.backends import edit_backend
from factory_sop.identifiers import new_id

NOW = datetime(2026, 9, 7, 1, 0, tzinfo=UTC)


def backend_editor() -> Caller:
    actor = new_id()
    return Caller(
        user=User(
            id=actor,
            login_name="device-admin",
            display_name="设备管理员",
            password_hash="not-used",  # pragma: allowlist secret
            status=UserStatus.ACTIVE,
        ),
        granted=frozenset({Permission.INFERENCE_BACKEND_EDIT}),
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


def a_backend(
    host_id: UUID,
    *,
    port: int = 8000,
    template_version_id: UUID | None = None,
) -> InferenceBackend:
    return InferenceBackend(
        id=new_id(),
        host_id=host_id,
        base_url=f"http://10.0.8.11:{port}",
        template_version_id=template_version_id,
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


def a_station(*, code: str = "A-001") -> Station:
    return Station(
        id=new_id(),
        code=code,
        name="装配一号工位",
        tags=("装配", "一线"),
        status=DeviceStatus.ACTIVE,
        revision=1,
        created_by=new_id(),
        updated_by=new_id(),
        created_at=NOW,
        updated_at=NOW,
    )


def a_camera(station: Station, host: InferenceHost, backend: InferenceBackend) -> Camera:
    return Camera(
        id=new_id(),
        name="一号相机",
        address="10.0.8.21",
        main_stream_path="/Streaming/Channels/101",
        sub_stream_path="/Streaming/Channels/102",
        credentials_configured=False,
        station_id=station.id,
        host_id=host.id,
        backend_id=backend.id,
        status=DeviceStatus.ACTIVE,
        revision=1,
        created_by=new_id(),
        updated_by=new_id(),
        created_at=NOW,
        updated_at=NOW,
    )


def test_device_dtos_survive_save_commit_and_new_session_reload(engine: Engine) -> None:
    station = a_station()
    host = a_host()
    backend = a_backend(host.id, template_version_id=new_id())
    camera = replace(a_camera(station, host, backend), credentials_configured=True)
    setup = DatabaseSession(engine)
    try:
        stations = PostgresStationRepository(setup)
        hosts = PostgresInferenceHostRepository(setup)
        backends = PostgresInferenceBackendRepository(setup)
        cameras = PostgresCameraRepository(setup)
        stations.add(station)
        hosts.add(host)
        backends.add(backend)
        cameras.add(camera)
        setup.commit()

        saved_station = replace(station, tags=("质量", "二线"), revision=2)
        saved_backend = replace(backend, template_version_id=new_id(), revision=2)
        saved_camera = replace(
            camera,
            main_stream_path="/Streaming/Channels/201",
            sub_stream_path="/Streaming/Channels/202",
            revision=2,
        )
        stations.save(saved_station, expected_revision=station.revision)
        backends.save(saved_backend, expected_revision=backend.revision)
        cameras.save(saved_camera, expected_revision=camera.revision)
        setup.commit()

        with DatabaseSession(engine) as reloaded_session:
            reloaded_stations = PostgresStationRepository(reloaded_session)
            reloaded_backends = PostgresInferenceBackendRepository(reloaded_session)
            reloaded_cameras = PostgresCameraRepository(reloaded_session)
            assert reloaded_stations.by_id(station.id) == saved_station
            assert reloaded_backends.by_id(backend.id) == saved_backend
            assert reloaded_cameras.by_id(camera.id) == saved_camera
    finally:
        cleanup = DatabaseSession(engine)
        try:
            PostgresCameraRepository(cleanup).remove(camera.id, expected_revision=2)
            PostgresInferenceBackendRepository(cleanup).remove(backend.id, expected_revision=2)
            PostgresInferenceHostRepository(cleanup).remove(
                host.id, expected_revision=host.revision
            )
            PostgresStationRepository(cleanup).remove(station.id, expected_revision=2)
            cleanup.commit()
        finally:
            cleanup.close()


def test_station_code_is_a_real_unique_constraint(session: DatabaseSession) -> None:
    stations = PostgresStationRepository(session)
    stations.add(a_station())

    with pytest.raises(DeviceRefusedError) as refused:
        stations.add(a_station())

    assert refused.value.code is DeviceRefusalCode.STATION_CODE_TAKEN


def test_database_rejects_a_camera_whose_host_differs_from_its_backend(
    session: DatabaseSession,
) -> None:
    stations = PostgresStationRepository(session)
    hosts = PostgresInferenceHostRepository(session)
    backends = PostgresInferenceBackendRepository(session)
    cameras = PostgresCameraRepository(session)
    station = a_station()
    host_a = a_host()
    host_b = a_host(name="推理机-2")
    backend = a_backend(host_a.id)
    stations.add(station)
    hosts.add(host_a)
    hosts.add(host_b)
    backends.add(backend)
    session.flush()

    with pytest.raises(DeviceRefusedError) as refused:
        cameras.add(a_camera(station, host_b, backend))

    assert refused.value.code is DeviceRefusalCode.CAMERA_HOST_BACKEND_MISMATCH


def test_database_rejects_cross_host_and_cross_template_cameras_for_one_station(
    session: DatabaseSession,
) -> None:
    stations = PostgresStationRepository(session)
    hosts = PostgresInferenceHostRepository(session)
    backends = PostgresInferenceBackendRepository(session)
    cameras = PostgresCameraRepository(session)
    station = a_station()
    host_a = a_host()
    host_b = a_host(name="推理机-2")
    template_a = new_id()
    backend_a = a_backend(host_a.id, port=8000, template_version_id=template_a)
    backend_b = a_backend(host_b.id, port=8000, template_version_id=template_a)
    backend_c = a_backend(host_a.id, port=8001, template_version_id=new_id())
    stations.add(station)
    hosts.add(host_a)
    hosts.add(host_b)
    backends.add(backend_a)
    backends.add(backend_b)
    backends.add(backend_c)
    cameras.add(a_camera(station, host_a, backend_a))

    with session.begin_nested(), pytest.raises(DeviceRefusedError) as host_refused:
        cameras.add(a_camera(station, host_b, backend_b))
    assert host_refused.value.code is DeviceRefusalCode.CAMERA_STATION_HOST_CONFLICT
    assert [(item.field, item.message) for item in host_refused.value.field_errors] == [
        ("host_id", "同一工位的相机必须属于同一推理机")
    ]

    with session.begin_nested(), pytest.raises(DeviceRefusedError) as template_refused:
        cameras.add(a_camera(station, host_a, backend_c))
    assert template_refused.value.code is DeviceRefusalCode.CAMERA_STATION_TEMPLATE_CONFLICT
    assert [(item.field, item.message) for item in template_refused.value.field_errors] == [
        ("backend_id", "该推理后端的模板必须与工位已有相机一致")
    ]


def test_backend_use_case_cannot_move_a_backend_that_a_camera_references(
    session: DatabaseSession,
) -> None:
    stations = PostgresStationRepository(session)
    hosts = PostgresInferenceHostRepository(session)
    backends = PostgresInferenceBackendRepository(session)
    cameras = PostgresCameraRepository(session)
    station = a_station()
    host_a = a_host()
    host_b = a_host(name="推理机-2")
    backend = a_backend(host_a.id)
    stations.add(station)
    hosts.add(host_a)
    hosts.add(host_b)
    backends.add(backend)
    cameras.add(a_camera(station, host_a, backend))
    session.flush()

    savepoint = session.begin_nested()
    try:
        with pytest.raises(DeviceRefusedError) as refused:
            edit_backend(
                backend_id=backend.id,
                host_id=host_b.id,
                base_url=backend.base_url,
                expected_revision=backend.revision,
                caller=backend_editor(),
                now=NOW,
                hosts=hosts,
                backends=backends,
            )
    finally:
        if savepoint.is_active:
            savepoint.rollback()

    assert refused.value.code is DeviceRefusalCode.CAMERA_HOST_BACKEND_MISMATCH
    assert len(cameras.for_station(station.id)) == 1


def test_parent_backend_host_change_is_rejected_while_a_camera_references_it(
    session: DatabaseSession,
) -> None:
    stations = PostgresStationRepository(session)
    hosts = PostgresInferenceHostRepository(session)
    backends = PostgresInferenceBackendRepository(session)
    cameras = PostgresCameraRepository(session)
    station = a_station()
    host_a = a_host()
    host_b = a_host(name="推理机-2")
    backend = a_backend(host_a.id, template_version_id=new_id())
    stations.add(station)
    hosts.add(host_a)
    hosts.add(host_b)
    backends.add(backend)
    cameras.add(a_camera(station, host_a, backend))
    session.flush()

    savepoint = session.begin_nested()
    try:
        with pytest.raises(DeviceRefusedError) as refused:
            backends.save(replace(backend, host_id=host_b.id, revision=2), expected_revision=1)
    finally:
        if savepoint.is_active:
            savepoint.rollback()

    assert refused.value.code is DeviceRefusalCode.CAMERA_HOST_BACKEND_MISMATCH
    assert [(item.field, item.message) for item in refused.value.field_errors] == [
        ("host_id", "必须与推理后端所属推理机一致"),
        ("backend_id", "必须属于所选推理机"),
    ]


def test_referenced_backend_allows_null_to_template_when_the_station_stays_consistent(
    session: DatabaseSession,
) -> None:
    stations = PostgresStationRepository(session)
    hosts = PostgresInferenceHostRepository(session)
    backends = PostgresInferenceBackendRepository(session)
    cameras = PostgresCameraRepository(session)
    station = a_station()
    host = a_host()
    backend = a_backend(host.id)
    stations.add(station)
    hosts.add(host)
    backends.add(backend)
    cameras.add(a_camera(station, host, backend))
    session.flush()

    updated = replace(backend, template_version_id=new_id(), revision=2)
    backends.save(updated, expected_revision=backend.revision)
    session.flush()

    assert backends.by_id(backend.id) == updated


def test_referenced_backend_allows_a_consistent_template_change(session: DatabaseSession) -> None:
    stations = PostgresStationRepository(session)
    hosts = PostgresInferenceHostRepository(session)
    backends = PostgresInferenceBackendRepository(session)
    cameras = PostgresCameraRepository(session)
    station = a_station()
    host = a_host()
    old_template = new_id()
    backend = a_backend(host.id, template_version_id=old_template)
    stations.add(station)
    hosts.add(host)
    backends.add(backend)
    cameras.add(a_camera(station, host, backend))
    session.flush()

    updated = replace(backend, template_version_id=new_id(), revision=2)
    backends.save(updated, expected_revision=backend.revision)
    session.flush()

    assert backends.by_id(backend.id) == updated


def test_referenced_backend_rejects_a_template_change_that_breaks_station_consistency(
    session: DatabaseSession,
) -> None:
    stations = PostgresStationRepository(session)
    hosts = PostgresInferenceHostRepository(session)
    backends = PostgresInferenceBackendRepository(session)
    cameras = PostgresCameraRepository(session)
    station = a_station()
    host = a_host()
    old_template = new_id()
    backend = a_backend(host.id, template_version_id=old_template)
    other_backend = a_backend(host.id, port=8001, template_version_id=old_template)
    stations.add(station)
    hosts.add(host)
    backends.add(backend)
    backends.add(other_backend)
    cameras.add(a_camera(station, host, backend))
    cameras.add(a_camera(station, host, other_backend))
    session.flush()

    savepoint = session.begin_nested()
    try:
        with pytest.raises(DeviceRefusedError) as refused:
            backends.save(
                replace(backend, template_version_id=new_id(), revision=2),
                expected_revision=backend.revision,
            )
    finally:
        if savepoint.is_active:
            savepoint.rollback()

    assert refused.value.code is DeviceRefusalCode.CAMERA_STATION_TEMPLATE_CONFLICT
    assert [(item.field, item.message) for item in refused.value.field_errors] == [
        ("backend_id", "该推理后端的模板必须与工位已有相机一致")
    ]


def test_referenced_backend_allows_an_endpoint_only_edit(
    session: DatabaseSession,
) -> None:
    stations = PostgresStationRepository(session)
    hosts = PostgresInferenceHostRepository(session)
    backends = PostgresInferenceBackendRepository(session)
    cameras = PostgresCameraRepository(session)
    station = a_station()
    host = a_host()
    backend = a_backend(host.id)
    stations.add(station)
    hosts.add(host)
    backends.add(backend)
    cameras.add(a_camera(station, host, backend))

    updated = replace(backend, base_url="http://10.0.8.11:8001", revision=2)
    backends.save(updated, expected_revision=1)
    session.flush()

    assert backends.by_id(backend.id) == updated


def test_deactivated_parents_reject_new_camera_but_existing_history_survives(
    session: DatabaseSession,
) -> None:
    stations = PostgresStationRepository(session)
    hosts = PostgresInferenceHostRepository(session)
    backends = PostgresInferenceBackendRepository(session)
    cameras = PostgresCameraRepository(session)
    station = a_station()
    host = a_host()
    backend = a_backend(host.id)
    stations.add(station)
    hosts.add(host)
    backends.add(backend)
    cameras.add(a_camera(station, host, backend))
    stations.save(
        replace(station, status=DeviceStatus.DEACTIVATED, revision=2), expected_revision=1
    )
    session.flush()
    assert len(cameras.for_station(station.id)) == 1

    with session.begin_nested(), pytest.raises(DeviceRefusedError) as refused:
        cameras.add(a_camera(station, host, backend))
    assert refused.value.code is DeviceRefusalCode.STATION_DEACTIVATED


@pytest.mark.parametrize("conflict_kind", ["host", "template"])
def test_concurrent_camera_creations_for_one_station_preserve_host_and_template_invariants(
    engine: Engine, conflict_kind: str
) -> None:
    station = a_station()
    host_a = a_host()
    host_b = a_host(name="推理机-2") if conflict_kind == "host" else host_a
    shared_template = new_id()
    backend_a = a_backend(host_a.id, template_version_id=shared_template)
    backend_b = a_backend(
        host_b.id,
        port=8000 if conflict_kind == "host" else 8001,
        template_version_id=shared_template if conflict_kind == "host" else new_id(),
    )
    camera_a = a_camera(station, host_a, backend_a)
    camera_b = a_camera(station, host_b, backend_b)
    setup = DatabaseSession(engine)
    try:
        stations = PostgresStationRepository(setup)
        hosts = PostgresInferenceHostRepository(setup)
        backends = PostgresInferenceBackendRepository(setup)
        stations.add(station)
        hosts.add(host_a)
        if host_b.id != host_a.id:
            hosts.add(host_b)
        backends.add(backend_a)
        backends.add(backend_b)
        setup.commit()

        first_flushed = threading.Event()
        second_started = threading.Event()
        second_flushed = threading.Event()
        release_first = threading.Event()
        outcomes: dict[str, tuple[str, BaseException | None]] = {}
        outcomes_lock = threading.Lock()

        def insert_camera(label: str, camera: Camera) -> None:
            local = DatabaseSession(engine)
            try:
                if label == "second":
                    second_started.set()
                PostgresCameraRepository(local).add(camera)
                with outcomes_lock:
                    outcomes[label] = ("flushed", None)
                if label == "first":
                    first_flushed.set()
                    release_first.wait(10)
                else:
                    second_flushed.set()
                local.commit()
                with outcomes_lock:
                    outcomes[label] = ("committed", None)
            except BaseException as error:
                local.rollback()
                with outcomes_lock:
                    outcomes[label] = ("error", error)
            finally:
                local.close()

        first = threading.Thread(target=insert_camera, args=("first", camera_a))
        second = threading.Thread(target=insert_camera, args=("second", camera_b))
        first.start()
        assert first_flushed.wait(10)
        second.start()
        assert second_started.wait(10)
        # 没有工位锁时，第二次 flush 会在第一个事务持有锁期间完成；
        # 有锁时它会等待，因此无论观察到哪种情况，都在此释放第一个事务。
        second_flushed.wait(2)
        release_first.set()
        first.join(10)
        second.join(10)
        assert not first.is_alive()
        assert not second.is_alive()

        assert {status for status, _ in outcomes.values()} == {"committed", "error"}
        error = outcomes["second"][1]
        assert isinstance(error, DeviceRefusedError)
        expected_code = (
            DeviceRefusalCode.CAMERA_STATION_HOST_CONFLICT
            if conflict_kind == "host"
            else DeviceRefusalCode.CAMERA_STATION_TEMPLATE_CONFLICT
        )
        assert error.code is expected_code
    finally:
        release_first.set()
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
                delete(InferenceHostRow).where(InferenceHostRow.id.in_({host_a.id, host_b.id}))
            )
            connection.execute(delete(StationRow).where(StationRow.id == station.id))


def test_concurrent_template_updates_for_one_station_cannot_write_skew(engine: Engine) -> None:
    station = a_station()
    host = a_host()
    old_template = new_id()
    backend_a = a_backend(host.id, template_version_id=old_template)
    backend_b = a_backend(host.id, port=8001, template_version_id=old_template)
    camera_a = a_camera(station, host, backend_a)
    camera_b = a_camera(station, host, backend_b)
    setup = DatabaseSession(engine)
    station_lock = DatabaseSession(engine)
    first_pid_ready = threading.Event()
    second_pid_ready = threading.Event()
    outcomes: dict[str, tuple[str, BaseException | None]] = {}
    pids: dict[str, int] = {}
    outcomes_lock = threading.Lock()
    try:
        stations = PostgresStationRepository(setup)
        hosts = PostgresInferenceHostRepository(setup)
        backends = PostgresInferenceBackendRepository(setup)
        cameras = PostgresCameraRepository(setup)
        stations.add(station)
        hosts.add(host)
        backends.add(backend_a)
        backends.add(backend_b)
        cameras.add(camera_a)
        cameras.add(camera_b)
        setup.commit()
        station_lock.execute(
            text("SELECT id FROM device_station WHERE id = :station_id FOR UPDATE"),
            {"station_id": station.id},
        )

        def update_template(
            label: str,
            backend: InferenceBackend,
            template_version_id: UUID,
            ready: threading.Event,
        ) -> None:
            local = DatabaseSession(engine)
            try:
                local.execute(text("SET deadlock_timeout = '100ms'"))
                pids[label] = local.scalar(text("SELECT pg_backend_pid()"))
                ready.set()
                PostgresInferenceBackendRepository(local).save(
                    replace(backend, template_version_id=template_version_id, revision=2),
                    expected_revision=backend.revision,
                )
                local.commit()
                with outcomes_lock:
                    outcomes[label] = ("committed", None)
            except BaseException as error:
                local.rollback()
                with outcomes_lock:
                    outcomes[label] = ("error", error)
            finally:
                local.close()

        first = threading.Thread(
            target=update_template, args=("first", backend_a, new_id(), first_pid_ready)
        )
        second = threading.Thread(
            target=update_template, args=("second", backend_b, new_id(), second_pid_ready)
        )
        first.start()
        second.start()
        assert first_pid_ready.wait(5)
        assert second_pid_ready.wait(5)

        with engine.connect() as observer:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                waiting = all(
                    observer.scalar(
                        text(
                            "SELECT wait_event_type = 'Lock' FROM pg_stat_activity WHERE pid = :pid"
                        ),
                        {"pid": pids[label]},
                    )
                    for label in ("first", "second")
                )
                if waiting:
                    break
                time.sleep(0.01)
            else:
                pytest.fail("both backend template updates did not reach the station lock")
        station_lock.rollback()
        first.join(10)
        second.join(10)
        assert not first.is_alive()
        assert not second.is_alive()

        assert {status for status, _ in outcomes.values()} == {"error"}
        assert all(
            isinstance(error, DeviceRefusedError)
            and error.code is DeviceRefusalCode.CAMERA_STATION_TEMPLATE_CONFLICT
            for _, error in outcomes.values()
        )
        with DatabaseSession(engine) as reloaded_session:
            reloaded_backends = PostgresInferenceBackendRepository(reloaded_session)
            assert reloaded_backends.by_id(backend_a.id) == backend_a
            assert reloaded_backends.by_id(backend_b.id) == backend_b
    finally:
        station_lock.rollback()
        station_lock.close()
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
            connection.execute(delete(InferenceHostRow).where(InferenceHostRow.id == host.id))
            connection.execute(delete(StationRow).where(StationRow.id == station.id))


def test_camera_creation_and_backend_relocation_serialize_at_the_backend_row(
    engine: Engine,
) -> None:
    station = a_station()
    host_a = a_host()
    host_b = a_host(name="推理机-2")
    backend = a_backend(host_a.id, template_version_id=new_id())
    camera = a_camera(station, host_a, backend)
    setup = DatabaseSession(engine)
    release_camera = threading.Event()
    camera_flushed = threading.Event()
    backend_pid_ready = threading.Event()
    outcomes: dict[str, tuple[str, BaseException | None]] = {}
    outcomes_lock = threading.Lock()
    backend_pid: int | None = None
    try:
        stations = PostgresStationRepository(setup)
        hosts = PostgresInferenceHostRepository(setup)
        backends = PostgresInferenceBackendRepository(setup)
        stations.add(station)
        hosts.add(host_a)
        hosts.add(host_b)
        backends.add(backend)
        setup.commit()

        def insert() -> None:
            local = DatabaseSession(engine)
            try:
                PostgresCameraRepository(local).add(camera)
                camera_flushed.set()
                release_camera.wait(10)
                local.commit()
                with outcomes_lock:
                    outcomes["camera"] = ("committed", None)
            except BaseException as error:
                local.rollback()
                with outcomes_lock:
                    outcomes["camera"] = ("error", error)
            finally:
                local.close()

        def relocate() -> None:
            nonlocal backend_pid
            local = DatabaseSession(engine)
            try:
                local.execute(text("SET deadlock_timeout = '100ms'"))
                backend_pid = local.scalar(text("SELECT pg_backend_pid()"))
                backend_pid_ready.set()
                assert camera_flushed.wait(10)
                PostgresInferenceBackendRepository(local).save(
                    replace(backend, host_id=host_b.id, revision=2), expected_revision=1
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

        camera_thread = threading.Thread(target=insert)
        backend_thread = threading.Thread(target=relocate)
        camera_thread.start()
        assert camera_flushed.wait(10)
        backend_thread.start()
        assert backend_pid_ready.wait(10)
        assert backend_pid is not None
        with engine.connect() as observer:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                waiting = observer.scalar(
                    text("SELECT wait_event_type = 'Lock' FROM pg_stat_activity WHERE pid = :pid"),
                    {"pid": backend_pid},
                )
                if waiting:
                    break
                time.sleep(0.01)
            else:
                pytest.fail("backend update never reached its database lock wait")
        release_camera.set()
        camera_thread.join(10)
        backend_thread.join(10)
        assert not camera_thread.is_alive()
        assert not backend_thread.is_alive()

        assert outcomes["camera"][0] == "committed"
        backend_error = outcomes["backend"][1]
        assert isinstance(backend_error, DeviceRefusedError)
        assert backend_error.code is DeviceRefusalCode.CAMERA_HOST_BACKEND_MISMATCH
    finally:
        release_camera.set()
        setup.close()
        with engine.begin() as connection:
            connection.execute(delete(CameraRow).where(CameraRow.id == camera.id))
            connection.execute(
                delete(InferenceBackendRow).where(InferenceBackendRow.id == backend.id)
            )
            connection.execute(
                delete(InferenceHostRow).where(InferenceHostRow.id.in_([host_a.id, host_b.id]))
            )
            connection.execute(delete(StationRow).where(StationRow.id == station.id))


def test_camera_move_and_backend_delete_serialize_without_a_deadlock(engine: Engine) -> None:
    station = a_station()
    host_a = a_host()
    host_b = a_host(name="推理机-2")
    backend_a = a_backend(host_a.id)
    backend_b = a_backend(host_b.id, port=8001)
    camera = a_camera(station, host_a, backend_a)
    setup = DatabaseSession(engine)
    delete_ready = threading.Event()
    move_pid_ready = threading.Event()
    release_delete = threading.Event()
    outcomes: dict[str, tuple[str, BaseException | None]] = {}
    outcomes_lock = threading.Lock()
    move_pid: int | None = None

    try:
        stations = PostgresStationRepository(setup)
        hosts = PostgresInferenceHostRepository(setup)
        backends = PostgresInferenceBackendRepository(setup)
        cameras = PostgresCameraRepository(setup)
        stations.add(station)
        hosts.add(host_a)
        hosts.add(host_b)
        backends.add(backend_a)
        backends.add(backend_b)
        cameras.add(camera)
        setup.commit()

        def delete_backend() -> None:
            local = DatabaseSession(engine)
            try:
                local.execute(text("SET deadlock_timeout = '100ms'"))
                local.execute(
                    text(
                        "SELECT id FROM device_inference_backend WHERE id = :backend_id FOR UPDATE"
                    ),
                    {"backend_id": backend_a.id},
                )
                delete_ready.set()
                release_delete.wait(10)
                PostgresInferenceBackendRepository(local).remove(
                    backend_a.id, expected_revision=backend_a.revision
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

        def move_camera() -> None:
            nonlocal move_pid
            local = DatabaseSession(engine)
            try:
                local.execute(text("SET deadlock_timeout = '100ms'"))
                move_pid = local.scalar(text("SELECT pg_backend_pid()"))
                move_pid_ready.set()
                PostgresCameraRepository(local).save(
                    replace(camera, host_id=host_b.id, backend_id=backend_b.id, revision=2),
                    expected_revision=camera.revision,
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

        backend_thread = threading.Thread(target=delete_backend)
        camera_thread = threading.Thread(target=move_camera)
        backend_thread.start()
        assert delete_ready.wait(5)
        camera_thread.start()
        assert move_pid_ready.wait(5)

        assert move_pid is not None
        with engine.connect() as observer:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                waiting = observer.scalar(
                    text("SELECT wait_event_type = 'Lock' FROM pg_stat_activity WHERE pid = :pid"),
                    {"pid": move_pid},
                )
                if waiting:
                    break
                time.sleep(0.01)
            else:
                pytest.fail("camera move never reached its database lock wait")

        release_delete.set()
        backend_thread.join(10)
        camera_thread.join(10)
        assert not backend_thread.is_alive()
        assert not camera_thread.is_alive()

        assert outcomes["camera"] == ("committed", None)
        delete_error = outcomes["backend"][1]
        assert isinstance(delete_error, DeviceRefusedError)
        assert delete_error.code is DeviceRefusalCode.INFERENCE_BACKEND_HAS_CAMERAS
    finally:
        release_delete.set()
        setup.close()
        with engine.begin() as connection:
            connection.execute(delete(CameraRow).where(CameraRow.id == camera.id))
            connection.execute(
                delete(InferenceBackendRow).where(
                    InferenceBackendRow.id.in_([backend_a.id, backend_b.id])
                )
            )
            connection.execute(
                delete(InferenceHostRow).where(InferenceHostRow.id.in_([host_a.id, host_b.id]))
            )
            connection.execute(delete(StationRow).where(StationRow.id == station.id))


def test_backend_delete_waits_for_a_concurrent_camera_creation(
    engine: Engine,
) -> None:
    station = a_station()
    host = a_host()
    backend = a_backend(host.id)
    camera = a_camera(station, host, backend)
    setup = DatabaseSession(engine)
    camera_flushed = threading.Event()
    delete_pid_ready = threading.Event()
    release_camera = threading.Event()
    outcomes: dict[str, tuple[str, BaseException | None]] = {}
    outcomes_lock = threading.Lock()
    delete_pid: int | None = None

    try:
        stations = PostgresStationRepository(setup)
        hosts = PostgresInferenceHostRepository(setup)
        backends = PostgresInferenceBackendRepository(setup)
        stations.add(station)
        hosts.add(host)
        backends.add(backend)
        setup.commit()

        def create_camera() -> None:
            local = DatabaseSession(engine)
            try:
                PostgresCameraRepository(local).add(camera)
                camera_flushed.set()
                release_camera.wait(10)
                local.commit()
                with outcomes_lock:
                    outcomes["camera"] = ("committed", None)
            except BaseException as error:
                local.rollback()
                with outcomes_lock:
                    outcomes["camera"] = ("error", error)
            finally:
                local.close()

        def delete_backend() -> None:
            nonlocal delete_pid
            local = DatabaseSession(engine)
            try:
                local.execute(text("SET deadlock_timeout = '100ms'"))
                delete_pid = local.scalar(text("SELECT pg_backend_pid()"))
                delete_pid_ready.set()
                assert camera_flushed.wait(5)
                PostgresInferenceBackendRepository(local).remove(
                    backend.id, expected_revision=backend.revision
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

        camera_thread = threading.Thread(target=create_camera)
        backend_thread = threading.Thread(target=delete_backend)
        camera_thread.start()
        assert camera_flushed.wait(5)
        backend_thread.start()
        assert delete_pid_ready.wait(5)

        assert delete_pid is not None
        with engine.connect() as observer:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                waiting = observer.scalar(
                    text("SELECT wait_event_type = 'Lock' FROM pg_stat_activity WHERE pid = :pid"),
                    {"pid": delete_pid},
                )
                if waiting:
                    break
                time.sleep(0.01)
            else:
                pytest.fail("backend delete never waited for the creating transaction")

        release_camera.set()
        camera_thread.join(10)
        backend_thread.join(10)
        assert not camera_thread.is_alive()
        assert not backend_thread.is_alive()

        assert outcomes["camera"] == ("committed", None)
        delete_error = outcomes["backend"][1]
        assert isinstance(delete_error, DeviceRefusedError)
        assert delete_error.code is DeviceRefusalCode.INFERENCE_BACKEND_HAS_CAMERAS
    finally:
        release_camera.set()
        setup.close()
        with engine.begin() as connection:
            connection.execute(delete(CameraRow).where(CameraRow.id == camera.id))
            connection.execute(
                delete(InferenceBackendRow).where(InferenceBackendRow.id == backend.id)
            )
            connection.execute(delete(InferenceHostRow).where(InferenceHostRow.id == host.id))
            connection.execute(delete(StationRow).where(StationRow.id == station.id))


def test_station_and_backend_delete_refuse_while_camera_history_exists(
    session: DatabaseSession,
) -> None:
    stations = PostgresStationRepository(session)
    hosts = PostgresInferenceHostRepository(session)
    backends = PostgresInferenceBackendRepository(session)
    cameras = PostgresCameraRepository(session)
    station = a_station()
    host = a_host()
    backend = a_backend(host.id)
    stations.add(station)
    hosts.add(host)
    backends.add(backend)
    cameras.add(a_camera(station, host, backend))
    session.flush()

    with pytest.raises(DeviceRefusedError) as station_refused:
        stations.remove(station.id, expected_revision=station.revision)
    assert station_refused.value.code is DeviceRefusalCode.STATION_HAS_CAMERAS

    session.rollback()
    # 失败语句后外层 fixture 事务可以回滚；为后端外键断言安排一份
    # 新的已提交设置，确保测试观察到真实数据库状态。
    stations.add(station)
    hosts.add(host)
    backends.add(backend)
    cameras.add(a_camera(station, host, backend))
    session.flush()
    with pytest.raises(DeviceRefusedError) as backend_refused:
        backends.remove(backend.id, expected_revision=backend.revision)
    assert backend_refused.value.code is DeviceRefusalCode.INFERENCE_BACKEND_HAS_CAMERAS


def test_a_failed_camera_write_rolls_back_the_same_transaction(session: DatabaseSession) -> None:
    stations = PostgresStationRepository(session)
    hosts = PostgresInferenceHostRepository(session)
    cameras = PostgresCameraRepository(session)
    station = a_station()
    host = a_host()
    stations.add(station)
    hosts.add(host)
    session.flush()

    with pytest.raises(DeviceRefusedError):
        cameras.add(a_camera(station, host, a_backend(new_id())))
    session.rollback()

    assert stations.by_id(station.id) is None
