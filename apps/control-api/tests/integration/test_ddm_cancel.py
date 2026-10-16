"""未派发 DDM 取消：真实 PostgreSQL、既有执行器与基座 HTTP seam。"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from _integration_support import RedisServer, settings_for
from redis import Redis
from sqlalchemy import Engine
from test_ddm_dispatch import DdmHttpServer, executor, run
from test_ddm_dispatch import selections as selections
from test_ddm_training_command import NOW, _caller, _submit

from factory_sop.auth.api import AuthorizationRefusedError, Caller, Permission
from factory_sop.auth.model import User, UserStatus
from factory_sop.dataset.adapters.ddm import NvidiaDdmAnnotationGenerator
from factory_sop.dataset.adapters.ddm_execution import (
    DdmTrainingDispatchOutcome,
    HttpDdmTrainingBackend,
)
from factory_sop.dataset.adapters.repository import PostgresDatasetRepository
from factory_sop.dataset.errors import DatasetRefusalCode, DatasetRefusedError
from factory_sop.dataset.model import DdmTrainingCommand, DdmTrainingCommandStatus
from factory_sop.dataset.usecases.ddm_command import (
    cancel_ddm_training_command,
    read_ddm_training_command,
)
from factory_sop.dataset.usecases.usage import DdmInputSelection
from factory_sop.job.adapters.dispatcher import ArqJobDispatcher
from factory_sop.job.adapters.repository import PostgresJobRepository
from factory_sop.job.adapters.worker import dispatch_ddm_training_job, dispatch_pending_jobs
from factory_sop.job.api import JobStatus
from factory_sop.persistence import session_factory


def test_cancel_pending_is_durable_idempotent_and_never_starts(
    engine: Engine,
    tmp_path: Path,
    selections: tuple[DdmInputSelection, DdmInputSelection],
) -> None:
    submitted = _submit(
        engine, idempotency_key="cancel-pending", training=selections[0], validation=selections[1]
    )
    actor = _caller(Permission.DATASET_EDIT)
    cancelled_at = submitted.command.created_at + timedelta(seconds=1)
    with session_factory(engine).begin() as session:
        cancelled = cancel_ddm_training_command(
            dataset_id=submitted.command.dataset_id,
            command_id=submitted.command.id,
            caller=actor,
            now=cancelled_at,
            datasets=PostgresDatasetRepository(session),
        )
    assert cancelled.status.value == "cancelled"
    assert cancelled.cancelled_by == actor.user.id
    assert cancelled.cancelled_at == cancelled_at
    assert cancelled.payload == submitted.command.payload
    assert cancelled.request_digest == submitted.command.request_digest
    assert cancelled.job_id == submitted.job.id
    assert cancelled.created_by == submitted.command.created_by
    assert cancelled.created_at == submitted.command.created_at
    with session_factory(engine).begin() as session:
        datasets = PostgresDatasetRepository(session)
        assert (
            read_ddm_training_command(
                dataset_id=cancelled.dataset_id,
                command_id=cancelled.id,
                caller=_caller(Permission.DATASET_VIEW),
                datasets=datasets,
            )
            == cancelled
        )
        assert (
            cancel_ddm_training_command(
                dataset_id=cancelled.dataset_id,
                command_id=cancelled.id,
                caller=actor,
                now=cancelled_at + timedelta(seconds=20),
                datasets=datasets,
            )
            == cancelled
        )
    with DdmHttpServer() as server:
        for _redelivery in range(3):
            result = run(executor(engine, tmp_path, server), cancelled.id)
            assert result.outcome.value == "cancelled"
            assert result.command == cancelled
        assert server.starts == []
        assert not list(tmp_path.iterdir())
    replay = _submit(
        engine, idempotency_key="cancel-pending", training=selections[0], validation=selections[1]
    )
    assert replay.command == cancelled
    assert replay.job.id == submitted.job.id
    assert replay.command.base_job_id is None
    assert replay.command.dispatch_facts is None
    assert replay.command.base_observation is None


def test_cancelled_outbox_redelivery_finishes_job_without_start(
    engine: Engine,
    tmp_path: Path,
    selections: tuple[DdmInputSelection, DdmInputSelection],
    redis_server: RedisServer,
    redis_client: Redis,
) -> None:
    submitted = _submit(
        engine, idempotency_key="cancel-outbox", training=selections[0], validation=selections[1]
    )
    factory = session_factory(engine)
    with factory.begin() as session:
        cancelled = cancel_ddm_training_command(
            dataset_id=submitted.command.dataset_id,
            command_id=submitted.command.id,
            caller=_caller(Permission.DATASET_EDIT),
            now=NOW,
            datasets=PostgresDatasetRepository(session),
        )
    with DdmHttpServer() as server:
        settings = settings_for(
            engine, storage_root=tmp_path, redis_url=redis_server.url
        ).model_copy(
            update={
                "ddm_training_backend_url": server.url,
                "ddm_training_config_path": str(tmp_path / "ddm-config.yaml"),
            }
        )
        ctx = {
            "settings": settings,
            "session_factory": factory,
            "dispatcher": ArqJobDispatcher.from_settings(settings, session_factory=factory),
            "ddm_executor": executor(engine, tmp_path, server),
            "current_caller_resolver": lambda _: _caller(Permission.DATASET_EDIT),
            "blocking_job_slots": asyncio.Semaphore(1),
        }
        # PostgreSQL outbox 真实补投 Redis 后，迟到 worker 只收尾原 job。
        asyncio.run(dispatch_pending_jobs(ctx))
        assert redis_client.keys(f"arq:job:{submitted.job.id}:*")
        asyncio.run(dispatch_ddm_training_job(ctx, str(submitted.job.id)))
        with factory() as session:
            jobs = PostgresJobRepository(session)
            finished = jobs.by_id(submitted.job.id)
            assert finished is not None
            assert finished.status == JobStatus.SUPERSEDED
            assert all(
                job.id != finished.id for job in jobs.pending(limit=100, include_ddm_training=True)
            )
        asyncio.run(dispatch_pending_jobs(ctx))
        asyncio.run(dispatch_ddm_training_job(ctx, str(submitted.job.id)))
        assert server.starts == []
        with factory() as session:
            assert (
                PostgresDatasetRepository(session).ddm_training_command_by_id(cancelled.id)
                == cancelled
            )
    replay = _submit(
        engine, idempotency_key="cancel-outbox", training=selections[0], validation=selections[1]
    )
    assert replay.command == cancelled
    assert replay.job.status == JobStatus.SUPERSEDED


def _edit_caller(actor_id: UUID) -> Caller:
    """构造一个与提交者不同的合法取消者，用于验证首次取消事实不被对端改写。"""
    return Caller(
        user=User(
            id=actor_id,
            login_name="ddm-cancel-peer",
            display_name="DDM 取消对端",
            password_hash="integration-only",  # pragma: allowlist secret
            status=UserStatus.ACTIVE,
        ),
        granted=frozenset({Permission.DATASET_EDIT}),
    )


def _blocking_generate(
    *, entered: threading.Event, release: threading.Event, fail: bool = False
) -> Callable[[Path, str], bytes]:
    """用公开的 generate 回调在准备中确定阻塞，让取消与迟到 worker 的顺序可复现。"""
    real_generate = NvidiaDdmAnnotationGenerator().generate

    def generate(path: Path, filename: str) -> bytes:
        entered.set()
        assert release.wait(timeout=30)
        if fail:
            raise OSError("simulated preparation failure")
        return real_generate(path, filename)

    return generate


def test_cancel_during_preparation_cancels_late_worker_and_cleans_own_workcopies(
    engine: Engine,
    tmp_path: Path,
    selections: tuple[DdmInputSelection, DdmInputSelection],
) -> None:
    """取消在长准备期间胜出：迟到 worker 返回取消、不发送，且只清理本次新建的自身副本。"""
    submitted = _submit(
        engine,
        idempotency_key="cancel-late-prep",
        training=selections[0],
        validation=selections[1],
    )
    neighbour = tmp_path / "unrelated-neighbour"
    neighbour.mkdir()
    (neighbour / "keep.txt").write_text("keep")
    entered, release = threading.Event(), threading.Event()
    with DdmHttpServer() as server:
        worker = executor(
            engine, tmp_path, server, generate=_blocking_generate(entered=entered, release=release)
        )
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(run, worker, submitted.command.id)
            try:
                assert entered.wait(timeout=30)
                with session_factory(engine).begin() as session:
                    cancelled = cancel_ddm_training_command(
                        dataset_id=submitted.command.dataset_id,
                        command_id=submitted.command.id,
                        caller=_caller(Permission.DATASET_EDIT),
                        now=NOW + timedelta(seconds=5),
                        datasets=PostgresDatasetRepository(session),
                    )
            finally:
                release.set()
            result = future.result(timeout=30)
        assert result.outcome is DdmTrainingDispatchOutcome.CANCELLED
        assert result.command == cancelled
        assert server.starts == []
        assert not (tmp_path / submitted.command.payload["input_id"]).exists()
        assert not (tmp_path / f"ddm-{submitted.command.id}-training").exists()
        assert not (tmp_path / f"ddm-{submitted.command.id}-validation").exists()
        assert (neighbour / "keep.txt").read_text() == "keep"
        # 来源与派发历史保持：取消不改写冻结负载与 job 身份，也不改动无关邻目录。
        assert cancelled.payload == submitted.command.payload
        assert cancelled.job_id == submitted.job.id
        assert cancelled.cancelled_by == _caller(Permission.DATASET_EDIT).user.id


def test_late_preparation_failure_does_not_overwrite_cancel(
    engine: Engine,
    tmp_path: Path,
    selections: tuple[DdmInputSelection, DdmInputSelection],
) -> None:
    """准备失败在取消之后到达：迟到 worker 返回取消，不把已取消命令改写成确定失败。"""
    submitted = _submit(
        engine,
        idempotency_key="cancel-late-failure",
        training=selections[0],
        validation=selections[1],
    )
    entered, release = threading.Event(), threading.Event()
    with DdmHttpServer() as server:
        worker = executor(
            engine,
            tmp_path,
            server,
            generate=_blocking_generate(entered=entered, release=release, fail=True),
        )
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(run, worker, submitted.command.id)
            try:
                assert entered.wait(timeout=30)
                with session_factory(engine).begin() as session:
                    cancelled = cancel_ddm_training_command(
                        dataset_id=submitted.command.dataset_id,
                        command_id=submitted.command.id,
                        caller=_caller(Permission.DATASET_EDIT),
                        now=NOW + timedelta(seconds=5),
                        datasets=PostgresDatasetRepository(session),
                    )
            finally:
                release.set()
            result = future.result(timeout=30)
        assert result.outcome is DdmTrainingDispatchOutcome.CANCELLED
        assert server.starts == []
        assert not (tmp_path / submitted.command.payload["input_id"]).exists()
        with session_factory(engine)() as session:
            persisted = PostgresDatasetRepository(session).ddm_training_command_by_id(
                submitted.command.id
            )
        assert persisted is not None
        assert persisted.status is DdmTrainingCommandStatus.CANCELLED
        assert persisted.failure_code is None
        assert persisted.cancelled_at == cancelled.cancelled_at


def test_late_permission_revocation_does_not_overwrite_cancel(
    engine: Engine,
    tmp_path: Path,
    selections: tuple[DdmInputSelection, DdmInputSelection],
) -> None:
    """准备完成后权限被撤销：迟到 worker 返回取消，而不是覆盖已取消命令为失败。"""
    submitted = _submit(
        engine,
        idempotency_key="cancel-late-revoked",
        training=selections[0],
        validation=selections[1],
    )
    entered, release = threading.Event(), threading.Event()
    calls = {"count": 0}

    def resolve(user_id: UUID) -> Caller:
        del user_id
        calls["count"] += 1
        if calls["count"] == 1:
            return _caller(Permission.DATASET_EDIT)
        return _caller(Permission.DATASET_VIEW)

    with DdmHttpServer() as server:
        worker = executor(
            engine, tmp_path, server, generate=_blocking_generate(entered=entered, release=release)
        )
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                worker.execute,
                command_id=submitted.command.id,
                now=NOW,
                resolve_current_caller=resolve,
            )
            try:
                assert entered.wait(timeout=30)
                with session_factory(engine).begin() as session:
                    cancelled = cancel_ddm_training_command(
                        dataset_id=submitted.command.dataset_id,
                        command_id=submitted.command.id,
                        caller=_caller(Permission.DATASET_EDIT),
                        now=NOW + timedelta(seconds=5),
                        datasets=PostgresDatasetRepository(session),
                    )
            finally:
                release.set()
            result = future.result(timeout=30)
        assert result.outcome is DdmTrainingDispatchOutcome.CANCELLED
        assert server.starts == []
        assert not (tmp_path / submitted.command.payload["input_id"]).exists()
        assert not (tmp_path / f"ddm-{submitted.command.id}-training").exists()
        assert not (tmp_path / f"ddm-{submitted.command.id}-validation").exists()
        with session_factory(engine)() as session:
            persisted = PostgresDatasetRepository(session).ddm_training_command_by_id(
                submitted.command.id
            )
        assert persisted is not None
        assert persisted.status is DdmTrainingCommandStatus.CANCELLED
        assert persisted.failure_code is None
        assert persisted.cancelled_at == cancelled.cancelled_at


@pytest.mark.parametrize("start_error", [None, 502])
def test_cancel_refused_after_intent_committed_and_inflight_guard_kept(
    engine: Engine,
    tmp_path: Path,
    selections: tuple[DdmInputSelection, DdmInputSelection],
    start_error: int | None,
) -> None:
    """意图提交后取消被拒；UNKNOWN 丢响应与 BOUND 都保留首次事实与在途约束。"""
    key = f"cancel-intent-{start_error}"
    submitted = _submit(
        engine, idempotency_key=key, training=selections[0], validation=selections[1]
    )
    entered, release = threading.Event(), threading.Event()

    def block_before_start() -> None:
        entered.set()
        assert release.wait(timeout=30)

    with DdmHttpServer() as server:
        server.start_error = start_error
        server.before_start = block_before_start
        worker = executor(
            engine,
            tmp_path,
            server,
            backend=HttpDdmTrainingBackend(base_url=server.url, timeout_seconds=60),
        )
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(run, worker, submitted.command.id)
            try:
                assert entered.wait(timeout=30)
                with session_factory(engine)() as session:
                    committed = PostgresDatasetRepository(session).ddm_training_command_by_id(
                        submitted.command.id
                    )
                assert committed is not None
                assert committed.status is DdmTrainingCommandStatus.RESULT_UNKNOWN
                with (
                    session_factory(engine)() as session,
                    pytest.raises(DatasetRefusedError) as refused,
                ):
                    cancel_ddm_training_command(
                        dataset_id=submitted.command.dataset_id,
                        command_id=submitted.command.id,
                        caller=_caller(Permission.DATASET_EDIT),
                        now=NOW,
                        datasets=PostgresDatasetRepository(session),
                    )
                assert refused.value.code is DatasetRefusalCode.USAGE_STATE_CONFLICT
            finally:
                release.set()
            result = future.result(timeout=60)
        expected = (
            DdmTrainingDispatchOutcome.BOUND
            if start_error is None
            else DdmTrainingDispatchOutcome.RESULT_UNKNOWN
        )
        assert result.outcome is expected
        assert len(server.starts) == 1
        # 首次事实保持：取消从未写入，冻结负载与在途保护不变。
        with session_factory(engine)() as session:
            persisted = PostgresDatasetRepository(session).ddm_training_command_by_id(
                submitted.command.id
            )
        assert persisted is not None
        assert persisted.cancelled_by is None
        assert persisted.payload == submitted.command.payload
        with session_factory(engine)() as session, pytest.raises(DatasetRefusedError) as again:
            cancel_ddm_training_command(
                dataset_id=persisted.dataset_id,
                command_id=persisted.id,
                caller=_caller(Permission.DATASET_EDIT),
                now=NOW,
                datasets=PostgresDatasetRepository(session),
            )
        assert again.value.code is DatasetRefusalCode.USAGE_STATE_CONFLICT
        second = _submit(
            engine,
            idempotency_key=f"{key}-second",
            training=selections[0],
            validation=selections[1],
        )
        blocked = run(executor(engine, tmp_path, server), second.command.id)
        assert blocked.outcome is DdmTrainingDispatchOutcome.BLOCKED
        assert blocked.failure_code == "DISPATCH_IN_FLIGHT"
        assert len(server.starts) == 1


@pytest.mark.parametrize(
    "permissions",
    [
        (),
        (Permission.DATASET_VIEW,),
        (Permission.DATASET_IMPORT,),
        (Permission.DATASET_VIEW, Permission.DATASET_IMPORT),
    ],
)
def test_cancel_requires_edit_permission(
    engine: Engine,
    selections: tuple[DdmInputSelection, DdmInputSelection],
    permissions: tuple[Permission, ...],
) -> None:
    """查看/导入或无权限都不能取消；取消不因持有 view/import 而隐含。"""
    label = "-".join(permission.value for permission in permissions) or "none"
    submitted = _submit(
        engine,
        idempotency_key=f"cancel-perm-{label}",
        training=selections[0],
        validation=selections[1],
    )
    with session_factory(engine)() as session, pytest.raises(AuthorizationRefusedError):
        cancel_ddm_training_command(
            dataset_id=submitted.command.dataset_id,
            command_id=submitted.command.id,
            caller=_caller(*permissions),
            now=NOW,
            datasets=PostgresDatasetRepository(session),
        )
    with session_factory(engine)() as session:
        persisted = PostgresDatasetRepository(session).ddm_training_command_by_id(
            submitted.command.id
        )
    assert persisted is not None
    assert persisted.status is DdmTrainingCommandStatus.PENDING
    assert persisted.cancelled_by is None


def test_query_requires_view_and_not_edit_or_import(
    engine: Engine,
    selections: tuple[DdmInputSelection, DdmInputSelection],
) -> None:
    """查询命令需要 view；edit 不隐含 view，import 也不授予查看。"""
    submitted = _submit(
        engine, idempotency_key="query-perm", training=selections[0], validation=selections[1]
    )
    with session_factory(engine)() as session:
        assert (
            read_ddm_training_command(
                dataset_id=submitted.command.dataset_id,
                command_id=submitted.command.id,
                caller=_caller(Permission.DATASET_VIEW),
                datasets=PostgresDatasetRepository(session),
            )
            == submitted.command
        )
    for caller in (
        _caller(),
        _caller(Permission.DATASET_EDIT),
        _caller(Permission.DATASET_IMPORT),
    ):
        with session_factory(engine)() as session, pytest.raises(AuthorizationRefusedError):
            read_ddm_training_command(
                dataset_id=submitted.command.dataset_id,
                command_id=submitted.command.id,
                caller=caller,
                datasets=PostgresDatasetRepository(session),
            )


def test_cancel_refuses_wrong_dataset_and_missing_command(
    engine: Engine,
    selections: tuple[DdmInputSelection, DdmInputSelection],
) -> None:
    """归属不符或命令缺失一律按不存在拒绝，不影响其它数据集的命令。"""
    submitted = _submit(
        engine, idempotency_key="cancel-owner", training=selections[0], validation=selections[1]
    )
    actor = _caller(Permission.DATASET_EDIT)
    with session_factory(engine)() as session, pytest.raises(DatasetRefusedError) as wrong:
        cancel_ddm_training_command(
            dataset_id=uuid4(),
            command_id=submitted.command.id,
            caller=actor,
            now=NOW,
            datasets=PostgresDatasetRepository(session),
        )
    assert wrong.value.code is DatasetRefusalCode.DDM_TRAINING_COMMAND_NOT_FOUND
    with session_factory(engine)() as session, pytest.raises(DatasetRefusedError) as missing:
        cancel_ddm_training_command(
            dataset_id=submitted.command.dataset_id,
            command_id=uuid4(),
            caller=actor,
            now=NOW,
            datasets=PostgresDatasetRepository(session),
        )
    assert missing.value.code is DatasetRefusalCode.DDM_TRAINING_COMMAND_NOT_FOUND
    with session_factory(engine)() as session, pytest.raises(DatasetRefusedError):
        read_ddm_training_command(
            dataset_id=uuid4(),
            command_id=submitted.command.id,
            caller=_caller(Permission.DATASET_VIEW),
            datasets=PostgresDatasetRepository(session),
        )
    with session_factory(engine)() as session:
        persisted = PostgresDatasetRepository(session).ddm_training_command_by_id(
            submitted.command.id
        )
    assert persisted is not None
    assert persisted.status is DdmTrainingCommandStatus.PENDING


def test_rolled_back_cancel_keeps_command_pending_and_startable(
    engine: Engine,
    tmp_path: Path,
    selections: tuple[DdmInputSelection, DdmInputSelection],
) -> None:
    """取消只在提交后生效：回滚的取消不写事实，命令仍可按未派发状态真实启动。"""
    submitted = _submit(
        engine, idempotency_key="cancel-rollback", training=selections[0], validation=selections[1]
    )
    with session_factory(engine)() as session:
        cancel_ddm_training_command(
            dataset_id=submitted.command.dataset_id,
            command_id=submitted.command.id,
            caller=_caller(Permission.DATASET_EDIT),
            now=NOW,
            datasets=PostgresDatasetRepository(session),
        )
        session.rollback()
    with session_factory(engine)() as session:
        persisted = PostgresDatasetRepository(session).ddm_training_command_by_id(
            submitted.command.id
        )
    assert persisted is not None
    assert persisted.status is DdmTrainingCommandStatus.PENDING
    assert persisted.cancelled_by is None
    assert persisted.cancelled_at is None
    with DdmHttpServer() as server:
        result = run(executor(engine, tmp_path, server), submitted.command.id)
        assert result.outcome is DdmTrainingDispatchOutcome.BOUND
        assert len(server.starts) == 1


def test_concurrent_duplicate_cancel_keeps_first_actor_and_time(
    engine: Engine,
    selections: tuple[DdmInputSelection, DdmInputSelection],
) -> None:
    """两个合法取消者竞争同一 pending 行：获胜方写首次事实，落败方重放同一事实。"""
    submitted = _submit(
        engine,
        idempotency_key="cancel-concurrent",
        training=selections[0],
        validation=selections[1],
    )
    first_actor = _caller(Permission.DATASET_EDIT)
    second_actor = _edit_caller(uuid4())
    first_now = NOW + timedelta(seconds=1)
    second_now = NOW + timedelta(seconds=2)
    barrier = threading.Barrier(2)
    results: list[DdmTrainingCommand] = []

    def cancel(caller: Caller, now: datetime) -> None:
        barrier.wait(timeout=30)
        with session_factory(engine).begin() as session:
            results.append(
                cancel_ddm_training_command(
                    dataset_id=submitted.command.dataset_id,
                    command_id=submitted.command.id,
                    caller=caller,
                    now=now,
                    datasets=PostgresDatasetRepository(session),
                )
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(cancel, caller, now)
            for caller, now in ((first_actor, first_now), (second_actor, second_now))
        ]
        for future in futures:
            future.result(timeout=30)
    assert len(results) == 2
    winner = results[0]
    assert winner.status is DdmTrainingCommandStatus.CANCELLED
    assert {result.cancelled_by for result in results} == {winner.cancelled_by}
    assert {result.cancelled_at for result in results} == {winner.cancelled_at}
    assert winner.cancelled_by in {first_actor.user.id, second_actor.user.id}
    assert winner.cancelled_at in {first_now, second_now}
    with session_factory(engine)() as session:
        persisted = PostgresDatasetRepository(session).ddm_training_command_by_id(
            submitted.command.id
        )
    assert persisted is not None
    assert persisted.cancelled_by == winner.cancelled_by
    assert persisted.cancelled_at == winner.cancelled_at
