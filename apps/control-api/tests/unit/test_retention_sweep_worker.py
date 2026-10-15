"""已有 ARQ worker 按秒级配置推进周期，提交后才记本进程已执行。"""

import asyncio
import threading
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from factory_sop.job.adapters.worker import run_retention_sweep_cron
from factory_sop.settings import Settings
from factory_sop.worker_entrypoint import retention_sweep_runner

_NOW = datetime(2026, 10, 14, tzinfo=UTC)


def test_worker_honors_subminute_cycles_without_reopening_same_cycle() -> None:
    settings = MagicMock(spec=Settings)
    settings.retention_sweep_interval_seconds = 10
    settings.retention_sweep_batch_limit = 32
    factory = MagicMock()
    session = factory.return_value.__enter__.return_value
    with (
        patch("factory_sop.worker_entrypoint.datetime") as clock,
        patch("factory_sop.worker_entrypoint.run_retention_sweep") as sweep,
    ):
        clock.now.side_effect = [_NOW, _NOW + timedelta(seconds=1), _NOW + timedelta(seconds=10)]
        runner = retention_sweep_runner(settings, factory)
        runner()
        runner()
        runner()
    assert factory.call_count == 2
    assert sweep.call_count == 2
    assert session.commit.call_count == 2
    assert sweep.call_args_list[0].kwargs["interval_seconds"] == 10
    assert sweep.call_args_list[1].kwargs["now"] == _NOW + timedelta(seconds=10)


def test_failed_commit_does_not_suppress_same_cycle_retry() -> None:
    settings = MagicMock(spec=Settings)
    settings.retention_sweep_interval_seconds = 10
    settings.retention_sweep_batch_limit = 32
    factory = MagicMock()
    session = factory.return_value.__enter__.return_value
    session.commit.side_effect = [RuntimeError("injected commit failure"), None]
    with (
        patch("factory_sop.worker_entrypoint.datetime") as clock,
        patch("factory_sop.worker_entrypoint.run_retention_sweep") as sweep,
    ):
        clock.now.return_value = _NOW
        runner = retention_sweep_runner(settings, factory)
        with pytest.raises(RuntimeError, match="injected commit failure"):
            runner()
        runner()
        runner()
    assert sweep.call_count == 2
    assert session.commit.call_count == 2


def test_overlapping_cron_does_not_start_a_second_sweep() -> None:
    started = threading.Event()
    finish = threading.Event()
    calls: list[int] = []

    def busy() -> None:
        calls.append(1)
        started.set()
        finish.wait(5)

    async def exercise() -> None:
        ctx = {"retention_sweep": busy, "retention_sweep_lock": asyncio.Lock()}
        first = asyncio.create_task(run_retention_sweep_cron(ctx))
        try:
            assert await asyncio.to_thread(started.wait, 2)
            await run_retention_sweep_cron(ctx)
        finally:
            finish.set()
        await first

    asyncio.run(exercise())
    assert calls == [1]
