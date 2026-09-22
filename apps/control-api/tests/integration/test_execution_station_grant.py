"""execution 工位物理执行权在真实 PostgreSQL 上的并发不变量。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from threading import Barrier, Thread

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session as DatabaseSession

from factory_sop.device.adapters.repository import (
    PostgresInferenceHostRepository,
    PostgresStationRepository,
)
from factory_sop.device.model import DeviceStatus, InferenceHost, Station
from factory_sop.execution.adapters.dependencies import lease_gateway
from factory_sop.execution.api import ExecutionRefusalCode, ExecutionRefusedError
from factory_sop.identifiers import new_id

NOW = datetime(2026, 9, 22, 1, 0, tzinfo=UTC)
SEVEN_DAYS = timedelta(days=7)


def _host(name: str) -> InferenceHost:
    return InferenceHost(
        id=new_id(),
        name=name,
        address="10.0.8.11",
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


def _station() -> Station:
    return Station(
        id=new_id(),
        code=f"EXEC-{new_id().hex[:8]}",
        name="执行权测试工位",
        tags=(),
        status=DeviceStatus.ACTIVE,
        revision=1,
        created_by=new_id(),
        updated_by=new_id(),
        created_at=NOW,
        updated_at=NOW,
    )


def _arrange_targets(engine: Engine) -> tuple[Station, InferenceHost, InferenceHost]:
    station = _station()
    host_a = _host("执行权测试主机-A")
    host_b = _host("执行权测试主机-B")
    session = DatabaseSession(engine)
    try:
        PostgresStationRepository(session).add(station)
        hosts = PostgresInferenceHostRepository(session)
        hosts.add(host_a)
        hosts.add(host_b)
        session.commit()
    finally:
        session.close()
    return station, host_a, host_b


def _cleanup_targets(
    engine: Engine,
    station: Station,
    host_a: InferenceHost,
    host_b: InferenceHost,
) -> None:
    with engine.begin() as connection:
        connection.execute(
            text("DELETE FROM execution_station_grant WHERE station_id = :station_id"),
            {"station_id": station.id},
        )
        connection.execute(
            text("DELETE FROM device_station WHERE id = :station_id"),
            {"station_id": station.id},
        )
        connection.execute(
            text("DELETE FROM device_inference_host WHERE id IN (:host_a, :host_b)"),
            {"host_a": host_a.id, "host_b": host_b.id},
        )


def test_concurrent_acquire_allows_only_one_unexpired_holder(engine: Engine) -> None:
    station, host_a, host_b = _arrange_targets(engine)
    barrier = Barrier(2)
    granted = []
    refused: list[ExecutionRefusalCode] = []

    def contend(host: InferenceHost) -> None:
        session = DatabaseSession(engine)
        try:
            gateway = lease_gateway(session)
            barrier.wait(timeout=5)
            try:
                value = gateway.acquire(
                    station_id=station.id,
                    holder_host_id=host.id,
                    request_id=new_id(),
                    now=NOW,
                )
                session.commit()
                granted.append(value)
            except ExecutionRefusedError as error:
                session.rollback()
                refused.append(error.code)
        finally:
            session.close()

    threads = [Thread(target=contend, args=(host,)) for host in (host_a, host_b)]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        assert all(not thread.is_alive() for thread in threads)
        assert len(granted) == 1
        assert refused == [ExecutionRefusalCode.ACTIVE_GRANT_EXISTS]
        assert granted[0].lease_expires_at - granted[0].renewed_at == SEVEN_DAYS

        with engine.connect() as connection:
            stored = connection.execute(
                text(
                    "SELECT grant_id, holder_host_id, lease_expires_at, renewed_at, request_id "
                    "FROM execution_station_grant WHERE station_id = :station_id"
                ),
                {"station_id": station.id},
            ).one()
        assert stored.holder_host_id == granted[0].holder_host_id
        assert stored.grant_id == granted[0].grant_id
        assert stored.lease_expires_at == granted[0].lease_expires_at
        assert stored.renewed_at == granted[0].renewed_at
        assert stored.request_id == granted[0].request_id
    finally:
        _cleanup_targets(engine, station, host_a, host_b)


def test_expired_acquire_wins_concurrent_stale_renewal_without_overlap(engine: Engine) -> None:
    station, host_a, host_b = _arrange_targets(engine)
    try:
        first_session = DatabaseSession(engine)
        try:
            first = lease_gateway(first_session).acquire(
                station_id=station.id,
                holder_host_id=host_a.id,
                request_id=new_id(),
                now=NOW,
            )
            first_session.commit()
        finally:
            first_session.close()

        barrier = Barrier(2)
        replacement_request = new_id()
        replacements = []
        refused: list[ExecutionRefusalCode] = []
        unexpected: list[BaseException] = []

        def acquire_replacement() -> None:
            session = DatabaseSession(engine)
            try:
                barrier.wait(timeout=5)
                try:
                    value = lease_gateway(session).acquire(
                        station_id=station.id,
                        holder_host_id=host_b.id,
                        request_id=replacement_request,
                        now=first.lease_expires_at,
                    )
                    session.commit()
                    replacements.append(value)
                except BaseException as error:
                    session.rollback()
                    unexpected.append(error)
            finally:
                session.close()

        def renew_stale_grant() -> None:
            session = DatabaseSession(engine)
            try:
                barrier.wait(timeout=5)
                try:
                    lease_gateway(session).renew(
                        station_id=station.id,
                        grant_id=first.grant_id,
                        holder_host_id=host_a.id,
                        request_id=new_id(),
                        now=first.lease_expires_at,
                    )
                    session.commit()
                    unexpected.append(
                        AssertionError("expired grant renewal unexpectedly succeeded")
                    )
                except ExecutionRefusedError as error:
                    session.rollback()
                    refused.append(error.code)
                except BaseException as error:
                    session.rollback()
                    unexpected.append(error)
            finally:
                session.close()

        threads = [Thread(target=acquire_replacement), Thread(target=renew_stale_grant)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        assert all(not thread.is_alive() for thread in threads)
        assert unexpected == []
        assert refused == [ExecutionRefusalCode.GRANT_NOT_RENEWABLE]
        assert len(replacements) == 1
        replacement = replacements[0]
        assert replacement.grant_id != first.grant_id
        assert replacement.holder_host_id == host_b.id
        assert replacement.request_id == replacement_request
        assert replacement.lease_expires_at - replacement.renewed_at == SEVEN_DAYS
    finally:
        _cleanup_targets(engine, station, host_a, host_b)


def test_renewal_keeps_grant_identity_and_resets_exact_seven_day_expiry(engine: Engine) -> None:
    station, host_a, host_b = _arrange_targets(engine)
    try:
        first_session = DatabaseSession(engine)
        try:
            first = lease_gateway(first_session).acquire(
                station_id=station.id,
                holder_host_id=host_a.id,
                request_id=new_id(),
                now=NOW,
            )
            first_session.commit()
        finally:
            first_session.close()

        renewed_at = NOW + timedelta(days=1)
        renewal_request = new_id()
        renewal_session = DatabaseSession(engine)
        try:
            renewed = lease_gateway(renewal_session).renew(
                station_id=station.id,
                grant_id=first.grant_id,
                holder_host_id=host_a.id,
                request_id=renewal_request,
                now=renewed_at,
            )
            renewal_session.commit()
        finally:
            renewal_session.close()

        assert renewed.grant_id == first.grant_id
        assert renewed.station_id == station.id
        assert renewed.holder_host_id == host_a.id
        assert renewed.request_id == renewal_request
        assert renewed.renewed_at == renewed_at
        assert renewed.lease_expires_at == renewed_at + SEVEN_DAYS
    finally:
        _cleanup_targets(engine, station, host_a, host_b)


def test_stale_earlier_renewal_cannot_overwrite_newer_lease(engine: Engine) -> None:
    station, host_a, host_b = _arrange_targets(engine)
    try:
        first_session = DatabaseSession(engine)
        try:
            first = lease_gateway(first_session).acquire(
                station_id=station.id,
                holder_host_id=host_a.id,
                request_id=new_id(),
                now=NOW,
            )
            first_session.commit()
        finally:
            first_session.close()

        later_at = NOW + timedelta(days=2)
        later_request = new_id()
        later_session = DatabaseSession(engine)
        try:
            later = lease_gateway(later_session).renew(
                station_id=station.id,
                grant_id=first.grant_id,
                holder_host_id=host_a.id,
                request_id=later_request,
                now=later_at,
            )
            later_session.commit()
        finally:
            later_session.close()

        stale_session = DatabaseSession(engine)
        try:
            with pytest.raises(ExecutionRefusedError) as refused:
                lease_gateway(stale_session).renew(
                    station_id=station.id,
                    grant_id=first.grant_id,
                    holder_host_id=host_a.id,
                    request_id=new_id(),
                    now=NOW + timedelta(days=1),
                )
            assert refused.value.code is ExecutionRefusalCode.GRANT_NOT_RENEWABLE
            stale_session.rollback()
        finally:
            stale_session.close()

        with engine.connect() as connection:
            stored = connection.execute(
                text(
                    "SELECT lease_expires_at, renewed_at, request_id "
                    "FROM execution_station_grant WHERE station_id = :station_id"
                ),
                {"station_id": station.id},
            ).one()
        assert stored.renewed_at == later.renewed_at
        assert stored.lease_expires_at == later.lease_expires_at
        assert stored.request_id == later_request
    finally:
        _cleanup_targets(engine, station, host_a, host_b)
