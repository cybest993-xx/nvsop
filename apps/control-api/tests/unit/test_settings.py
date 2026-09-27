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
        "SOP_DATASET_STORAGE_ROOT": str(tmp_path / "dataset-files"),
        "SOP_REDIS_URL_FILE": write_secret(
            tmp_path, "redis://redis.internal:6379/0\n", name="redis-url"
        ),
        "SOP_DATASET_UPLOAD_TTL_SECONDS": "900",
        "SOP_DATASET_MAX_UPLOAD_BYTES": str(8 * 1024**3),
        "SOP_DATASET_SUPPORTED_CODECS": "h264,h265",
        "SOP_MEDIA_PROBE_BINARY": "ffprobe",
        "SOP_MEDIA_PROBE_TIMEOUT_SECONDS": "60",
        "SOP_ANNOTATION_DATA_ROOT": str(tmp_path / "annotation-data"),
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
        dataset_storage_root=str(tmp_path / "dataset-files"),
        redis_url=SecretStr("redis://redis.internal:6379/0"),
        annotation_data_root=str(tmp_path / "annotation-data"),
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


def test_refuses_a_deployment_without_dataset_runtime_configuration(tmp_path: Path) -> None:
    values = environment(tmp_path)
    del values["SOP_DATASET_UPLOAD_TTL_SECONDS"]
    with pytest.raises(ConfigurationError, match="SOP_DATASET_UPLOAD_TTL_SECONDS"):
        Settings.from_environment(values)


def test_refuses_a_deployment_without_dataset_storage_root(tmp_path: Path) -> None:
    values = environment(tmp_path)
    del values["SOP_DATASET_STORAGE_ROOT"]
    with pytest.raises(ConfigurationError, match="训练素材本地存储根目录"):
        Settings.from_environment(values)


def test_refuses_a_deployment_without_redis(tmp_path: Path) -> None:
    values = environment(tmp_path)
    del values["SOP_REDIS_URL_FILE"]
    with pytest.raises(ConfigurationError, match="Redis"):
        Settings.from_environment(values)


def test_refuses_an_invalid_redis_url(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="redis_url"):
        Settings.from_environment(
            environment(
                tmp_path,
                SOP_REDIS_URL_FILE=write_secret(
                    tmp_path, "http://redis.internal\n", name="invalid-redis-url"
                ),
            )
        )


def test_refuses_a_relative_dataset_storage_root(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="dataset_storage_root"):
        Settings.from_environment(
            environment(tmp_path, SOP_DATASET_STORAGE_ROOT="relative/dataset-files")
        )


def test_refuses_an_empty_codec_list(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="dataset_supported_codecs"):
        Settings.from_environment(environment(tmp_path, SOP_DATASET_SUPPORTED_CODECS=",, "))


def test_refuses_a_negative_redis_database(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="redis_url"):
        Settings.from_environment(
            environment(
                tmp_path,
                SOP_REDIS_URL_FILE=write_secret(
                    tmp_path, "redis://redis.internal/-1\n", name="negative-redis-db"
                ),
            )
        )


def test_accepts_annotation_backend_with_a_same_host_https_media_origin(tmp_path: Path) -> None:
    settings = Settings.from_environment(
        environment(
            tmp_path,
            SOP_ANNOTATION_BACKEND_URL="http://annotation-backend.internal:8000",
            SOP_ANNOTATION_MEDIA_ORIGIN="https://sop.example.internal:8444",
        )
    )

    assert settings.annotation_backend_url == "http://annotation-backend.internal:8000"
    assert settings.annotation_media_origin == "https://sop.example.internal:8444"


def test_accepts_annotation_media_origin_over_http_for_fixed_main_development(
    tmp_path: Path,
) -> None:
    settings = Settings.from_environment(
        environment(
            tmp_path,
            SOP_DEPLOYMENT_MODE="fixed_main",
            SOP_SESSION_COOKIE_TRANSPORT="allow_http",
            SOP_ANNOTATION_BACKEND_URL="http://annotation-backend.internal:8000",
            SOP_ANNOTATION_MEDIA_ORIGIN="http://localhost:8444",
        )
    )

    assert settings.annotation_media_origin == "http://localhost:8444"
    assert settings.deployment_mode == "fixed_main"


def test_refuses_http_annotation_media_origin_in_secure_mode(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="allow_http"):
        Settings.from_environment(
            environment(
                tmp_path,
                SOP_ANNOTATION_BACKEND_URL="http://annotation-backend.internal:8000",
                SOP_ANNOTATION_MEDIA_ORIGIN="http://localhost:8444",
            )
        )


@pytest.mark.parametrize(
    "backend_url",
    [
        "ftp://annotation-backend.internal:8000",
        "http://user:password@annotation-backend.internal:8000",  # pragma: allowlist secret
        "http://annotation-backend.internal:8000?tenant=one",
    ],
)
def test_refuses_an_unsafe_annotation_backend_url(tmp_path: Path, backend_url: str) -> None:
    with pytest.raises(ConfigurationError, match="annotation_backend_url"):
        Settings.from_environment(
            environment(
                tmp_path,
                SOP_ANNOTATION_BACKEND_URL=backend_url,
                SOP_ANNOTATION_MEDIA_ORIGIN="https://sop.example.internal:8444",
            )
        )


@pytest.mark.parametrize(
    "origin",
    [
        "ftp://sop.example.internal:8444",
        "https://sop.example.internal/annotation",
        "https://sop.example.internal:8444?resource=video",
        "https://sop.example.internal:99999",
    ],
)
def test_refuses_an_unsafe_annotation_media_origin(tmp_path: Path, origin: str) -> None:
    with pytest.raises(ConfigurationError, match="annotation_media_origin"):
        Settings.from_environment(
            environment(
                tmp_path,
                SOP_ANNOTATION_BACKEND_URL="http://annotation-backend.internal:8000",
                SOP_ANNOTATION_MEDIA_ORIGIN=origin,
            )
        )


def test_refuses_plain_http_cookie_transport_outside_fixed_main(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="fixed_main"):
        Settings.from_environment(environment(tmp_path, SOP_SESSION_COOKIE_TRANSPORT="allow_http"))


def test_refuses_plain_http_cookie_transport_for_non_loopback_fixed_main(
    tmp_path: Path,
) -> None:
    with pytest.raises(ConfigurationError, match="localhost:8444"):
        Settings.from_environment(
            environment(
                tmp_path,
                SOP_DEPLOYMENT_MODE="fixed_main",
                SOP_SESSION_COOKIE_TRANSPORT="allow_http",
                SOP_ANNOTATION_BACKEND_URL="http://annotation-backend.internal:8000",
                SOP_ANNOTATION_MEDIA_ORIGIN="http://annotation.internal:8444",
            )
        )


def test_accepts_plain_http_cookie_transport_only_for_fixed_main(tmp_path: Path) -> None:
    settings = Settings.from_environment(
        environment(
            tmp_path,
            SOP_DEPLOYMENT_MODE="fixed_main",
            SOP_SESSION_COOKIE_TRANSPORT="allow_http",
            SOP_ANNOTATION_BACKEND_URL="http://annotation-backend.internal:8000",
            SOP_ANNOTATION_MEDIA_ORIGIN="http://localhost:8444",
        )
    )

    assert settings.session_cookie_transport == "allow_http"


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
