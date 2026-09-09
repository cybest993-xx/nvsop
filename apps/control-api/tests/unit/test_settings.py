from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr

from factory_sop.settings import ConfigurationError, Settings


def write_secret(tmp_path: Path, content: str, name: str = "database-password") -> str:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return str(path)


def environment(tmp_path: Path, **overrides: str) -> dict[str, str]:
    """A complete, valid deployment environment, with per-test overrides applied.

    Every test starts from something that loads, so a test that asserts a rejection is
    asserting about the one variable it changed.
    """
    base = {
        "SOP_LOG_LEVEL": "info",
        "SOP_DATABASE_HOST": "postgres.internal",
        "SOP_DATABASE_PORT": "5432",
        "SOP_DATABASE_NAME": "factory_sop",
        "SOP_DATABASE_USER": "factory_sop",
        "SOP_DATABASE_PASSWORD_FILE": write_secret(tmp_path, "hunter2\n"),
        "SOP_SESSION_IDLE_TIMEOUT_MINUTES": "720",
        "SOP_SESSION_ABSOLUTE_LIFETIME_MINUTES": "43200",
        "SOP_SESSION_COOKIE_TRANSPORT": "require_https",
        "SOP_CSRF_SECRET_FILE": write_secret(tmp_path, "csrf-secret\n", name="csrf-secret"),
    }
    base.update(overrides)
    return base


def test_loads_a_complete_environment(tmp_path: Path) -> None:
    settings = Settings.from_environment(environment(tmp_path))

    assert settings == Settings(
        log_level="info",
        database_host="postgres.internal",
        database_port=5432,
        database_name="factory_sop",
        database_user="factory_sop",
        database_password=SecretStr("hunter2"),
        session_idle_timeout_minutes=720,
        session_absolute_lifetime_minutes=43200,
        session_cookie_transport="require_https",
        csrf_secret=SecretStr("csrf-secret"),
    )


def test_the_configured_session_limits_are_loaded_as_minutes(tmp_path: Path) -> None:
    # The two limits are deployment values, not constants: a plant running one shift wants a
    # different idle timeout from one running three. `create_app` turns them into the
    # `SessionPolicy` — this object stays below the domain in the layering.
    settings = Settings.from_environment(
        environment(
            tmp_path,
            SOP_SESSION_IDLE_TIMEOUT_MINUTES="30",
            SOP_SESSION_ABSOLUTE_LIFETIME_MINUTES="480",
        )
    )

    assert settings.session_idle_timeout_minutes == 30
    assert settings.session_absolute_lifetime_minutes == 480


def test_refuses_a_session_lifetime_shorter_than_its_idle_timeout(tmp_path: Path) -> None:
    # The absolute lifetime would then be the only limit that ever fires, so the idle timeout
    # would be configured and inert — the deployment would believe an unattended browser
    # closes when it does not.
    with pytest.raises(ConfigurationError, match="idle timeout"):
        Settings.from_environment(
            environment(
                tmp_path,
                SOP_SESSION_IDLE_TIMEOUT_MINUTES="480",
                SOP_SESSION_ABSOLUTE_LIFETIME_MINUTES="60",
            )
        )


def test_refuses_a_non_positive_session_timeout(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="session_idle_timeout_minutes"):
        Settings.from_environment(environment(tmp_path, SOP_SESSION_IDLE_TIMEOUT_MINUTES="0"))


def test_refuses_a_public_minio_endpoint_without_the_rest_of_minio_config(
    tmp_path: Path,
) -> None:
    with pytest.raises(ConfigurationError, match="must be configured together"):
        Settings.from_environment(
            environment(tmp_path, SOP_MINIO_PUBLIC_ENDPOINT="https://minio.example.test")
        )


def test_accepts_minio_config_without_a_public_endpoint(tmp_path: Path) -> None:
    settings = Settings.from_environment(
        environment(
            tmp_path,
            SOP_MINIO_ENDPOINT="http://minio.internal:9000",
            SOP_MINIO_BUCKET="training",
            SOP_MINIO_ACCESS_KEY_FILE=write_secret(tmp_path, "access", name="minio-access"),
            SOP_MINIO_SECRET_KEY_FILE=write_secret(tmp_path, "secret", name="minio-secret"),
        )
    )

    assert settings.minio_public_endpoint is None
    assert settings.minio_bucket == "training"


def test_refuses_any_cookie_transport_that_allows_plain_http(tmp_path: Path) -> None:
    # §六 has no development exception: every session cookie is Secure. Local development
    # must provide HTTPS rather than turning a production security attribute off.
    with pytest.raises(ConfigurationError, match="session_cookie_transport"):
        Settings.from_environment(environment(tmp_path, SOP_SESSION_COOKIE_TRANSPORT="allow_http"))


def test_keeps_the_csrf_secret_out_of_the_repr(tmp_path: Path) -> None:
    settings = Settings.from_environment(environment(tmp_path))

    assert "csrf-secret" not in repr(settings)


def test_reads_a_secret_from_the_path_variable_and_strips_the_trailing_newline(
    tmp_path: Path,
) -> None:
    settings = Settings.from_environment(environment(tmp_path))

    assert settings.database_password.get_secret_value() == "hunter2"


def test_refuses_a_secret_passed_by_value(tmp_path: Path) -> None:
    # §六: secrets arrive as a path to a file, never as the value itself. Accepting the
    # value form would make the insecure deployment the quiet one.
    with pytest.raises(ConfigurationError, match="SOP_DATABASE_PASSWORD"):
        Settings.from_environment(
            environment(tmp_path, SOP_DATABASE_PASSWORD="hunter2")  # pragma: allowlist secret
        )


def test_refuses_to_start_when_a_secret_file_is_missing(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="does not exist"):
        Settings.from_environment(
            environment(tmp_path, SOP_DATABASE_PASSWORD_FILE=str(tmp_path / "absent"))
        )


def test_refuses_to_start_on_an_empty_secret_file(tmp_path: Path) -> None:
    # An empty file is the shape a half-finished deployment produces. Starting anyway would
    # be the "degrade to unencrypted" path §六 forbids.
    with pytest.raises(ConfigurationError, match="is empty"):
        Settings.from_environment(
            environment(
                tmp_path,
                SOP_DATABASE_PASSWORD_FILE=write_secret(tmp_path, "\n", name="empty"),
            )
        )


def test_refuses_to_start_when_a_required_variable_is_absent(tmp_path: Path) -> None:
    incomplete = environment(tmp_path)
    del incomplete["SOP_DATABASE_HOST"]

    with pytest.raises(ConfigurationError, match="database_host"):
        Settings.from_environment(incomplete)


def test_refuses_an_unrecognized_variable(tmp_path: Path) -> None:
    # A typo in a deployment variable is otherwise silent: the intended value never
    # arrives and the default takes over.
    with pytest.raises(ConfigurationError, match="SOP_DATABSE_HOST"):
        Settings.from_environment(environment(tmp_path, SOP_DATABSE_HOST="postgres.internal"))


def test_refuses_an_unknown_log_level(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="log_level"):
        Settings.from_environment(environment(tmp_path, SOP_LOG_LEVEL="chatty"))


def test_keeps_the_secret_out_of_the_repr(tmp_path: Path) -> None:
    settings = Settings.from_environment(environment(tmp_path))

    assert "hunter2" not in repr(settings)
