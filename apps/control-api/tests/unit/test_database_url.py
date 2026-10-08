"""数据库连接字段保真和默认显示的秘密保护。"""

from pathlib import Path

import pytest
from sqlalchemy.engine import make_url
from test_settings import environment, write_secret

from factory_sop.persistence import database_url
from factory_sop.settings import Settings


@pytest.mark.parametrize("reserved", ["@", "%", "/", ":?#[]"])
def test_database_url_preserves_resolved_fields(tmp_path: Path, reserved: str) -> None:
    password = f"synthetic{reserved}password"
    user = f"operator{reserved}name"
    database = f"factory{reserved}data"
    settings = Settings.from_environment(
        environment(
            tmp_path,
            SOP_DATABASE_USER=user,
            SOP_DATABASE_NAME=database,
            SOP_DATABASE_PASSWORD_FILE=write_secret(tmp_path, password, name="special-password"),
        )
    )
    url = make_url(database_url(settings))
    assert (url.username, url.password, url.host, url.port, url.database) == (
        user,
        password,
        "postgres.internal",
        5432,
        database,
    )
    assert password not in str(database_url(settings))
    assert password not in repr(database_url(settings))
