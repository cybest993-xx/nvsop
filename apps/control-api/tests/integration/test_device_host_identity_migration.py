"""0017 身份迁移的真实 PostgreSQL 升级、失败和回退证据。"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, inspect, text

CONTROL_API = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 9, 8, 8, 0, tzinfo=UTC)
LEGACY_HASH = "a" * 64


def _configuration(database: Engine) -> Config:
    configuration = Config(str(CONTROL_API / "alembic.ini"))
    configuration.set_main_option("script_location", str(CONTROL_API / "migrations"))
    configuration.set_main_option(
        "sqlalchemy.url", database.url.render_as_string(hide_password=False)
    )
    return configuration


@pytest.fixture
def migration_database(engine: Engine) -> Iterator[Engine]:
    """为每个迁移测试创建独立数据库，避免改变 session fixture 的 head schema。"""
    installer = create_engine(
        engine.url._replace(database="postgres"), isolation_level="AUTOCOMMIT"
    )
    database_name = f"nvsop_host_identity_migration_{uuid4().hex[:12]}"
    with installer.connect() as connection:
        connection.exec_driver_sql(f'DROP DATABASE IF EXISTS "{database_name}"')
        connection.exec_driver_sql(f'CREATE DATABASE "{database_name}"')
    installer.dispose()

    migrated = create_engine(engine.url._replace(database=database_name))
    try:
        yield migrated
    finally:
        migrated.dispose()
        with create_engine(
            engine.url._replace(database="postgres"), isolation_level="AUTOCOMMIT"
        ).connect() as connection:
            connection.exec_driver_sql(f'DROP DATABASE "{database_name}" WITH (FORCE)')


def _insert_host(
    database: Engine,
    *,
    host_id: UUID,
    legacy_hash: str | None = None,
    public_key: str | None = None,
) -> None:
    columns = [
        "id",
        "name",
        "address",
        "mediamtx_address",
        "recording_window_seconds",
        "disk_watermark_percent",
        "status",
        "revision",
        "created_by",
        "updated_by",
        "created_at",
        "updated_at",
    ]
    values: dict[str, object] = {
        "id": host_id,
        "name": f"迁移推理机-{host_id.hex[:8]}",
        "address": "10.0.8.11",
        "mediamtx_address": None,
        "recording_window_seconds": 604800,
        "disk_watermark_percent": 85,
        "status": "active",
        "revision": 1,
        "created_by": uuid4(),
        "updated_by": uuid4(),
        "created_at": NOW,
        "updated_at": NOW,
    }
    if legacy_hash is not None:
        columns.append("credential_hash")
        values["credential_hash"] = legacy_hash
    if public_key is not None:
        columns.append("identity_public_key")
        values["identity_public_key"] = public_key
    placeholders = ", ".join(f":{column}" for column in columns)
    with database.begin() as connection:
        connection.execute(
            text(
                f"INSERT INTO device_inference_host ({', '.join(columns)}) VALUES ({placeholders})"
            ),
            values,
        )


def _version(database: Engine) -> str:
    with database.connect() as connection:
        return str(connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one())


def test_upgrade_0017_removes_empty_legacy_identity_and_leaves_hosts_unconfigured(
    migration_database: Engine,
) -> None:
    configuration = _configuration(migration_database)
    command.upgrade(configuration, "0016")
    host_id = uuid4()
    _insert_host(migration_database, host_id=host_id, legacy_hash="")

    command.upgrade(configuration, "0017")

    with migration_database.connect() as connection:
        inspector = inspect(connection)
        host_columns = {column["name"] for column in inspector.get_columns("device_inference_host")}
        pending_columns = {
            column["name"] for column in inspector.get_columns("device_pending_command")
        }
        assert "credential_hash" not in host_columns
        assert "identity_public_key" in host_columns
        assert "result_credentials_configured" in pending_columns
        assert inspector.has_table("device_inference_host_identity_nonce")
        assert (
            connection.execute(
                text("SELECT identity_public_key FROM device_inference_host WHERE id = :id"),
                {"id": host_id},
            ).scalar_one()
            is None
        )

    assert _version(migration_database) == "0017"


def test_upgrade_0017_fails_atomically_when_a_legacy_identity_is_configured(
    migration_database: Engine,
) -> None:
    configuration = _configuration(migration_database)
    command.upgrade(configuration, "0016")
    host_id = uuid4()
    _insert_host(migration_database, host_id=host_id, legacy_hash=LEGACY_HASH)

    with pytest.raises(RuntimeError, match="credential_hash"):
        command.upgrade(configuration, "0017")

    with migration_database.connect() as connection:
        inspector = inspect(connection)
        host_columns = {column["name"] for column in inspector.get_columns("device_inference_host")}
        assert _version(migration_database) == "0016"
        assert "credential_hash" in host_columns
        assert "identity_public_key" not in host_columns
        assert not inspector.has_table("device_inference_host_identity_nonce")
        assert (
            connection.execute(
                text("SELECT credential_hash FROM device_inference_host WHERE id = :id"),
                {"id": host_id},
            ).scalar_one()
            == LEGACY_HASH
        )


def test_upgrade_0017_fails_for_mixed_empty_and_configured_legacy_identities(
    migration_database: Engine,
) -> None:
    configuration = _configuration(migration_database)
    command.upgrade(configuration, "0016")
    empty_host = uuid4()
    configured_host = uuid4()
    _insert_host(migration_database, host_id=empty_host, legacy_hash="")
    _insert_host(migration_database, host_id=configured_host, legacy_hash=LEGACY_HASH)

    with pytest.raises(RuntimeError, match="credential_hash"):
        command.upgrade(configuration, "0017")

    assert _version(migration_database) == "0016"


def test_downgrade_0017_recreates_empty_legacy_identity_and_can_upgrade_again(
    migration_database: Engine,
) -> None:
    configuration = _configuration(migration_database)
    command.upgrade(configuration, "0017")
    host_id = uuid4()
    _insert_host(migration_database, host_id=host_id)

    command.downgrade(configuration, "0016")

    with migration_database.connect() as connection:
        inspector = inspect(connection)
        host_columns = {column["name"] for column in inspector.get_columns("device_inference_host")}
        legacy = next(
            column
            for column in inspector.get_columns("device_inference_host")
            if column["name"] == "credential_hash"
        )
        assert _version(migration_database) == "0016"
        assert "identity_public_key" not in host_columns
        assert not inspector.has_table("device_inference_host_identity_nonce")
        assert legacy["nullable"] is False
        assert getattr(legacy["type"], "length", None) == 64
        assert legacy["default"] is not None
        assert (
            connection.execute(
                text("SELECT credential_hash FROM device_inference_host WHERE id = :id"),
                {"id": host_id},
            ).scalar_one()
            == ""
        )

    command.upgrade(configuration, "0017")
    with migration_database.connect() as connection:
        assert _version(migration_database) == "0017"
        assert (
            connection.execute(
                text("SELECT identity_public_key FROM device_inference_host WHERE id = :id"),
                {"id": host_id},
            ).scalar_one()
            is None
        )


def test_downgrade_0017_refuses_to_drop_a_configured_public_key(
    migration_database: Engine,
) -> None:
    configuration = _configuration(migration_database)
    command.upgrade(configuration, "0017")
    host_id = uuid4()
    _insert_host(migration_database, host_id=host_id, public_key="synthetic-public-key")

    with pytest.raises(RuntimeError, match="identity_public_key"):
        command.downgrade(configuration, "0016")

    with migration_database.connect() as connection:
        inspector = inspect(connection)
        host_columns = {column["name"] for column in inspector.get_columns("device_inference_host")}
        assert _version(migration_database) == "0017"
        assert "identity_public_key" in host_columns
        assert "credential_hash" not in host_columns
        assert (
            connection.execute(
                text("SELECT identity_public_key FROM device_inference_host WHERE id = :id"),
                {"id": host_id},
            ).scalar_one()
            == "synthetic-public-key"
        )
