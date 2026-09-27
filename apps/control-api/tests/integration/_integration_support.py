"""真实 control-api 集成测试共用的最小组合根。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx2 import Response
from pydantic import SecretStr
from sqlalchemy import Engine, text

from factory_sop.app import create_app
from factory_sop.auth.adapters import dependencies as auth_dependencies
from factory_sop.auth.adapters.cookies import CSRF_COOKIE, CSRF_HEADER
from factory_sop.auth.authorization import Caller
from factory_sop.auth.model import Session, User, UserStatus
from factory_sop.auth.permissions import Permission
from factory_sop.auth.usecases.sessions import RestoredSession
from factory_sop.job.adapters.dispatcher import ArqJobDispatcher
from factory_sop.persistence import session_factory
from factory_sop.settings import Settings


@dataclass(frozen=True, slots=True)
class RedisServer:
    """由测试容器提供的临时 Redis 端点。"""

    url: str
    host: str
    port: int


ACTOR_ID = UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f301")
ACTOR_SESSION_ID = UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f302")
NOW = datetime(2026, 9, 9, 1, 0, tzinfo=UTC)


def settings_for(
    engine: Engine,
    *,
    storage_root: Path,
    redis_url: str | None = None,
    upload_ttl_seconds: int = 900,
) -> Settings:
    """用 fixture 参数构造配置，不读取或修改进程环境。"""
    url = engine.url
    assert url.host is not None
    assert url.port is not None
    assert url.database is not None
    assert url.username is not None
    assert url.password is not None
    values: dict[str, object] = {
        "log_level": "warning",
        "database_host": url.host,
        "database_port": url.port,
        "database_name": url.database,
        "database_user": url.username,
        "database_password": SecretStr(url.password),
        "session_idle_timeout_minutes": 720,
        "session_absolute_lifetime_minutes": 43200,
        "session_cookie_transport": "require_https",
        "csrf_secret": SecretStr("integration-csrf-secret"),
        "dataset_storage_root": str(storage_root),
        "dataset_upload_ttl_seconds": upload_ttl_seconds,
        "dataset_supported_codecs": "h264,h265",
        "media_probe_binary": "ffprobe",
        "media_probe_timeout_seconds": 60,
        "redis_url": SecretStr(redis_url or "redis://127.0.0.1:1/0"),
    }
    return Settings.model_validate(values)


def restored_session() -> RestoredSession:
    """给数据集 HTTP 测试使用的真实路由认证边界内 caller。"""
    return RestoredSession(
        session=Session(
            id=ACTOR_SESSION_ID,
            user_id=ACTOR_ID,
            token_fingerprint="integration-token",
            created_at=NOW,
            last_used_at=NOW,
        ),
        user=User(
            id=ACTOR_ID,
            login_name="dataset-integration",
            display_name="数据集集成测试",
            password_hash="integration-only",  # pragma: allowlist secret
            status=UserStatus.ACTIVE,
        ),
    )


def caller() -> Caller:
    """给直接调用用例的并发测试提供带导入权限的 caller。"""
    return Caller(
        user=restored_session().user,
        granted=frozenset({Permission.DATASET_IMPORT, Permission.DATASET_VIEW}),
    )


def build_app(
    engine: Engine,
    settings: Settings,
    *,
    permissions: frozenset[Permission] | None = None,
) -> FastAPI:
    """装配生产 app，仅把认证 caller 固定到测试 actor。"""
    app = create_app(settings)
    factory = session_factory(engine)
    app.state.session_factory = factory
    app.state.job_dispatcher = ArqJobDispatcher.from_settings(settings, session_factory=factory)
    app.dependency_overrides[auth_dependencies.authenticated_caller] = restored_session
    app.dependency_overrides[auth_dependencies.granted_permissions] = lambda: (
        permissions or frozenset({Permission.DATASET_IMPORT, Permission.DATASET_VIEW})
    )
    return app


@contextmanager
def client_for(
    engine: Engine,
    settings: Settings,
    *,
    permissions: frozenset[Permission] | None = None,
) -> Iterator[TestClient]:
    """以真实 FastAPI 路由、请求事务和 PostgreSQL session 运行测试。"""
    with TestClient(
        build_app(engine, settings, permissions=permissions), base_url="https://testserver"
    ) as client:
        yield client


def upload_video_content(client: TestClient, upload: dict[str, Any], content: bytes) -> Response:
    """通过正式中心入口流式上传视频体，不绕过授权、会话或 CSRF 边界。"""
    headers = {str(name): str(value) for name, value in dict(upload["headers"]).items()}
    csrf = client.cookies.get(CSRF_COOKIE)
    if isinstance(csrf, str):
        headers[CSRF_HEADER] = csrf
    return client.put(str(upload["url"]), content=content, headers=headers)


def cleanup_dataset(engine: Engine, dataset_id: UUID) -> None:
    """删除本测试创建的业务行，保留迁移建立的 schema 和参考数据。"""
    with engine.begin() as connection:
        connection.execute(
            text(
                "DELETE FROM job_application_job "
                "WHERE dataset_id = :dataset_id OR member_id IN "
                "(SELECT id FROM dataset_member WHERE dataset_id = :dataset_id)"
            ),
            {"dataset_id": dataset_id},
        )
        connection.execute(
            text("DELETE FROM dataset_training_dataset WHERE id = :dataset_id"),
            {"dataset_id": dataset_id},
        )


def row(engine: Engine, query: str, **parameters: object) -> tuple[object, ...]:
    """读取一个集成断言所需的已提交数据库事实。"""
    with engine.connect() as connection:
        return tuple(connection.execute(text(query), parameters).one())
