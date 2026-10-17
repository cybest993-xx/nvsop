"""完整 dataset execute seam：真实 PostgreSQL 与原 DDM 查询 HTTP handler。"""

from __future__ import annotations

import importlib.util
import os
import threading
from collections.abc import Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import timedelta
from hashlib import sha256
from pathlib import Path
from typing import Any, BinaryIO, cast
from uuid import UUID, uuid4

import pytest
import yaml  # type: ignore[import-untyped]
from _integration_support import cleanup_dataset
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker
from test_ddm_training_command import NOW, _caller, _FakeStorage, _seed_passing_ddm, _submit

from factory_sop.auth.api import Caller, Permission
from factory_sop.dataset.adapters.ddm import LocalDdmResultVolume, NvidiaDdmAnnotationGenerator
from factory_sop.dataset.adapters.ddm_execution import (
    DdmTrainingServiceAdapter,
    HttpDdmTrainingBackend,
)
from factory_sop.dataset.adapters.repository import PostgresDatasetRepository
from factory_sop.dataset.adapters.storage import LocalFileObjectStorage
from factory_sop.dataset.adapters.training_execution import (
    PostgresTrainingCommandExecutor,
    TrainingDispatchOutcome,
    TrainingDispatchResult,
)
from factory_sop.dataset.api import TrainingJobResult
from factory_sop.dataset.model import TrainingCommand, TrainingCommandStatus
from factory_sop.dataset.storage import ObjectStorage
from factory_sop.dataset.usecases.usage import (
    DDM_CONSUMER_PARAMETERS,
    DDM_VALIDATION_CONSUMER_PARAMETERS,
    DdmInputSelection,
)
from factory_sop.job.adapters.repository import PostgresJobRepository
from factory_sop.job.api import JobStatus, JobType
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

    def __init__(self, inner: HttpDdmTrainingBackend) -> None:
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

    def cancel(self, job_id: str) -> Mapping[str, Any]:
        return self._inner.cancel(job_id=job_id)


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
) -> PostgresTrainingCommandExecutor:
    """构造真实组合：共享执行器 + DDM 服务 adapter；覆盖项按所属层分流。"""
    executor_options: dict[str, Any] = {
        "factory": session_factory(engine),
        "storage_factory": lambda: cast(ObjectStorage, Storage()),
    }
    adapter_options: dict[str, Any] = {
        "generate": NvidiaDdmAnnotationGenerator().generate,
        "backend": HttpDdmTrainingBackend(base_url=server.url, timeout_seconds=2),
        "backend_target": server.url,
        "dataset_root": root,
        "training_consumer_parameters": DDM_CONSUMER_PARAMETERS,
        "validation_consumer_parameters": DDM_VALIDATION_CONSUMER_PARAMETERS,
    }
    for key, value in overrides.items():
        if key in executor_options:
            executor_options[key] = value
        else:
            adapter_options[key] = value
    return PostgresTrainingCommandExecutor(
        **executor_options, adapter=DdmTrainingServiceAdapter(**adapter_options)
    )


def run(worker: PostgresTrainingCommandExecutor, command_id: UUID) -> TrainingDispatchResult:
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
        assert first.outcome is TrainingDispatchOutcome.BOUND
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
        observed: list[TrainingCommand] = []

        def inspect_intent() -> None:
            with session_factory(engine)() as session:
                command = PostgresDatasetRepository(session).training_command_by_id(
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
        assert result.outcome is TrainingDispatchOutcome.RESULT_UNKNOWN
        # result_unknown 期间必须保留本次准备的在途输入，等待唯一恢复绑定。
        assert (tmp_path / submitted.command.payload["input_id"]).is_dir()
        assert (tmp_path / f"ddm-{submitted.command.id}-training").is_dir()
        assert (tmp_path / f"ddm-{submitted.command.id}-validation").is_dir()
        recovered = run(executor(engine, tmp_path, server), submitted.command.id)
        assert recovered.outcome is TrainingDispatchOutcome.BOUND
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
        results: list[TrainingDispatchResult] = []

        def work() -> None:
            barrier.wait(timeout=30)
            results.append(run(executor(engine, tmp_path, server), submitted.command.id))

        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda _index: work(), range(2)))
        assert len(server.starts) == 1
        bound_ids = {
            result.command.base_job_id
            for result in results
            if result.outcome is TrainingDispatchOutcome.BOUND and result.command is not None
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
        assert result.outcome is TrainingDispatchOutcome.RESULT_UNKNOWN
        recovered = run(executor(engine, tmp_path, server), first.command.id)
        assert recovered.outcome is TrainingDispatchOutcome.RESULT_UNKNOWN
        assert len(server.starts) == 1
        second = _submit(
            engine, idempotency_key="zero-second", training=selections[0], validation=selections[1]
        )
        blocked = run(executor(engine, tmp_path, server), second.command.id)
        assert blocked.outcome is TrainingDispatchOutcome.BLOCKED
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
        assert result.outcome is TrainingDispatchOutcome.RESULT_UNKNOWN
        server.add_job(f"ddm-{submitted.command.id}-training")
        recovered = run(executor(engine, tmp_path, server), submitted.command.id)
        assert recovered.outcome is TrainingDispatchOutcome.RESULT_UNKNOWN
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
            is TrainingDispatchOutcome.RESULT_UNKNOWN
        )
        server.query_error = True
        recovered = run(executor(engine, tmp_path, server), submitted.command.id)
        assert recovered.outcome is TrainingDispatchOutcome.RESULT_UNKNOWN
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
            is TrainingDispatchOutcome.RESULT_UNKNOWN
        )
        server.add_malformed_job(aug_dataset_id=f"ddm-{submitted.command.id}-training")
        recovered = run(executor(engine, tmp_path, server), submitted.command.id)
        assert recovered.outcome is TrainingDispatchOutcome.RESULT_UNKNOWN
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
        assert first.outcome is TrainingDispatchOutcome.BOUND
        assert first.command is not None
        bound_id = first.command.base_job_id
        with session_factory(engine)() as session:
            PostgresDatasetRepository(session).bind_training_base_job(
                command_id=submitted.command.id, base_job_id=str(uuid4()), now=NOW
            )
            session.commit()
        with session_factory(engine)() as session:
            reloaded = PostgresDatasetRepository(session).training_command_by_id(
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
        assert result.outcome is TrainingDispatchOutcome.FAILED
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
        assert result.outcome is TrainingDispatchOutcome.FAILED
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
        assert result.outcome is TrainingDispatchOutcome.FAILED
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
        assert result.outcome is TrainingDispatchOutcome.FAILED
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
            is TrainingDispatchOutcome.RESULT_UNKNOWN
        )
        mismatched = executor(engine, tmp_path, server, backend_target="http://127.0.0.1:1")
        result = run(mismatched, submitted.command.id)
        assert result.outcome is TrainingDispatchOutcome.BLOCKED
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

        monkeypatch.setattr(PostgresDatasetRepository, "bind_training_base_job", explode)
        first = run(executor(engine, tmp_path, server), submitted.command.id)
        assert first.outcome is TrainingDispatchOutcome.RESULT_UNKNOWN
        assert first.failure_code == "BIND_UNVERIFIED"
        assert len(server.starts) == 1
        monkeypatch.undo()
        second = run(executor(engine, tmp_path, server), submitted.command.id)
        assert second.outcome is TrainingDispatchOutcome.BOUND
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
            committed = PostgresDatasetRepository(session).training_command_by_id(
                submitted.command.id
            )
        assert committed is not None
        assert committed.status == "result_unknown"
        assert committed.base_job_id is None

        recovered = run(executor(engine, tmp_path, server), submitted.command.id)
        assert recovered.outcome is TrainingDispatchOutcome.BOUND
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
            committed = PostgresDatasetRepository(session).training_command_by_id(
                submitted.command.id
            )
        assert committed is not None
        assert committed.status == "result_unknown"
        assert committed.base_job_id is None

        redelivery = run(executor(engine, tmp_path, server), submitted.command.id)
        assert redelivery.outcome is TrainingDispatchOutcome.RESULT_UNKNOWN
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
                    PostgresDatasetRepository(session).record_training_failure(
                        command_id=submitted.command.id,
                        expected_status=TrainingCommandStatus.PENDING,
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
        assert result.outcome is TrainingDispatchOutcome.FAILED
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
        assert result.outcome is TrainingDispatchOutcome.FAILED
        assert result.failure_code == "PERMISSION_REVOKED"
        assert server.starts == []
        assert not (tmp_path / submitted.command.payload["input_id"]).exists()
        assert not (tmp_path / f"ddm-{submitted.command.id}-training").exists()
        assert not (tmp_path / f"ddm-{submitted.command.id}-validation").exists()


def _write_base_result_files(
    *,
    results_root: Path,
    dataset_root: Path,
    base_job_id: str,
    training_dataset_id: str,
    validation_dataset_id: str,
    checkpoint_bytes: bytes = b"real-checkpoint-bytes",
    include_checkpoint: bool = True,
    reader_overrides: Mapping[str, object] | None = None,
) -> None:
    """复刻基座真实落盘布局：job 配置 + 合并 config.yaml + last.ckpt。"""
    job_directory = results_root / base_job_id
    train_directory = job_directory / "train" / base_job_id
    train_directory.mkdir(parents=True)
    dataset_config: dict[str, object] = {
        "dataset": "DDMDataset",
        "num_classes": 2,
        "frames_per_side": 5,
        "downsample": 1,
        "min_change_dur": 0.3,
        "video_backend": "pyav",
        "train_config": {
            "data_root": str(dataset_root / training_dataset_id),
            "anno_path": str(dataset_root / training_dataset_id / "ddm_train_annotation.json"),
            "mode": "train",
        },
        "val_config": {
            "data_root": str(dataset_root / validation_dataset_id),
            "anno_path": str(dataset_root / validation_dataset_id / "ddm_val_annotation.json"),
            "mode": "val",
            "temporal_stride": 1,
        },
    }
    if reader_overrides:
        dataset_config.update(reader_overrides)
    document = {
        "dataset_config": dataset_config,
        "model_config": {"num_classes": 2},
        "training_config": {"output": str(job_directory), "exp_name": base_job_id},
    }
    (job_directory / f"{base_job_id}.yaml").write_text(yaml.safe_dump(document), encoding="utf-8")
    (train_directory / "config.yaml").write_text(yaml.safe_dump(document), encoding="utf-8")
    if include_checkpoint:
        (train_directory / "last.ckpt").write_bytes(checkpoint_bytes)


def _result_executor(
    engine: Engine,
    root: Path,
    server: Any,  # noqa: ANN401 — 动态加载的 HTTP fixture
    *,
    results_root: Path,
    objects_root: Path,
) -> PostgresTrainingCommandExecutor:
    return PostgresTrainingCommandExecutor(
        factory=session_factory(engine),
        storage_factory=lambda: LocalFileObjectStorage(objects_root),
        adapter=DdmTrainingServiceAdapter(
            generate=NvidiaDdmAnnotationGenerator().generate,
            backend=HttpDdmTrainingBackend(base_url=server.url, timeout_seconds=2),
            backend_target=server.url,
            dataset_root=root,
            training_consumer_parameters=DDM_CONSUMER_PARAMETERS,
            validation_consumer_parameters=DDM_VALIDATION_CONSUMER_PARAMETERS,
            result_volume=LocalDdmResultVolume(results_root=results_root, dataset_root=root),
        ),
    )


def _reload(engine: Engine, command_id: UUID) -> TrainingCommand:
    with session_factory(engine)() as session:
        command = PostgresDatasetRepository(session).training_command_by_id(command_id)
    assert command is not None
    return command


def test_base_completed_registers_real_config_and_checkpoint_then_releases_guard(
    engine: Engine,
    tmp_path: Path,
    selections: tuple[DdmInputSelection, DdmInputSelection],
) -> None:
    submitted = _submit(
        engine, idempotency_key="result-ok", training=selections[0], validation=selections[1]
    )
    results_root = tmp_path / "results"
    objects_root = tmp_path / "objects"
    with DdmHttpServer() as server:
        bound = run(executor(engine, tmp_path, server), submitted.command.id)
        assert bound.outcome is TrainingDispatchOutcome.BOUND
        assert bound.command is not None
        base_job_id = bound.command.base_job_id
        assert base_job_id is not None
        _write_base_result_files(
            results_root=results_root,
            dataset_root=tmp_path,
            base_job_id=base_job_id,
            training_dataset_id=f"ddm-{submitted.command.id}-training",
            validation_dataset_id=f"ddm-{submitted.command.id}-validation",
        )
        server.jobs[base_job_id].status = "completed"
        worker = _result_executor(
            engine, tmp_path, server, results_root=results_root, objects_root=objects_root
        )
        result = run(worker, submitted.command.id)
        assert result.outcome is TrainingDispatchOutcome.BOUND
        stored = _reload(engine, submitted.command.id)
        assert stored.status is TrainingCommandStatus.COMPLETED
        assert stored.result_status == "registered"
        assert stored.result_registered_at is not None
        assert stored.result_generation == 1
        assert stored.result_config_object_key is not None
        assert stored.result_checkpoint_object_key is not None
        config_path = objects_root / stored.result_config_object_key
        checkpoint_path = objects_root / stored.result_checkpoint_object_key
        assert config_path.is_file()
        assert checkpoint_path.is_file()
        assert stored.result_config_size == config_path.stat().st_size
        assert stored.result_config_sha256 == sha256(config_path.read_bytes()).hexdigest()
        assert stored.result_checkpoint_sha256 == sha256(checkpoint_path.read_bytes()).hexdigest()
        # 已发布终态：再次投递不再观察、不重发 start。
        queries_before = list(server.queries)
        run(worker, submitted.command.id)
        assert len(server.starts) == 1
        assert server.queries == queries_before


def test_completed_without_readable_checkpoint_keeps_guard_and_records_diagnostic(
    engine: Engine,
    tmp_path: Path,
    selections: tuple[DdmInputSelection, DdmInputSelection],
) -> None:
    submitted = _submit(
        engine, idempotency_key="result-empty", training=selections[0], validation=selections[1]
    )
    results_root = tmp_path / "results"
    objects_root = tmp_path / "objects"
    with DdmHttpServer() as server:
        bound = run(executor(engine, tmp_path, server), submitted.command.id)
        assert bound.command is not None
        assert bound.command.base_job_id is not None
        base_job_id = bound.command.base_job_id
        # 空 checkpoint：绝不登记为空/假结果。
        _write_base_result_files(
            results_root=results_root,
            dataset_root=tmp_path,
            base_job_id=base_job_id,
            training_dataset_id=f"ddm-{submitted.command.id}-training",
            validation_dataset_id=f"ddm-{submitted.command.id}-validation",
            checkpoint_bytes=b"",
        )
        server.jobs[base_job_id].status = "completed"
        worker = _result_executor(
            engine, tmp_path, server, results_root=results_root, objects_root=objects_root
        )
        run(worker, submitted.command.id)
        stored = _reload(engine, submitted.command.id)
        assert stored.status is TrainingCommandStatus.BOUND
        assert stored.result_status == "not_registered"
        assert stored.result_failure_code is not None
        assert stored.base_observation is not None
        assert stored.base_observation["status"] == "completed"
        assert len(server.starts) == 1


def test_reader_parameter_mismatch_never_registers_result(
    engine: Engine,
    tmp_path: Path,
    selections: tuple[DdmInputSelection, DdmInputSelection],
) -> None:
    submitted = _submit(
        engine, idempotency_key="result-mismatch", training=selections[0], validation=selections[1]
    )
    results_root = tmp_path / "results"
    objects_root = tmp_path / "objects"
    with DdmHttpServer() as server:
        bound = run(executor(engine, tmp_path, server), submitted.command.id)
        assert bound.command is not None
        assert bound.command.base_job_id is not None
        base_job_id = bound.command.base_job_id
        _write_base_result_files(
            results_root=results_root,
            dataset_root=tmp_path,
            base_job_id=base_job_id,
            training_dataset_id=f"ddm-{submitted.command.id}-training",
            validation_dataset_id=f"ddm-{submitted.command.id}-validation",
            reader_overrides={"frames_per_side": 999},
        )
        server.jobs[base_job_id].status = "completed"
        worker = _result_executor(
            engine, tmp_path, server, results_root=results_root, objects_root=objects_root
        )
        run(worker, submitted.command.id)
        stored = _reload(engine, submitted.command.id)
        assert stored.result_status == "not_registered"
        assert stored.result_failure_code == "RESULT_UNREADABLE"
        assert not (objects_root / "ddm-results" / str(submitted.command.id)).exists()


def test_late_result_writer_cannot_replace_registered_result(
    engine: Engine,
    tmp_path: Path,
    selections: tuple[DdmInputSelection, DdmInputSelection],
) -> None:
    submitted = _submit(
        engine, idempotency_key="result-late", training=selections[0], validation=selections[1]
    )
    results_root = tmp_path / "results"
    objects_root = tmp_path / "objects"
    with DdmHttpServer() as server:
        bound = run(executor(engine, tmp_path, server), submitted.command.id)
        assert bound.command is not None
        assert bound.command.base_job_id is not None
        base_job_id = bound.command.base_job_id
        _write_base_result_files(
            results_root=results_root,
            dataset_root=tmp_path,
            base_job_id=base_job_id,
            training_dataset_id=f"ddm-{submitted.command.id}-training",
            validation_dataset_id=f"ddm-{submitted.command.id}-validation",
        )
        server.jobs[base_job_id].status = "completed"
        worker = _result_executor(
            engine, tmp_path, server, results_root=results_root, objects_root=objects_root
        )
        run(worker, submitted.command.id)
        original = _reload(engine, submitted.command.id)
        assert original.result_status == "registered"
        with session_factory(engine)() as session:
            replaced = PostgresDatasetRepository(session).register_training_result(
                command_id=submitted.command.id,
                base_job_id=base_job_id,
                config_object_key="ddm-results/late/config.yaml",
                config_sha256="f" * 64,
                config_size=1,
                checkpoint_object_key="ddm-results/late/checkpoint.ckpt",
                checkpoint_sha256="e" * 64,
                checkpoint_size=1,
                now=NOW,
            )
            session.commit()
        assert replaced is False
        after = _reload(engine, submitted.command.id)
        assert after.result_config_sha256 == original.result_config_sha256
        assert after.result_generation == original.result_generation
        assert after.result_checkpoint_object_key is not None
        checkpoint_path = objects_root / after.result_checkpoint_object_key
        assert checkpoint_path.is_file()


@pytest.mark.parametrize(
    "damage",
    [
        "missing-parameter",
        "annotation-path",
        "mutated-media",
        "checkpoint-symlink",
        "merged-identity",
    ],
)
def test_result_requires_actual_frozen_inputs_and_safe_source(
    engine: Engine,
    tmp_path: Path,
    selections: tuple[DdmInputSelection, DdmInputSelection],
    damage: str,
) -> None:
    submitted = _submit(
        engine, idempotency_key=f"source-{damage}", training=selections[0], validation=selections[1]
    )
    results_root, objects_root = tmp_path / "results", tmp_path / "objects"
    with DdmHttpServer() as server:
        bound = run(executor(engine, tmp_path, server), submitted.command.id)
        assert bound.command is not None
        assert bound.command.base_job_id is not None
        job_id = bound.command.base_job_id
        training_id = f"ddm-{submitted.command.id}-training"
        _write_base_result_files(
            results_root=results_root,
            dataset_root=tmp_path,
            base_job_id=job_id,
            training_dataset_id=training_id,
            validation_dataset_id=f"ddm-{submitted.command.id}-validation",
        )
        config_path = results_root / job_id / "train" / job_id / "config.yaml"
        doc = yaml.safe_load(config_path.read_text())
        if damage == "missing-parameter":
            del doc["dataset_config"]["frames_per_side"]
        elif damage == "annotation-path":
            doc["dataset_config"]["train_config"]["anno_path"] = "/outside/annotation.json"
        elif damage == "merged-identity":
            doc["training_config"]["exp_name"] = str(uuid4())
        elif damage == "mutated-media":
            next((tmp_path / training_id).glob("*.mp4")).write_bytes(b"different-media")
        else:
            checkpoint = config_path.parent / "last.ckpt"
            checkpoint.unlink()
            outside = tmp_path / "outside.ckpt"
            outside.write_bytes(b"unrelated-checkpoint")
            checkpoint.symlink_to(outside)
        config_path.write_text(yaml.safe_dump(doc))
        server.jobs[job_id].status = "completed"
        run(
            _result_executor(
                engine, tmp_path, server, results_root=results_root, objects_root=objects_root
            ),
            submitted.command.id,
        )
        observed = _reload(engine, submitted.command.id)
        assert observed.status is TrainingCommandStatus.BOUND
        assert observed.result_status == "not_registered"
        assert observed.result_failure_code == "RESULT_UNREADABLE"


def test_job_finish_rejection_rolls_back_result_and_late_writer_preserves_winner(
    engine: Engine,
    tmp_path: Path,
    selections: tuple[DdmInputSelection, DdmInputSelection],
) -> None:
    submitted = _submit(
        engine, idempotency_key="fenced-result", training=selections[0], validation=selections[1]
    )
    results_root, objects_root = tmp_path / "results", tmp_path / "objects"
    with DdmHttpServer() as server:
        bound = run(executor(engine, tmp_path, server), submitted.command.id)
        assert bound.command is not None
        assert bound.command.base_job_id is not None
        job_id = bound.command.base_job_id
        _write_base_result_files(
            results_root=results_root,
            dataset_root=tmp_path,
            base_job_id=job_id,
            training_dataset_id=f"ddm-{submitted.command.id}-training",
            validation_dataset_id=f"ddm-{submitted.command.id}-validation",
        )
        server.jobs[job_id].status = "completed"
        winner = _result_executor(
            engine, tmp_path, server, results_root=results_root, objects_root=objects_root
        )

        def commit(session: object) -> bool:
            cast(Session, session).commit()
            return True

        with session_factory(engine).begin() as session:
            jobs = PostgresJobRepository(session)
            old_lease = jobs.mark_running(job_id=submitted.job.id, now=NOW)
            assert old_lease is not None
            jobs.recover_stale_running(
                job_type=JobType.DATASET_DDM_TRAINING,
                now=NOW + timedelta(seconds=2),
                stale_after_seconds=1,
            )
            current_lease = jobs.mark_running(
                job_id=submitted.job.id, now=NOW + timedelta(seconds=3)
            )
            assert current_lease is not None

        def finish_old(session: object, result: TrainingJobResult) -> bool:
            return PostgresJobRepository(cast(Session, session)).finish(
                job_id=submitted.job.id,
                status=JobStatus.SUCCEEDED.value,
                failure_code=None,
                now=NOW + timedelta(seconds=10),
                expected_updated_at=old_lease.updated_at,
            )

        def finish_current(session: object, result: TrainingJobResult) -> bool:
            return PostgresJobRepository(cast(Session, session)).finish(
                job_id=submitted.job.id,
                status=JobStatus.SUCCEEDED.value,
                failure_code=None,
                now=NOW + timedelta(seconds=11),
                expected_updated_at=current_lease.updated_at,
            )

        winner.execute_job(
            command_id=submitted.command.id,
            now=NOW + timedelta(seconds=5),
            resolve_current_caller=lambda _: _caller(Permission.DATASET_EDIT),
            finish_job=finish_old,
            commit_transaction=commit,
        )
        rejected = _reload(engine, submitted.command.id)
        assert rejected.status is TrainingCommandStatus.BOUND
        assert rejected.result_status == "not_registered"
        assert not list(objects_root.rglob("*.ckpt"))

        class LateStorage(LocalFileObjectStorage):
            @contextmanager
            def writing(self, *, object_key: str) -> Iterator[BinaryIO]:
                if object_key.endswith("checkpoint.ckpt"):
                    winner.execute_job(
                        command_id=submitted.command.id,
                        now=NOW + timedelta(seconds=11),
                        resolve_current_caller=lambda _: _caller(Permission.DATASET_EDIT),
                        finish_job=finish_current,
                        commit_transaction=commit,
                    )
                    (results_root / job_id / "train" / job_id / "last.ckpt").write_bytes(
                        b"late-bytes"
                    )
                with super().writing(object_key=object_key) as sink:
                    yield sink

        late = executor(
            engine,
            tmp_path,
            server,
            result_volume=LocalDdmResultVolume(results_root=results_root, dataset_root=tmp_path),
            storage_factory=lambda: LateStorage(objects_root),
        )
        late.execute_job(
            command_id=submitted.command.id,
            now=NOW + timedelta(seconds=10),
            resolve_current_caller=lambda _: _caller(Permission.DATASET_EDIT),
            finish_job=finish_old,
            commit_transaction=commit,
        )
        published = _reload(engine, submitted.command.id)
        assert published.status is TrainingCommandStatus.COMPLETED
        assert published.result_checkpoint_object_key is not None
        assert (
            objects_root / published.result_checkpoint_object_key
        ).read_bytes() == b"real-checkpoint-bytes"
        assert published.result_checkpoint_sha256 == sha256(b"real-checkpoint-bytes").hexdigest()
        assert len(list(objects_root.rglob("*.ckpt"))) == 1


def test_cancelled_metadata_keeps_original_job_and_inflight_guard(
    engine: Engine,
    tmp_path: Path,
    selections: tuple[DdmInputSelection, DdmInputSelection],
) -> None:
    submitted = _submit(
        engine,
        idempotency_key="cancel-unverified",
        training=selections[0],
        validation=selections[1],
    )
    with DdmHttpServer() as server:
        bound = run(executor(engine, tmp_path, server), submitted.command.id)
        assert bound.command is not None
        assert bound.command.base_job_id is not None
        job_id = bound.command.base_job_id
        server.jobs[job_id].status = "cancelled"
        cancelled = run(executor(engine, tmp_path, server), submitted.command.id)
        assert cancelled.command is not None
        assert cancelled.command.status is TrainingCommandStatus.BOUND
        stored = _reload(engine, submitted.command.id)
        assert stored.base_observation is not None
        assert stored.base_observation["status"] == "cancelled"
        assert stored.observation_failure_code == "BASE_CANCELLATION_UNVERIFIED"
        server.jobs[job_id].status = "running"
        run(executor(engine, tmp_path, server), submitted.command.id)
        resumed = _reload(engine, submitted.command.id)
        assert resumed.status is TrainingCommandStatus.BOUND
        assert resumed.base_observation is not None
        assert resumed.base_observation["status"] == "running"
        assert resumed.observation_failure_code is None
        assert len(server.starts) == 1


def test_observation_failure_preserves_last_observation_and_raw_status(
    engine: Engine,
    tmp_path: Path,
    selections: tuple[DdmInputSelection, DdmInputSelection],
) -> None:
    submitted = _submit(
        engine, idempotency_key="observe-fail", training=selections[0], validation=selections[1]
    )
    with DdmHttpServer() as server:
        bound = run(executor(engine, tmp_path, server), submitted.command.id)
        assert bound.command is not None
        assert bound.command.base_job_id is not None
        base_job_id = bound.command.base_job_id
        server.jobs[base_job_id].status = "running"
        run(executor(engine, tmp_path, server), submitted.command.id)
        observed = _reload(engine, submitted.command.id)
        assert observed.base_observation is not None
        assert observed.base_observation["status"] == "running"
        last_observed = observed.last_observed_at
        assert last_observed is not None
        server.query_error = True
        failed = run(executor(engine, tmp_path, server), submitted.command.id)
        assert failed.outcome is TrainingDispatchOutcome.RESULT_UNKNOWN
        stored = _reload(engine, submitted.command.id)
        # 查询失败不清空基座原状态与最后成功观测时刻，也不表示完成。
        assert stored.status is TrainingCommandStatus.BOUND
        assert stored.base_observation is not None
        assert stored.base_observation["status"] == "running"
        assert stored.last_observed_at == last_observed
        assert stored.observation_failure_code == "BASE_OBSERVATION_UNAVAILABLE"
        assert len(server.starts) == 1
