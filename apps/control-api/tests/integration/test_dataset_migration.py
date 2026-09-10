"""用真实 PostgreSQL 走过 0023 → 0024 的训练数据集迁移。"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, text

CONTROL_API = Path(__file__).resolve().parents[2]


@pytest.fixture
def database_at_0023(engine: Engine) -> Iterator[Engine]:
    """在独立数据库中保留 0023 状态，再由测试执行升级。"""
    server = engine.url._replace(database="postgres")
    installer = create_engine(server, isolation_level="AUTOCOMMIT")
    database_name = f"nvsop_migration_{uuid4().hex[:12]}"
    with installer.connect() as connection:
        connection.exec_driver_sql(f'DROP DATABASE IF EXISTS "{database_name}"')
        connection.exec_driver_sql(f'CREATE DATABASE "{database_name}"')

    upgraded = create_engine(engine.url._replace(database=database_name))
    configuration = Config(str(CONTROL_API / "alembic.ini"))
    configuration.set_main_option("script_location", str(CONTROL_API / "migrations"))
    configuration.set_main_option(
        "sqlalchemy.url", upgraded.url.render_as_string(hide_password=False)
    )
    command.upgrade(configuration, "0023")
    try:
        yield upgraded
    finally:
        upgraded.dispose()
        with installer.connect() as connection:
            connection.exec_driver_sql(f'DROP DATABASE IF EXISTS "{database_name}"')
        installer.dispose()


def _tables(database: Engine) -> set[str]:
    with database.connect() as connection:
        return set(
            connection.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))
            .scalars()
            .all()
        )


def _columns(database: Engine, table: str) -> set[str]:
    with database.connect() as connection:
        return set(
            connection.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = :table"
                ),
                {"table": table},
            )
            .scalars()
            .all()
        )


def test_training_dataset_migration_upgrades_and_rolls_back_on_real_postgres(
    database_at_0023: Engine,
) -> None:
    assert "job_application_job" in _tables(database_at_0023)
    assert not {"dataset_training_dataset", "dataset_member", "dataset_upload_attempt"} & _tables(
        database_at_0023
    )

    configuration = Config(str(CONTROL_API / "alembic.ini"))
    configuration.set_main_option("script_location", str(CONTROL_API / "migrations"))
    configuration.set_main_option(
        "sqlalchemy.url", database_at_0023.url.render_as_string(hide_password=False)
    )
    command.upgrade(configuration, "head")

    assert {"dataset_training_dataset", "dataset_member", "dataset_upload_attempt"} <= _tables(
        database_at_0023
    )
    assert {
        "id",
        "dataset_id",
        "original_filename",
        "declared_size",
        "declared_sha256",
        "current_attempt_id",
        "actual_size",
        "actual_sha256",
        "duration_seconds",
        "codec",
        "container",
        "object_key",
        "validation_job_id",
        "failure_code",
        "recovery_action",
    } <= _columns(database_at_0023, "dataset_member")
    assert {
        "id",
        "dataset_id",
        "member_id",
        "idempotency_key",
        "object_key",
        "declared_size",
        "declared_sha256",
        "expires_at",
        "status",
        "validation_job_id",
        "object_version_id",
    } <= _columns(database_at_0023, "dataset_upload_attempt")

    with database_at_0023.connect() as connection:
        version = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    assert version == "0024"

    command.downgrade(configuration, "0023")
    assert not {"dataset_training_dataset", "dataset_member", "dataset_upload_attempt"} & _tables(
        database_at_0023
    )
