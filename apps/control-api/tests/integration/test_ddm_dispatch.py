"""完整 dataset execute seam：真实 PostgreSQL 与原 DDM 查询 HTTP handler。"""

from __future__ import annotations

import importlib.util
import os
import threading
from collections.abc import Callable, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, BinaryIO, cast
from uuid import UUID, uuid4

import pytest
from _integration_support import cleanup_dataset
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker
from test_ddm_training_command import NOW, _caller, _FakeStorage, _seed_passing_ddm, _submit

from factory_sop.auth.api import AuthorizationRefusedError, Caller, Permission
from factory_sop.auth.model import User, UserStatus
from factory_sop.dataset.adapters.ddm import NvidiaDdmAnnotationGenerator
from factory_sop.dataset.adapters.ddm_execution import (
    DdmTrainingBackend,
    DdmTrainingDispatchOutcome,
    DdmTrainingDispatchResult,
    HttpDdmTrainingBackend,
    PostgresDdmTrainingExecutor,
)
from factory_sop.dataset.adapters.repository import PostgresDatasetRepository
from factory_sop.dataset.errors import DatasetRefusalCode, DatasetRefusedError
from factory_sop.dataset.model import DdmTrainingCommand, DdmTrainingCommandStatus
from factory_sop.dataset.storage import ObjectStorage
from factory_sop.dataset.usecases.ddm_command import (
    cancel_ddm_training_command,
    get_ddm_training_command,
)
from factory_sop.dataset.usecases.usage import (
    DDM_CONSUMER_PARAMETERS,
    DDM_VALIDATION_CONSUMER_PARAMETERS,
    DdmInputSelection,
)
from factory_sop.persistence import session_factory

ROOT = Path(__file__).resolve().parents[4]
_spec = importlib.util.spec_from_file_location(
    "ddm_http_fixture", ROOT / "tests/contract/base/ddm_http_fixture.py"
)
assert _spec is not None
assert _spec.loader is not None
_fixture = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fixture)
DdmHttpServer: Any = _fixture.DdmHttpServer
_ARTIFACT = b"synthetic immutable artifact"


class Storage(_FakeStorage):
    def download_to(self, *, object_key: str, destination: BinaryIO) -> None:
        if object_key.startswith("artifacts/"):
            destination.write(_ARTIFACT)
        else:
            super().download_to(object_key=object_key, destination=destination)


class _BackendAcceptedThenInterrupted(BaseException):
    """基座已接受 start 后进程中断；BaseException 会穿越 executor 的 HTTP 失败处理。"""


class _InterruptingBackend:
    """代理真实 HTTP 基座，但在 start 已创建任务后抛 BaseException 模拟发送后崩溃。"""

    def __init__(self, inner: DdmTrainingBackend) -> None:
        self._inner = inner

    def start(self, *, training_dataset_id: str, validation_dataset_id: str) -> str:
        self._inner.start(
            training_dataset_id=training_dataset_id,
            validation_dataset_id=validation_dataset_id,
        )
        raise _BackendAcceptedThenInterrupted

    def all_jobs(self) -> Mapping[str, Mapping[str, Any]]:
        return self._inner.all_jobs()

    def status(self, job_id: str) -> Mapping[str, Any]:
        return self._inner.status(job_id)


class _IntentCommitUncertainSession(Session):
    """真实提交后再报错：模拟「意图提交结果在发送前不确定」的事务故障 seam。"""

    def commit(self) -> None:
        super().commit()
        raise RuntimeError("simulated uncertain intent commit")


@pytest.fixture
def selections(engine: Engine) -> Iterator[tuple[DdmInputSelection, DdmInputSelection]]:
    ids = (uuid4(), uuid4())
    inputs = []
    with session_factory(engine).begin() as session:
        for index, dataset_id in enumerate(ids):
            check, artifact = _seed_passing_ddm(
                session,
                label=f"dispatch-{dataset_id}",
                dataset_id=dataset_id,
                member_id=uuid4(),
                context_id=uuid4(),
                submission_id=uuid4(),
                execution_id=uuid4(),
                now=NOW + timedelta(seconds=index * 10),
                artifact_content=_ARTIFACT,
            )
            inputs.append(
                DdmInputSelection(dataset_id=dataset_id, usage_check_id=check, artifact_id=artifact)
            )
    try:
        yield inputs[0], inputs[1]
    finally:
        for dataset_id in ids:
            cleanup_dataset(engine, dataset_id)


def executor(
    engine: Engine,
    root: Path,
    server: Any,  # noqa: ANN401 — 动态加载的 HTTP fixture
    **overrides: Any,  # noqa: ANN401 — 按需覆盖受信组合参数
) -> PostgresDdmTrainingExecutor:
    options: dict[str, Any] = {
        "factory": session_factory(engine),
        "storage_factory": lambda: cast(ObjectStorage, Storage()),
        "generate": NvidiaDdmAnnotationGenerator().generate,
        "backend": HttpDdmTrainingBackend(base_url=server.url, timeout_seconds=2),
        "backend_target": server.url,
        "dataset_root": root,
        "training_consumer_parameters": DDM_CONSUMER_PARAMETERS,
        "validation_consumer_parameters": DDM_VALIDATION_CONSUMER_PARAMETERS,
    }
    options.update(overrides)
    return PostgresDdmTrainingExecutor(**options)


def run(worker: PostgresDdmTrainingExecutor, command_id: UUID) -> DdmTrainingDispatchResult:
    return worker.execute(
        command_id=command_id,
        now=NOW,
        resolve_current_caller=lambda _id: _caller(Permission.DATASET_EDIT),
    )


def test_normal_response_and_redelivery_only_poll_original_job(
    engine: Engine,
    tmp_path: Path,
    selections: tuple[DdmInputSelection, DdmInputSelection],
) -> None:
    submitted = _submit(
        engine, idempotency_key="normal", training=selections[0], validation=selections[1]
    )
    with DdmHttpServer() as server:
        first = run(executor(engine, tmp_path, server), submitted.command.id)
        assert first.outcome is DdmTrainingDispatchOutcome.BOUND
        assert first.command is not None
        assert first.command.base_job_id in server.jobs
        # BOUND 后成功在途输入必须保留：清理绝不能销毁基座正在读取的输入。
        assert (tmp_path / submitted.command.payload["input_id"]).is_dir()
        assert (tmp_path / f"ddm-{submitted.command.id}-training").is_dir()
        assert (tmp_path / f"ddm-{submitted.command.id}-validation").is_dir()
        server.queries.clear()
        second = run(executor(engine, tmp_path, server), submitted.command.id)
        assert second.command is not None
        assert second.command.base_job_id == first.command.base_job_id
        assert server.queries == [first.command.base_job_id]
        assert len(server.starts) == 1


def test_full_one_shot_intent_is_durable_before_start(
    engine: Engine,
    tmp_path: Path,
    selections: tuple[DdmInputSelection, DdmInputSelection],
) -> None:
    submitted = _submit(
        engine, idempotency_key="intent", training=selections[0], validation=selections[1]
    )
    with DdmHttpServer() as server:
        old_id = server.add_job(f"ddm-{submitted.command.id}-training")
        observed: list[DdmTrainingCommand] = []

        def inspect_intent() -> None:
            with session_factory(engine)() as session:
                command = PostgresDatasetRepository(session).ddm_training_command_by_id(
                    submitted.command.id
                )
            assert command is not None
            observed.append(command)

        server.before_start = inspect_intent
        result = run(executor(engine, tmp_path, server), submitted.command.id)
        assert observed[0].status == "result_unknown"
        assert observed[0].baseline_job_ids == (old_id,)
        facts = observed[0].dispatch_facts
        assert facts is not None
        assert facts["input_id"] == submitted.command.payload["input_id"]
        assert facts["training_dataset_id"] == server.starts[0][0]
        assert facts["consumption"]["consumed_files"]
        assert observed[0].payload == submitted.command.payload
        assert result.command is not None
        assert result.command.base_job_id != old_id


@pytest.mark.parametrize("http_error", [400, 502])
def test_post_send_errors_are_unknown_then_only_unique_new_job_binds(
    engine: Engine,
    tmp_path: Path,
    selections: tuple[DdmInputSelection, DdmInputSelection],
    http_error: int,
) -> None:
    submitted = _submit(
        engine,
        idempotency_key=f"lost-{http_error}",
        training=selections[0],
        validation=selections[1],
    )
    with DdmHttpServer() as server:
        server.start_error = http_error
        result = run(executor(engine, tmp_path, server), submitted.command.id)
        assert result.outcome is DdmTrainingDispatchOutcome.RESULT_UNKNOWN
        # result_unknown 期间必须保留本次准备的在途输入，等待唯一恢复绑定。
        assert (tmp_path / submitted.command.payload["input_id"]).is_dir()
        assert (tmp_path / f"ddm-{submitted.command.id}-training").is_dir()
        assert (tmp_path / f"ddm-{submitted.command.id}-validation").is_dir()
        recovered = run(executor(engine, tmp_path, server), submitted.command.id)
        assert recovered.outcome is DdmTrainingDispatchOutcome.BOUND
        assert recovered.command is not None
        assert recovered.command.base_job_id in server.jobs
        assert len(server.starts) == 1


def test_parallel_workers_never_duplicate_start(
    engine: Engine,
    tmp_path: Path,
    selections: tuple[DdmInputSelection, DdmInputSelection],
) -> None:
    submitted = _submit(
        engine, idempotency_key="parallel", training=selections[0], validation=selections[1]
    )
    with DdmHttpServer() as server:
        barrier = threading.Barrier(2)
        results: list[DdmTrainingDispatchResult] = []

        def work() -> None:
            barrier.wait(timeout=30)
            results.append(run(executor(engine, tmp_path, server), submitted.command.id))

        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda _index: work(), range(2)))
        assert len(server.starts) == 1
        bound_ids = {
            result.command.base_job_id
            for result in results
            if result.outcome is DdmTrainingDispatchOutcome.BOUND and result.command is not None
        }
        assert len(bound_ids) == 1
        assert next(iter(bound_ids)) in server.jobs


def test_zero_match_retains_guard_and_blocks_second_command(
    engine: Engine,
    tmp_path: Path,
    selections: tuple[DdmInputSelection, DdmInputSelection],
) -> None:
    first = _submit(
        engine, idempotency_key="zero-first", training=selections[0], validation=selections[1]
    )
    with DdmHttpServer() as server:
        server.accept = False
        server.start_error = 502
        result = run(executor(engine, tmp_path, server), first.command.id)
        assert result.outcome is DdmTrainingDispatchOutcome.RESULT_UNKNOWN
        recovered = run(executor(engine, tmp_path, server), first.command.id)
        assert recovered.outcome is DdmTrainingDispatchOutcome.RESULT_UNKNOWN
        assert len(server.starts) == 1
        second = _submit(
            engine, idempotency_key="zero-second", training=selections[0], validation=selections[1]
        )
        blocked = run(executor(engine, tmp_path, server), second.command.id)
        assert blocked.outcome is DdmTrainingDispatchOutcome.BLOCKED
        assert blocked.failure_code == "DISPATCH_IN_FLIGHT"
        assert len(server.starts) == 1


def test_multiple_matches_retain_guard(
    engine: Engine,
    tmp_path: Path,
    selections: tuple[DdmInputSelection, DdmInputSelection],
) -> None:
    submitted = _submit(
        engine, idempotency_key="multi", training=selections[0], validation=selections[1]
    )
    with DdmHttpServer() as server:
        server.start_error = 502
        result = run(executor(engine, tmp_path, server), submitted.command.id)
        assert result.outcome is DdmTrainingDispatchOutcome.RESULT_UNKNOWN
        server.add_job(f"ddm-{submitted.command.id}-training")
        recovered = run(executor(engine, tmp_path, server), submitted.command.id)
        assert recovered.outcome is DdmTrainingDispatchOutcome.RESULT_UNKNOWN
        assert recovered.failure_code == "DISPATCH_RESULT_UNVERIFIED"
        assert len(server.starts) == 1


def test_unreachable_query_retains_guard(
    engine: Engine,
    tmp_path: Path,
    selections: tuple[DdmInputSelection, DdmInputSelection],
) -> None:
    submitted = _submit(
        engine, idempotency_key="unreachable", training=selections[0], validation=selections[1]
    )
    with DdmHttpServer() as server:
        server.start_error = 502
        assert (
            run(executor(engine, tmp_path, server), submitted.command.id).outcome
            is DdmTrainingDispatchOutcome.RESULT_UNKNOWN
        )
        server.query_error = True
        recovered = run(executor(engine, tmp_path, server), submitted.command.id)
        assert recovered.outcome is DdmTrainingDispatchOutcome.RESULT_UNKNOWN
        assert recovered.failure_code == "BASE_UNREACHABLE"
        assert len(server.starts) == 1


def test_malformed_query_identity_stays_unknown_without_binding(
    engine: Engine,
    tmp_path: Path,
    selections: tuple[DdmInputSelection, DdmInputSelection],
) -> None:
    submitted = _submit(
        engine, idempotency_key="malformed", training=selections[0], validation=selections[1]
    )
    with DdmHttpServer() as server:
        server.start_error = 502
        assert (
            run(executor(engine, tmp_path, server), submitted.command.id).outcome
            is DdmTrainingDispatchOutcome.RESULT_UNKNOWN
        )
        server.add_malformed_job(aug_dataset_id=f"ddm-{submitted.command.id}-training")
        recovered = run(executor(engine, tmp_path, server), submitted.command.id)
        assert recovered.outcome is DdmTrainingDispatchOutcome.RESULT_UNKNOWN
        assert recovered.command is not None
        assert recovered.command.base_job_id is None
        assert recovered.command.status == "result_unknown"
        assert len(server.starts) == 1


def test_late_writer_cannot_replace_bound_identity(
    engine: Engine,
    tmp_path: Path,
    selections: tuple[DdmInputSelection, DdmInputSelection],
) -> None:
    submitted = _submit(
        engine, idempotency_key="late-writer", training=selections[0], validation=selections[1]
    )
    with DdmHttpServer() as server:
        first = run(executor(engine, tmp_path, server), submitted.command.id)
        assert first.outcome is DdmTrainingDispatchOutcome.BOUND
        assert first.command is not None
        bound_id = first.command.base_job_id
        with session_factory(engine)() as session:
            PostgresDatasetRepository(session).bind_ddm_training_base_job(
                command_id=submitted.command.id, base_job_id=str(uuid4()), now=NOW
            )
            session.commit()
        with session_factory(engine)() as session:
            reloaded = PostgresDatasetRepository(session).ddm_training_command_by_id(
                submitted.command.id
            )
        assert reloaded is not None
        assert reloaded.base_job_id == bound_id
        assert len(server.starts) == 1


def test_permission_revoked_never_sends(
    engine: Engine,
    tmp_path: Path,
    selections: tuple[DdmInputSelection, DdmInputSelection],
) -> None:
    submitted = _submit(
        engine, idempotency_key="revoked", training=selections[0], validation=selections[1]
    )
    with DdmHttpServer() as server:
        result = executor(engine, tmp_path, server).execute(
            command_id=submitted.command.id,
            now=NOW,
            resolve_current_caller=lambda _actor_id: _caller(Permission.DATASET_VIEW),
        )
        assert result.outcome is DdmTrainingDispatchOutcome.FAILED
        assert result.failure_code == "PERMISSION_REVOKED"
        assert server.starts == []


def test_permission_revoked_during_preparation_never_sends(
    engine: Engine,
    tmp_path: Path,
    selections: tuple[DdmInputSelection, DdmInputSelection],
) -> None:
    submitted = _submit(
        engine, idempotency_key="revoked-mid", training=selections[0], validation=selections[1]
    )
    with DdmHttpServer() as server:
        calls = {"count": 0}

        def resolve(user_id: UUID) -> Caller:
            del user_id
            calls["count"] += 1
            if calls["count"] == 1:
                return _caller(Permission.DATASET_EDIT)
            return _caller(Permission.DATASET_VIEW)

        result = executor(engine, tmp_path, server).execute(
            command_id=submitted.command.id,
            now=NOW,
            resolve_current_caller=resolve,
        )
        assert result.outcome is DdmTrainingDispatchOutcome.FAILED
        assert result.failure_code == "PERMISSION_REVOKED"
        assert server.starts == []
        # 完整准备后权限撤销：本次新建的 own root 与两个已平铺目录都必须清理。
        assert not (tmp_path / submitted.command.payload["input_id"]).exists()
        assert not (tmp_path / f"ddm-{submitted.command.id}-training").exists()
        assert not (tmp_path / f"ddm-{submitted.command.id}-validation").exists()


def test_reader_parameter_mismatch_never_sends(
    engine: Engine,
    tmp_path: Path,
    selections: tuple[DdmInputSelection, DdmInputSelection],
) -> None:
    submitted = _submit(
        engine, idempotency_key="reader-mismatch", training=selections[0], validation=selections[1]
    )
    with DdmHttpServer() as server:
        worker = executor(
            engine, tmp_path, server, training_consumer_parameters={"temporal_stride": 999}
        )
        result = run(worker, submitted.command.id)
        assert result.outcome is DdmTrainingDispatchOutcome.FAILED
        assert server.starts == []
        # 确定失败后本次新建的工作副本必须清理，不能留下孤儿目录。
        assert not (tmp_path / submitted.command.payload["input_id"]).exists()


def test_partial_exposure_failure_cleans_only_own_workcopies(
    engine: Engine,
    tmp_path: Path,
    selections: tuple[DdmInputSelection, DdmInputSelection],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """第二个平铺 move 失败：只删除 own root 与已移动的自身目标，无关哨兵保留。"""
    submitted = _submit(
        engine, idempotency_key="partial-expose", training=selections[0], validation=selections[1]
    )
    with DdmHttpServer() as server:
        sentinel = tmp_path / "unrelated-sentinel"
        sentinel.write_text("keep")
        real_replace = os.replace
        moved: list[Path] = []

        def flaky_replace(source: str | Path, target: str | Path) -> None:
            if len(moved) == 1:
                raise OSError("simulated partial exposure failure")
            real_replace(source, target)
            moved.append(Path(target))

        monkeypatch.setattr(os, "replace", flaky_replace)
        result = run(executor(engine, tmp_path, server), submitted.command.id)
        assert result.outcome is DdmTrainingDispatchOutcome.FAILED
        assert result.failure_code == "STORAGE_UNAVAILABLE"
        assert server.starts == []
        assert len(moved) == 1
        assert not (tmp_path / submitted.command.payload["input_id"]).exists()
        assert not moved[0].exists()
        assert sentinel.read_text() == "keep"


def test_recovery_refuses_mismatched_backend_target(
    engine: Engine,
    tmp_path: Path,
    selections: tuple[DdmInputSelection, DdmInputSelection],
) -> None:
    submitted = _submit(
        engine, idempotency_key="target-mismatch", training=selections[0], validation=selections[1]
    )
    with DdmHttpServer() as server:
        server.start_error = 502
        assert (
            run(executor(engine, tmp_path, server), submitted.command.id).outcome
            is DdmTrainingDispatchOutcome.RESULT_UNKNOWN
        )
        mismatched = executor(engine, tmp_path, server, backend_target="http://127.0.0.1:1")
        result = run(mismatched, submitted.command.id)
        assert result.outcome is DdmTrainingDispatchOutcome.BLOCKED
        assert result.failure_code == "BACKEND_TARGET_MISMATCH"
        assert len(server.starts) == 1


def test_bind_write_fault_preserves_unknown_without_resend(
    engine: Engine,
    tmp_path: Path,
    selections: tuple[DdmInputSelection, DdmInputSelection],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 注入位置是 bind 写入调用（与提交不确定共用同一 _bind catch 路径），
    # 不代表真实 PostgreSQL commit 故障注入；只证明写失败不重发且后续可重投递绑定。
    submitted = _submit(
        engine, idempotency_key="bind-failure", training=selections[0], validation=selections[1]
    )
    with DdmHttpServer() as server:

        def explode(_repository: object, **_kwargs: object) -> None:
            raise RuntimeError("simulated bind write failure")

        monkeypatch.setattr(PostgresDatasetRepository, "bind_ddm_training_base_job", explode)
        first = run(executor(engine, tmp_path, server), submitted.command.id)
        assert first.outcome is DdmTrainingDispatchOutcome.RESULT_UNKNOWN
        assert first.failure_code == "BIND_UNVERIFIED"
        assert len(server.starts) == 1
        monkeypatch.undo()
        second = run(executor(engine, tmp_path, server), submitted.command.id)
        assert second.outcome is DdmTrainingDispatchOutcome.BOUND
        assert second.command is not None
        assert second.command.base_job_id in server.jobs
        assert len(server.starts) == 1


def test_interruption_after_base_accepts_reconstructs_and_binds_without_second_start(
    engine: Engine,
    tmp_path: Path,
    selections: tuple[DdmInputSelection, DdmInputSelection],
) -> None:
    """基座已接受 start 后进程中断：意图仍在，重建执行器只查询并唯一绑定，不重发。"""
    submitted = _submit(
        engine, idempotency_key="interrupted", training=selections[0], validation=selections[1]
    )
    with DdmHttpServer() as server:
        inner = HttpDdmTrainingBackend(base_url=server.url, timeout_seconds=2)
        faulted = executor(engine, tmp_path, server, backend=_InterruptingBackend(inner))
        with pytest.raises(_BackendAcceptedThenInterrupted):
            run(faulted, submitted.command.id)
        assert len(server.starts) == 1
        with session_factory(engine)() as session:
            committed = PostgresDatasetRepository(session).ddm_training_command_by_id(
                submitted.command.id
            )
        assert committed is not None
        assert committed.status == "result_unknown"
        assert committed.base_job_id is None

        recovered = run(executor(engine, tmp_path, server), submitted.command.id)
        assert recovered.outcome is DdmTrainingDispatchOutcome.BOUND
        assert recovered.command is not None
        assert recovered.command.base_job_id in server.jobs
        assert len(server.starts) == 1


def test_uncertain_intent_commit_never_sends(
    engine: Engine,
    tmp_path: Path,
    selections: tuple[DdmInputSelection, DdmInputSelection],
) -> None:
    """意图提交结果不确定时不得发送 start；已提交的 result_unknown 由后续投递保持未知。"""
    submitted = _submit(
        engine,
        idempotency_key="intent-commit-fault",
        training=selections[0],
        validation=selections[1],
    )
    with DdmHttpServer() as server:
        failing_factory = sessionmaker(bind=engine, class_=_IntentCommitUncertainSession)
        faulted = executor(engine, tmp_path, server, factory=failing_factory)
        with pytest.raises(RuntimeError):
            run(faulted, submitted.command.id)
        assert server.starts == []

        with session_factory(engine)() as session:
            committed = PostgresDatasetRepository(session).ddm_training_command_by_id(
                submitted.command.id
            )
        assert committed is not None
        assert committed.status == "result_unknown"
        assert committed.base_job_id is None

        redelivery = run(executor(engine, tmp_path, server), submitted.command.id)
        assert redelivery.outcome is DdmTrainingDispatchOutcome.RESULT_UNKNOWN
        assert server.starts == []


def test_begin_cas_loss_to_definite_failure_cleans_own_workcopies(
    engine: Engine,
    tmp_path: Path,
    selections: tuple[DdmInputSelection, DdmInputSelection],
) -> None:
    """准备快照过期：第二个 current actor 复核时命令已被其它 worker 按 pending->FAILED 终结。

    此时 ``_begin`` 的 pending CAS 落空、权威状态为 FAILED，本次新建的工作副本必须清理。
    """
    submitted = _submit(
        engine, idempotency_key="begin-cas-lost", training=selections[0], validation=selections[1]
    )
    with DdmHttpServer() as server:
        calls = {"count": 0}

        def resolve(user_id: UUID) -> Caller:
            del user_id
            calls["count"] += 1
            if calls["count"] == 2:
                with session_factory(engine)() as session:
                    PostgresDatasetRepository(session).record_ddm_training_failure(
                        command_id=submitted.command.id,
                        expected_status=DdmTrainingCommandStatus.PENDING,
                        failure_code="SIMULATED_TERMINAL",
                        failure_detail="其它 worker 已确定失败",
                        now=NOW,
                    )
                    session.commit()
            return _caller(Permission.DATASET_EDIT)

        result = executor(engine, tmp_path, server).execute(
            command_id=submitted.command.id,
            now=NOW,
            resolve_current_caller=resolve,
        )
        assert result.outcome is DdmTrainingDispatchOutcome.FAILED
        assert result.failure_code == "SIMULATED_TERMINAL"
        assert server.starts == []
        assert not (tmp_path / submitted.command.payload["input_id"]).exists()
        assert not (tmp_path / f"ddm-{submitted.command.id}-training").exists()
        assert not (tmp_path / f"ddm-{submitted.command.id}-validation").exists()


def test_revocation_failure_record_commit_uncertain_still_cleans_own_workcopies(
    engine: Engine,
    tmp_path: Path,
    selections: tuple[DdmInputSelection, DdmInputSelection],
) -> None:
    """失败记录真的提交为 FAILED 后才抛错：权威重读确认 FAILED，仍须清理本次新建工作副本。"""
    submitted = _submit(
        engine,
        idempotency_key="revoked-commit-fault",
        training=selections[0],
        validation=selections[1],
    )
    with DdmHttpServer() as server:
        calls = {"count": 0}

        def resolve(user_id: UUID) -> Caller:
            del user_id
            calls["count"] += 1
            if calls["count"] == 1:
                return _caller(Permission.DATASET_EDIT)
            return _caller(Permission.DATASET_VIEW)

        failing_factory = sessionmaker(bind=engine, class_=_IntentCommitUncertainSession)
        worker = executor(engine, tmp_path, server, factory=failing_factory)
        result = worker.execute(
            command_id=submitted.command.id,
            now=NOW,
            resolve_current_caller=resolve,
        )
        assert result.outcome is DdmTrainingDispatchOutcome.FAILED
        assert result.failure_code == "PERMISSION_REVOKED"
        assert server.starts == []
        assert not (tmp_path / submitted.command.payload["input_id"]).exists()
        assert not (tmp_path / f"ddm-{submitted.command.id}-training").exists()
        assert not (tmp_path / f"ddm-{submitted.command.id}-validation").exists()


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
            get_ddm_training_command(
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
            get_ddm_training_command(
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
            get_ddm_training_command(
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
        get_ddm_training_command(
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
