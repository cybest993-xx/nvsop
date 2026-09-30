from __future__ import annotations

import pytest
from arq.connections import RedisSettings
from pydantic import SecretStr

from factory_sop.job.adapters.dispatcher import ArqJobDispatcher
from factory_sop.settings import ConfigurationError, Settings


def settings(redis_url: str | None) -> Settings:
    return Settings(
        log_level="warning",
        database_host="unused",
        database_port=5432,
        database_name="unused",
        database_user="unused",
        database_password=SecretStr("unused"),
        session_idle_timeout_minutes=720,
        session_absolute_lifetime_minutes=43200,
        session_cookie_transport="require_https",
        csrf_secret=SecretStr("csrf-secret"),
        redis_url=SecretStr(redis_url) if redis_url is not None else None,
    )


@pytest.mark.parametrize(
    "redis_url",
    [
        "redis://127.0.0.1:0/0",
        "redis://127.0.0.1/-1",
        "redis://127.0.0.1/not-a-number",
        "redis://127.0.0.1:70000/0",
        "http://127.0.0.1:6379/0",
        "redis://[::1/0",
    ],
)
def test_dispatcher_rejects_invalid_redis_configuration(redis_url: str) -> None:
    with pytest.raises(ConfigurationError):
        ArqJobDispatcher.from_settings(settings(redis_url))


def test_dispatcher_rejects_missing_redis_configuration() -> None:
    with pytest.raises(ConfigurationError):
        ArqJobDispatcher.from_settings(settings(None))


def test_dispatcher_builds_redis_settings_from_the_shared_parse_rule() -> None:
    dispatcher = ArqJobDispatcher.from_settings(
        settings("redis://operator:secret@redis.internal:6380/3")  # pragma: allowlist secret
    )

    assert dispatcher.redis_settings == RedisSettings(
        host="redis.internal",
        port=6380,
        database=3,
        username="operator",
        password="secret",  # pragma: allowlist secret
        ssl=False,
    )
