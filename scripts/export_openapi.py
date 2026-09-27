"""导出中心后端的规范 OpenAPI 文档。

应用在进程内构建：导出契约不需要运行中的服务器或 PostgreSQL。settings 为合成值；
`create_app` 只构造 engine 对象、不建立数据库连接。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from pydantic import SecretStr

from factory_sop.app import create_app
from factory_sop.settings import Settings


def export_openapi(destination: Path) -> None:
    app = create_app(
        Settings(
            log_level="warning",
            database_host="contract.invalid",
            database_port=5432,
            database_name="factory_sop",
            database_user="factory_sop",
            database_password=SecretStr("not-used"),
            session_idle_timeout_minutes=720,
            session_absolute_lifetime_minutes=43200,
            session_cookie_transport="require_https",
            csrf_secret=SecretStr("not-used"),
            redis_url=SecretStr("redis://redis.invalid:6379/0"),
        )
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(app.openapi(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        raise SystemExit("usage: export_openapi.py DESTINATION")
    export_openapi(Path(argv[1]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
