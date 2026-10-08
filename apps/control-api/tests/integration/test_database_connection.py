"""合成特殊字符凭据通过应用与部署 Alembic 入口连接真实 PostgreSQL。"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from psycopg import sql
from sqlalchemy import Engine, text
from sqlalchemy.exc import OperationalError

from factory_sop.persistence import create_database_engine
from factory_sop.settings import Settings

CONTROL_API = Path(__file__).resolve().parents[2]


def test_application_and_migration_preserve_reserved_credentials(
    engine: Engine, tmp_path: Path
) -> None:
    suffix = uuid4().hex[:8]
    username = f"s153@%/user-{suffix}"
    database = f"s153@%/db-{suffix}"
    password = "synthetic@%/password:153"  # pragma: allowlist secret
    environment = {key: value for key, value in os.environ.items() if not key.startswith("SOP_")}
    for name, value in (
        ("DATABASE_PASSWORD", password),
        ("CSRF_SECRET", "synthetic-csrf"),
        ("REDIS_URL", "redis://127.0.0.1:1/0"),
    ):
        path = tmp_path / name.lower()
        path.write_text(value, encoding="utf-8")
        environment[f"SOP_{name}_FILE"] = str(path)
    environment.update(
        {
            "SOP_LOG_LEVEL": "warning",
            "SOP_SESSION_IDLE_TIMEOUT_MINUTES": "720",
            "SOP_SESSION_ABSOLUTE_LIFETIME_MINUTES": "43200",
            "SOP_SESSION_COOKIE_TRANSPORT": "require_https",
            "SOP_DATABASE_HOST": str(engine.url.host),
            "SOP_DATABASE_PORT": str(engine.url.port),
            "SOP_DATABASE_USER": username,
            "SOP_DATABASE_NAME": database,
            "SOP_DATASET_STORAGE_ROOT": str(tmp_path / "datasets"),
            "SOP_DATASET_UPLOAD_TTL_SECONDS": "900",
            "SOP_DATASET_MAX_UPLOAD_BYTES": str(8 * 1024**3),
            "SOP_DATASET_SUPPORTED_CODECS": "h264,h265",
            "SOP_MEDIA_PROBE_BINARY": "ffprobe",
            "SOP_MEDIA_PROBE_TIMEOUT_SECONDS": "60",
        }
    )
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as admin:
        connection = admin.connection.driver_connection
        assert connection is not None
        with connection.cursor() as cursor:
            cursor.execute(
                sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                    sql.Identifier(username),
                    sql.Literal(password),
                )
            )
            cursor.execute(
                sql.SQL("CREATE DATABASE {} OWNER {}").format(
                    sql.Identifier(database),
                    sql.Identifier(username),
                )
            )
        application: Engine | None = None
        try:
            application = create_database_engine(Settings.from_environment(environment))
            with application.connect() as opened:
                assert opened.execute(text("SELECT current_user, current_database()")).one() == (
                    username,
                    database,
                )
            migrated = subprocess.run(
                [sys.executable, "-m", "alembic", "upgrade", "head"],
                cwd=CONTROL_API,
                env=environment,
                capture_output=True,
                text=True,
                timeout=60,
            )
            assert password not in migrated.stdout + migrated.stderr
            assert migrated.returncode == 0, migrated.stderr
            with application.connect() as opened:
                assert opened.scalar(text("SELECT count(*) FROM alembic_version")) == 1
                assert opened.scalar(text("SELECT count(*) FROM auth_user")) == 0
            wrong_password = "incorrect@%/synthetic:153"  # pragma: allowlist secret
            Path(environment["SOP_DATABASE_PASSWORD_FILE"]).write_text(wrong_password)
            rejected = create_database_engine(Settings.from_environment(environment))
            try:
                with pytest.raises(OperationalError) as failure, rejected.connect():
                    pytest.fail("错误密码不应成功建立连接")
                assert wrong_password not in str(failure.value)
                assert "postgresql+psycopg://" not in str(failure.value)
            finally:
                rejected.dispose()
            refused_migration = subprocess.run(
                [sys.executable, "-m", "alembic", "upgrade", "head"],
                cwd=CONTROL_API,
                env=environment,
                capture_output=True,
                text=True,
                timeout=60,
            )
            assert refused_migration.returncode != 0
            assert wrong_password not in refused_migration.stdout + refused_migration.stderr
            assert (
                "postgresql+psycopg://" not in refused_migration.stdout + refused_migration.stderr
            )
        finally:
            if application is not None:
                application.dispose()
            with connection.cursor() as cursor:
                cursor.execute(
                    sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database))
                )
                cursor.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(username)))
