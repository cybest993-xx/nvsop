"""SYS-22-01 — bootstrap makes login reachable.

前置：全新部署，`auth_user` 为空，无任何会话。
动作：以部署环境运行 bootstrap 命令创建首个操作员；用其凭据 `POST /api/v1/auth/session`。
可观察结果：命令退出码 0 且在其输出留下 `auth.bootstrap.created` 诊断行；登录 201，
`Set-Cookie` 两枚（HttpOnly 会话 + 可读 CSRF），`expires_at` 为 UTC RFC3339 的 `Z` 形；
`auth_user` 与 `auth_session` 各一行，且已提交持久。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from factory_sop.bootstrap import main

BOOTSTRAP_PASSWORD = "first-shift-key"
BOOTSTRAP_LOGIN = "wang.admin"


@pytest.fixture
def deployment_environ(engine: Engine, tmp_path: Path) -> dict[str, str]:
    """The environment a real deployment runs the command with, aimed at the container."""
    database_password = tmp_path / "database-password"
    database_password.write_text(f"{engine.url.password}\n", encoding="utf-8")
    bootstrap_password = tmp_path / "bootstrap-password"
    bootstrap_password.write_text(f"{BOOTSTRAP_PASSWORD}\n", encoding="utf-8")
    csrf_secret = tmp_path / "csrf-secret"
    csrf_secret.write_text("csrf-secret\n", encoding="utf-8")
    url = engine.url
    assert url.host is not None
    assert url.port is not None
    assert url.database is not None
    assert url.username is not None
    return {
        "SOP_LOG_LEVEL": "info",
        "SOP_DATABASE_HOST": url.host,
        "SOP_DATABASE_PORT": str(url.port),
        "SOP_DATABASE_NAME": url.database,
        "SOP_DATABASE_USER": url.username,
        "SOP_DATABASE_PASSWORD_FILE": str(database_password),
        "SOP_SESSION_IDLE_TIMEOUT_MINUTES": "720",
        "SOP_SESSION_ABSOLUTE_LIFETIME_MINUTES": "43200",
        "SOP_SESSION_COOKIE_TRANSPORT": "require_https",
        "SOP_CSRF_SECRET_FILE": str(csrf_secret),
    }


def bootstrap_argv(tmp_path: Path) -> list[str]:
    return [
        "--login-name",
        BOOTSTRAP_LOGIN,
        "--display-name",
        "王管理员",
        "--password-file",
        str(tmp_path / "bootstrap-password"),
    ]


def test_the_fresh_deployment_is_enterable(
    client: TestClient,
    engine: Engine,
    deployment_environ: dict[str, str],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(argv=bootstrap_argv(tmp_path), environ=deployment_environ) == 0
    # The command owns its diagnostic stream like a real process does — it configured its own
    # renderer, so its line is read from its output, not from the application's capture.
    assert "auth.bootstrap.created" in capsys.readouterr().out

    response = client.post(
        "/api/v1/auth/session",
        json={"login_name": BOOTSTRAP_LOGIN, "password": BOOTSTRAP_PASSWORD},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["login_name"] == BOOTSTRAP_LOGIN
    assert body["expires_at"].endswith("Z")
    cookies = response.headers.get_list("set-cookie")
    assert len(cookies) == 2
    assert any("httponly" in cookie.lower() for cookie in cookies)

    with engine.begin() as connection:
        users = connection.execute(text("select count(*) from auth_user")).scalar_one()
        sessions = connection.execute(text("select count(*) from auth_session")).scalar_one()
    assert users == 1
    assert sessions == 1


def test_running_the_command_again_skips(
    deployment_environ: dict[str, str],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(argv=bootstrap_argv(tmp_path), environ=deployment_environ) == 0
    capsys.readouterr()

    assert main(argv=bootstrap_argv(tmp_path), environ=deployment_environ) == 0

    # The restart with the deployment's credentials still set skips rather than mints: the
    # only line is the skip, and nothing was created a second time.
    output = capsys.readouterr().out
    assert "auth.bootstrap.skipped" in output
    assert "auth.bootstrap.created" not in output
