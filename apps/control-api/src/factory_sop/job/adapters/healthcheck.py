"""检查 ARQ worker 最近写入的健康键，而不是只检查 Redis 是否可达。"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Mapping

from arq import create_pool

from factory_sop.job.adapters.dispatcher import ArqJobDispatcher
from factory_sop.settings import Settings

_HEALTH_KEY = "arq:queue:health-check"


async def _healthy(settings: Settings) -> bool:
    dispatcher = ArqJobDispatcher.from_settings(settings)
    if not isinstance(dispatcher, ArqJobDispatcher):  # pragma: no cover - 配置已在上游校验
        return False
    pool = await create_pool(dispatcher.redis_settings)
    try:
        value = await pool.get(_HEALTH_KEY)
        ttl = await pool.pttl(_HEALTH_KEY)
        return value is not None and ttl > 0
    finally:
        await pool.close()


def main(environ: Mapping[str, str] | None = None) -> int:
    """返回 0 表示 worker 在 TTL 内写过健康键。"""
    settings = Settings.from_environment(os.environ if environ is None else environ)
    return 0 if asyncio.run(_healthy(settings)) else 1


if __name__ == "__main__":
    raise SystemExit(main())
