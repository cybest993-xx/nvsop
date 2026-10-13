"""完整 dataset execute seam：真实 PostgreSQL 与原 DDM 查询 HTTP handler。"""

from __future__ import annotations

import importlib.util
import threading
from collections.abc import Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from typing import Any, BinaryIO, cast
from uuid import UUID, uuid4

import pytest
from _integration_support import cleanup_dataset
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker
from test_ddm_training_command import NOW, _caller, _FakeStorage, _seed_passing_ddm, _submit

from factory_sop.auth.api import Caller, Permission
from factory_sop.dataset.adapters.ddm import NvidiaDdmAnnotationGenerator
from factory_sop.dataset.adapters.ddm_execution import (
    DdmTrainingBackend,
    DdmTrainingDispatchOutcome,
    DdmTrainingDispatchResult,
    HttpDdmTrainingBackend,
    PostgresDdmTrainingExecutor,
)
from factory_sop.dataset.adapters.repository import PostgresDatasetRepository
from factory_sop.dataset.model import DdmTrainingCommand
from factory_sop.dataset.storage import ObjectStorage
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
