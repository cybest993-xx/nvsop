"""VLM 训练命令接入的真实证据：中心受理 → 真实启动 → 查询 → 产物登记。

命令与 outbox 事实写入真实 PostgreSQL；基座是 ``cr-training-ms`` 的原 handler（只有 start
是故障注入边界），输入材料化、实际消费核对与结果清单都作用在真实临时目录与真实本地存储上。
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from _integration_support import RedisServer, cleanup_dataset, row, settings_for
from redis import Redis
from sqlalchemy import Engine
from sqlalchemy.orm import Session as DatabaseSession
from test_ddm_training_command import (
    ACTOR_ID,
    _caller,
    _FakeMediaProbe,
    _noop_dispatch,
)

from factory_sop.auth.permissions import Permission
from factory_sop.dataset.adapters.dependencies import vlm_executor
from factory_sop.dataset.adapters.repository import PostgresDatasetRepository
from factory_sop.dataset.adapters.storage import LocalFileObjectStorage
from factory_sop.dataset.adapters.training_execution import (
    PostgresTrainingCommandExecutor,
    TrainingDispatchOutcome,
    TrainingDispatchResult,
)
from factory_sop.dataset.adapters.vlm import NvidiaVlmJsonDiscovery
from factory_sop.dataset.adapters.vlm_execution import (
    HttpVlmTrainingBackend,
    VlmTrainingServiceAdapter,
)
from factory_sop.dataset.api import TrainingJobExecutor
from factory_sop.dataset.model import (
    ActionListRevision,
    DatasetMember,
    MemberStatus,
    TrainingCommand,
    TrainingCommandStatus,
    TrainingDataset,
    TrainingResultStatus,
    TrainingService,
    UsageKind,
    VlmCandidateKind,
    VlmMediaReference,
)
from factory_sop.dataset.storage import ObjectStorage
from factory_sop.dataset.usecases.training_command import (
    TrainingCommandResult,
    cancel_training_command,
)
from factory_sop.dataset.usecases.usage import (
    VLM_RECORDS_FILENAME,
    VlmInputSelection,
    begin_usage_check,
    complete_usage_check,
    register_vlm_candidate,
    request_usage_check,
    run_usage_check,
)
from factory_sop.dataset.usecases.vlm_command import request_vlm_training_command
from factory_sop.job.adapters.dispatcher import ArqJobDispatcher
from factory_sop.job.adapters.repository import PostgresUsageJobQueue
from factory_sop.job.adapters.worker import dispatch_pending_jobs
from factory_sop.job.api import JobType
from factory_sop.persistence import session_factory
from factory_sop.settings import Settings

ROOT = Path(__file__).resolve().parents[4]
_spec = importlib.util.spec_from_file_location(
    "vlm_http_fixture", ROOT / "tests/contract/base/vlm_http_fixture.py"
)
assert _spec is not None
assert _spec.loader is not None
_fixture = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fixture)
VlmHttpServer: Any = _fixture.VlmHttpServer

NOW = datetime(2026, 10, 13, 1, 0, tzinfo=UTC)
_OBJECT_KEY = "datasets/vlm-source.mp4"
_SOURCE_BYTES = b"v" * 128
_SOURCE_SHA256 = hashlib.sha256(_SOURCE_BYTES).hexdigest()
_MODEL_SOURCE = "/models/cosmos-reason-7b"


class _VlmReader:
    """只在工作区里确认记录可加载；真实读取器契约由 contract 套件覆盖。"""

    def validate(self, *, workspace: Path, annotation_filename: str) -> None:
        assert (workspace / annotation_filename).is_file()


def _storage(root: Path) -> LocalFileObjectStorage:
    return LocalFileObjectStorage(root)


def _seed_object(root: Path) -> None:
    target = root / _OBJECT_KEY
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(_SOURCE_BYTES)


def _seed_vlm_dataset(
    session: DatabaseSession, *, label: str, dataset_id: UUID, now: datetime
) -> UUID:
    """写入一个已登记视频与动作列表，返回 member id。"""
    member_id = uuid4()
    datasets = PostgresDatasetRepository(session)
    datasets.add_dataset(
        TrainingDataset(
            id=dataset_id,
            name=f"vlm-{label}",
            created_by=ACTOR_ID,
            updated_by=ACTOR_ID,
            created_at=now,
            updated_at=now,
        )
    )
    datasets.add_member(
        DatasetMember(
            id=member_id,
            dataset_id=dataset_id,
            original_filename=f"{label}.mp4",
            source="integration",
            declared_size=len(_SOURCE_BYTES),
            declared_sha256=_SOURCE_SHA256,
            current_attempt_id=uuid4(),
            status=MemberStatus.REGISTERED,
            actual_size=len(_SOURCE_BYTES),
            actual_sha256=_SOURCE_SHA256,
            duration_seconds=10.0,
            codec="h264",
            container="mp4",
            object_key=_OBJECT_KEY,
            validation_job_id=None,
            failure_code=None,
            failure_detail=None,
            recovery_action=None,
            created_by=ACTOR_ID,
            updated_by=ACTOR_ID,
            created_at=now,
            updated_at=now,
        )
    )
    assert datasets.add_action_list(
        ActionListRevision(
            dataset_id=dataset_id,
            revision=1,
            actions=("(1) 取料", "(2) 安装"),
            created_by=ACTOR_ID,
            created_at=now,
        )
    )
    return member_id


def _passing_vlm_check(
    session: DatabaseSession,
    *,
    dataset_id: UUID,
    member_id: UUID,
    storage_root: Path,
    now: datetime,
) -> UUID:
    """把合成 VLM 候选跑过真实用途用例，返回已通过且当前可用的用途检查身份。"""
    datasets = PostgresDatasetRepository(session)
    jobs = PostgresUsageJobQueue(session, _noop_dispatch)
    candidate = register_vlm_candidate(
        dataset_id=dataset_id,
        kind=VlmCandidateKind.GQA,
        action_list_revision=1,
        records=(
            {
                "conversations": [
                    {"from": "human", "value": "<video>操作员在做什么？"},
                    {"from": "gpt", "value": "Picking material."},
                ],
                "video": "vlm-source.mp4",
            },
        ),
        media=(
            VlmMediaReference(
                key="vlm-source.mp4",
                member_id=member_id,
                source_object_key=_OBJECT_KEY,
                source_sha256=_SOURCE_SHA256,
            ),
        ),
        expected_revision=0,
        caller=_caller(Permission.DATASET_EDIT),
        now=now,
        datasets=datasets,
    )
    requested = request_usage_check(
        dataset_id=dataset_id,
        kind=UsageKind.VLM,
        candidate_id=candidate.id,
        caller=_caller(Permission.DATASET_EDIT),
        now=now,
        datasets=datasets,
        jobs=jobs,
    )
    target = begin_usage_check(job=requested.job, datasets=datasets, now=now + timedelta(seconds=1))
    assert target is not None
    validation = run_usage_check(
        target=target,
        storage=cast(ObjectStorage, _storage(storage_root)),
        media_probe=cast(Any, _FakeMediaProbe()),
        annotation_volume=None,
        vlm_reader=_VlmReader(),
    )
    assert validation.passed is True, validation.issues
    check = complete_usage_check(
        target=target,
        validation=validation,
        now=now + timedelta(seconds=2),
        datasets=datasets,
    )
    assert check.status.value == "passed"
    return check.id


@pytest.fixture
def vlm_dataset(
    engine: Engine, dataset_storage_root: Path, tmp_path: Path
) -> Iterator[tuple[UUID, UUID]]:
    """一套已通过用途检查的 VLM 输入，并确认对象真实落在本地存储卷上。"""
    _seed_object(dataset_storage_root)
    dataset_id = uuid4()
    with session_factory(engine).begin() as session:
        member_id = _seed_vlm_dataset(session, label="train", dataset_id=dataset_id, now=NOW)
        check_id = _passing_vlm_check(
            session,
            dataset_id=dataset_id,
            member_id=member_id,
            storage_root=dataset_storage_root,
            now=NOW,
        )
    try:
        yield dataset_id, check_id
    finally:
        cleanup_dataset(engine, dataset_id)


def _submit(
    engine: Engine,
    *,
    idempotency_key: str,
    selection: VlmInputSelection,
) -> TrainingCommandResult:
    with session_factory(engine)() as session:
        result = request_vlm_training_command(
            selection=selection,
            idempotency_key=idempotency_key,
            caller=_caller(Permission.DATASET_EDIT),
            now=datetime.now(UTC),
            datasets=PostgresDatasetRepository(session),
            jobs=PostgresUsageJobQueue(session, _noop_dispatch),
        )
        session.commit()
        return result


def _settings(
    engine: Engine,
    *,
    tmp_path: Path,
    dataset_storage_root: Path,
    server: Any = None,  # noqa: ANN401 — 动态加载的 HTTP fixture
    redis_url: str | None = None,
) -> Settings:
    """按部署形态构造 VLM 训练配置；未给基座地址时保持未配置。"""
    dataset_root = tmp_path / "vlm-inputs"
    results_root = tmp_path / "vlm-results"
    dataset_root.mkdir(exist_ok=True)
    results_root.mkdir(exist_ok=True)
    return settings_for(engine, storage_root=dataset_storage_root, redis_url=redis_url).model_copy(
        update={
            "vlm_training_backend_url": None if server is None else server.url,
            "vlm_training_dataset_root": str(dataset_root),
            "vlm_training_results_root": str(results_root),
        }
    )


def _executor(engine: Engine, settings: Settings) -> TrainingJobExecutor:
    """用生产组合根装配 VLM 执行器：真实存储、真实结果卷与基座原始契约。"""
    return vlm_executor(settings, session_factory(engine))


def _volume_less_executor(
    engine: Engine,
    settings: Settings,
    *,
    server: Any,  # noqa: ANN401 — HTTP fixture
    dataset_storage_root: Path,
) -> PostgresTrainingCommandExecutor:
    """结果卷未配置的注入形态；生产组合根总是提供结果卷。"""
    assert settings.vlm_training_dataset_root is not None
    return PostgresTrainingCommandExecutor(
        factory=session_factory(engine),
        storage_factory=lambda: _storage(dataset_storage_root),
        adapter=VlmTrainingServiceAdapter(
            backend=HttpVlmTrainingBackend(base_url=server.url, timeout_seconds=2),
            backend_target=server.url,
            dataset_root=Path(settings.vlm_training_dataset_root),
            annotation_volume=lambda: None,
            result_volume=None,
            discovery=NvidiaVlmJsonDiscovery(),
        ),
    )


def _run(worker: TrainingJobExecutor, command_id: UUID) -> TrainingDispatchResult:
    return cast(PostgresTrainingCommandExecutor, worker).execute(
        command_id=command_id,
        now=NOW,
        resolve_current_caller=lambda user_id: _caller(Permission.DATASET_EDIT),
    )


def _load(engine: Engine, command_id: UUID) -> TrainingCommand:
    with session_factory(engine)() as session:
        command = PostgresDatasetRepository(session).training_command_by_id(command_id)
    assert command is not None
    return command


def _worker_ctx(engine: Engine, settings: Settings) -> dict[str, Any]:
    """worker 的真实上下文：训练执行器按任务类型由组合层给出。"""
    factory = session_factory(engine)
    return {
        "settings": settings,
        "session_factory": factory,
        "dispatcher": ArqJobDispatcher.from_settings(settings, session_factory=factory),
        "training_executors": {
            JobType.DATASET_VLM_TRAINING: _executor(engine, settings),
        },
        "current_caller_resolver": lambda user_id: _caller(Permission.DATASET_EDIT),
        "blocking_job_slots": asyncio.Semaphore(1),
    }


def test_vlm_command_is_held_until_the_vlm_backend_is_configured(
    engine: Engine,
    vlm_dataset: tuple[UUID, UUID],
    tmp_path: Path,
    redis_server: RedisServer,
    redis_client: Redis,
) -> None:
    """未配置 VLM 基座时命令只保留 outbox 事实，不投递可实际 start 的函数。"""
    dataset_id, check_id = vlm_dataset
    submitted = _submit(
        engine,
        idempotency_key="vlm-held",
        selection=VlmInputSelection(dataset_id=dataset_id, usage_check_id=check_id),
    )
    assert submitted.command.status is TrainingCommandStatus.PENDING
    assert submitted.job.job_type is JobType.DATASET_VLM_TRAINING

    with VlmHttpServer() as server:
        unconfigured = _worker_ctx(
            engine,
            _settings(
                engine,
                tmp_path=tmp_path,
                dataset_storage_root=tmp_path / "files",
                redis_url=redis_server.url,
            ),
        )
        asyncio.run(dispatch_pending_jobs(unconfigured))
        assert not redis_client.keys(f"arq:job:{submitted.job.id}:*")
        assert server.starts == []

        configured = _worker_ctx(
            engine,
            _settings(
                engine,
                tmp_path=tmp_path,
                dataset_storage_root=tmp_path / "files",
                server=server,
                redis_url=redis_server.url,
            ),
        )
        asyncio.run(dispatch_pending_jobs(configured))
        assert redis_client.keys(f"arq:job:{submitted.job.id}:*")


def test_vlm_start_binds_real_job_and_redelivery_only_polls_original(
    engine: Engine, tmp_path: Path, dataset_storage_root: Path, vlm_dataset: tuple[UUID, UUID]
) -> None:
    """真实 start 返回任务身份后绑定；重投递只查询原任务，绝不再次启动。"""
    dataset_id, check_id = vlm_dataset
    submitted = _submit(
        engine,
        idempotency_key="vlm-start",
        selection=VlmInputSelection(dataset_id=dataset_id, usage_check_id=check_id),
    )
    identity = f"vlm-{submitted.command.id}"
    with VlmHttpServer() as server:
        settings = _settings(
            engine,
            tmp_path=tmp_path,
            dataset_storage_root=dataset_storage_root,
            server=server,
        )
        worker = _executor(engine, settings)
        result = _run(worker, submitted.command.id)
        assert result.outcome is TrainingDispatchOutcome.BOUND
        assert server.starts == [identity]
        bound = _load(engine, submitted.command.id)
        assert bound.status is TrainingCommandStatus.BOUND
        assert bound.base_job_id is not None
        assert bound.base_observation == {
            "status": "queued",
            "progress": 0.0,
            "current_step": 0,
            "total_steps": 10,
        }
        assert bound.dispatch_facts is not None
        facts = bound.dispatch_facts
        assert facts == {
            "input_id": str(bound.payload["input_id"]),
            "dataset_id": identity,
            "usage_check_id": str(check_id),
            "candidate_id": bound.payload["candidate_id"],
            "input_digest": bound.payload["input_digest"],
            "base_commit": bound.payload["base_commit"],
            "contract_version": bound.payload["contract_version"],
            "records_sha256": bound.payload["records_sha256"],
            "consumption": facts["consumption"],
        }
        assert facts["consumption"]["records_filename"] == VLM_RECORDS_FILENAME

        server.queries.clear()
        again = _run(worker, submitted.command.id)
        assert again.outcome is TrainingDispatchOutcome.BOUND
        assert server.starts == [identity]
        assert server.queries == [bound.base_job_id]


def test_vlm_lost_start_response_recovers_by_unique_identity_without_resend(
    engine: Engine, tmp_path: Path, dataset_storage_root: Path, vlm_dataset: tuple[UUID, UUID]
) -> None:
    """基座已接受但响应丢失：保留结果未知，下一轮只按原查询接口唯一匹配并绑定。"""
    dataset_id, check_id = vlm_dataset
    submitted = _submit(
        engine,
        idempotency_key="vlm-lost",
        selection=VlmInputSelection(dataset_id=dataset_id, usage_check_id=check_id),
    )
    identity = f"vlm-{submitted.command.id}"
    with VlmHttpServer() as server:
        settings = _settings(
            engine,
            tmp_path=tmp_path,
            dataset_storage_root=dataset_storage_root,
            server=server,
        )
        worker = _executor(engine, settings)
        server.start_error = 502
        first = _run(worker, submitted.command.id)
        assert first.outcome is TrainingDispatchOutcome.RESULT_UNKNOWN
        assert server.starts == [identity]
        unknown = _load(engine, submitted.command.id)
        assert unknown.status is TrainingCommandStatus.RESULT_UNKNOWN
        assert unknown.base_job_id is None
        assert unknown.failure_code == "BASE_RESULT_UNKNOWN"

        # 恢复只查询：唯一新任务恰好属于本命令时才绑定，start 不再调用。
        server.start_error = None
        recovered = _run(worker, submitted.command.id)
        assert recovered.outcome is TrainingDispatchOutcome.BOUND
        assert server.starts == [identity]
        assert _load(engine, submitted.command.id).base_job_id is not None


def test_vlm_ambiguous_recovery_keeps_inflight_protection(
    engine: Engine, tmp_path: Path, dataset_storage_root: Path, vlm_dataset: tuple[UUID, UUID]
) -> None:
    """两个同样匹配的新任务不能证明归属：保持结果未知与在途保护，不重发 start。"""
    dataset_id, check_id = vlm_dataset
    submitted = _submit(
        engine,
        idempotency_key="vlm-ambiguous",
        selection=VlmInputSelection(dataset_id=dataset_id, usage_check_id=check_id),
    )
    identity = f"vlm-{submitted.command.id}"
    with VlmHttpServer() as server:
        settings = _settings(
            engine,
            tmp_path=tmp_path,
            dataset_storage_root=dataset_storage_root,
            server=server,
        )
        worker = _executor(engine, settings)
        # 发送前基座已有另一个同身份任务：新任务仍被唯一匹配，但注入第二个后即无法归属。
        server.add_job(identity)
        server.start_error = 502
        assert _run(worker, submitted.command.id).outcome is TrainingDispatchOutcome.RESULT_UNKNOWN
        server.start_error = None
        server.add_job(identity)
        ambiguous = _run(worker, submitted.command.id)
        assert ambiguous.outcome is TrainingDispatchOutcome.RESULT_UNKNOWN
        assert ambiguous.failure_code == "DISPATCH_RESULT_UNVERIFIED"
        assert server.starts == [identity]
        assert _load(engine, submitted.command.id).status is TrainingCommandStatus.RESULT_UNKNOWN


def test_vlm_consumption_mismatch_refuses_dispatch_before_start(
    engine: Engine, tmp_path: Path, dataset_storage_root: Path, vlm_dataset: tuple[UUID, UUID]
) -> None:
    """基座会发现训练目录里的全部 JSON：多出的文件必须拒绝派发，且不调用 start。"""
    dataset_id, check_id = vlm_dataset
    submitted = _submit(
        engine,
        idempotency_key="vlm-extra-json",
        selection=VlmInputSelection(dataset_id=dataset_id, usage_check_id=check_id),
    )
    identity = f"vlm-{submitted.command.id}"
    dataset_root = tmp_path / "vlm-inputs"
    with VlmHttpServer() as server:
        injected = dataset_root / identity
        injected.mkdir(parents=True)
        (injected / "extra_manifest.json").write_text("{}", encoding="utf-8")
        settings = _settings(
            engine,
            tmp_path=tmp_path,
            dataset_storage_root=dataset_storage_root,
            server=server,
        )
        worker = _executor(engine, settings)
        result = _run(worker, submitted.command.id)
        assert result.outcome is TrainingDispatchOutcome.BLOCKED
        assert result.failure_code == "DISPATCH_PREPARATION_IN_FLIGHT"
        assert server.starts == []
        assert _load(engine, submitted.command.id).status is TrainingCommandStatus.PENDING


def _write_result_tree(
    *,
    dataset_root: Path,
    results_root: Path,
    identity: str,
    base_job_id: str,
    with_checkpoint: bool = True,
) -> Path:
    """写出一份与基座 dump_toml + cosmos-rl 输出同形的合并配置与步目录。"""
    job_directory = results_root / base_job_id
    step_directory = job_directory / "run-20261013" / "safetensors" / "step_100"
    step_directory.mkdir(parents=True, exist_ok=True)
    # 与基座 dump_toml 写出的合并配置同形：输出目录、试验身份、实际消费输入与模型来源。
    (job_directory / f"{base_job_id}.toml").write_text(
        f"[train]\n"
        f'output_dir = "{job_directory}"\n'
        f"[train.train_policy.dataset]\n"
        f'name = ["{dataset_root / identity / VLM_RECORDS_FILENAME}"]\n'
        f'split = ["vlm_candidate_records"]\n'
        f"[logging]\n"
        f'experiment_name = "{base_job_id}"\n'
        f"[policy]\n"
        f'model_name_or_path = "{_MODEL_SOURCE}"\n',
        encoding="utf-8",
    )
    if with_checkpoint:
        (step_directory / "model.safetensors").write_bytes(b"weights")
        (step_directory / "train_config.toml").write_text(
            "global_batch_size = 16\n", encoding="utf-8"
        )
    return step_directory


def test_vlm_completed_registers_config_and_checkpoint_manifest(
    engine: Engine, tmp_path: Path, dataset_storage_root: Path, vlm_dataset: tuple[UUID, UUID]
) -> None:
    """基座完成后核实真实文件、登记不可变清单并释放在途保护；重投递不再查询。"""
    dataset_id, check_id = vlm_dataset
    submitted = _submit(
        engine,
        idempotency_key="vlm-result",
        selection=VlmInputSelection(dataset_id=dataset_id, usage_check_id=check_id),
    )
    identity = f"vlm-{submitted.command.id}"
    dataset_root = tmp_path / "vlm-inputs"
    results_root = tmp_path / "vlm-results"
    with VlmHttpServer() as server:
        settings = _settings(
            engine,
            tmp_path=tmp_path,
            dataset_storage_root=dataset_storage_root,
            server=server,
        )
        worker = _executor(engine, settings)
        assert _run(worker, submitted.command.id).outcome is TrainingDispatchOutcome.BOUND
        bound = _load(engine, submitted.command.id)
        assert bound.base_job_id is not None
        server.complete_job(bound.base_job_id)
        step_directory = _write_result_tree(
            dataset_root=dataset_root,
            results_root=results_root,
            identity=identity,
            base_job_id=bound.base_job_id,
        )

        registered = _run(worker, submitted.command.id)
        assert registered.outcome is TrainingDispatchOutcome.BOUND
        final = _load(engine, submitted.command.id)
        assert final.status is TrainingCommandStatus.COMPLETED
        assert final.result_status is TrainingResultStatus.REGISTERED
        assert final.result_config_object_key is not None
        assert final.result_checkpoint_object_key is not None
        config_bytes = (dataset_storage_root / final.result_config_object_key).read_bytes()
        assert hashlib.sha256(config_bytes).hexdigest() == final.result_config_sha256
        manifest_bytes = (dataset_storage_root / final.result_checkpoint_object_key).read_bytes()
        assert hashlib.sha256(manifest_bytes).hexdigest() == final.result_checkpoint_sha256
        manifest = json.loads(manifest_bytes)
        assert manifest["job_id"] == bound.base_job_id
        assert manifest["step"] == 100
        assert manifest["checkpoint_directory"] == str(step_directory.relative_to(results_root))
        assert manifest["model_source"] == _MODEL_SOURCE
        assert manifest["input_identity"] == identity
        assert manifest["usage_check_id"] == str(check_id)
        assert {item["path"] for item in manifest["files"]} == {
            "model.safetensors",
            "train_config.toml",
        }
        for item in manifest["files"]:
            real = step_directory / item["path"]
            assert real.stat().st_size == item["size"]
            assert hashlib.sha256(real.read_bytes()).hexdigest() == item["sha256"]

        server.queries.clear()
        assert _run(worker, submitted.command.id).outcome is TrainingDispatchOutcome.BOUND
        assert server.queries == []


def test_vlm_missing_checkpoint_keeps_polling_without_repeating_start(
    engine: Engine, tmp_path: Path, dataset_storage_root: Path, vlm_dataset: tuple[UUID, UUID]
) -> None:
    """结果尚未写出不是失败：保留在途保护、记录可恢复诊断，绝不重发 start。"""
    dataset_id, check_id = vlm_dataset
    submitted = _submit(
        engine,
        idempotency_key="vlm-no-checkpoint",
        selection=VlmInputSelection(dataset_id=dataset_id, usage_check_id=check_id),
    )
    identity = f"vlm-{submitted.command.id}"
    results_root = tmp_path / "vlm-results"
    with VlmHttpServer() as server:
        settings = _settings(
            engine,
            tmp_path=tmp_path,
            dataset_storage_root=dataset_storage_root,
            server=server,
        )
        worker = _executor(engine, settings)
        assert _run(worker, submitted.command.id).outcome is TrainingDispatchOutcome.BOUND
        bound = _load(engine, submitted.command.id)
        assert bound.base_job_id is not None
        server.complete_job(bound.base_job_id)
        _write_result_tree(
            dataset_root=tmp_path / "vlm-inputs",
            results_root=results_root,
            identity=identity,
            base_job_id=bound.base_job_id,
            with_checkpoint=False,
        )

        pending = _run(worker, submitted.command.id)
        assert pending.outcome is TrainingDispatchOutcome.BOUND
        observed = _load(engine, submitted.command.id)
        assert observed.status is TrainingCommandStatus.BOUND
        assert observed.result_status is TrainingResultStatus.NOT_REGISTERED
        assert observed.result_failure_code == "RESULT_UNREADABLE"
        assert server.starts == [identity]
        assert not (dataset_storage_root / f"vlm-results/{submitted.command.id}").exists()


def test_vlm_cancel_request_calls_the_real_endpoint_without_claiming_stop(
    engine: Engine, tmp_path: Path, dataset_storage_root: Path, vlm_dataset: tuple[UUID, UUID]
) -> None:
    """已派发取消只调用基座真实入口并保留未核实状态：不把响应文案当成进程停止证明。"""
    dataset_id, check_id = vlm_dataset
    submitted = _submit(
        engine,
        idempotency_key="vlm-cancel",
        selection=VlmInputSelection(dataset_id=dataset_id, usage_check_id=check_id),
    )
    with VlmHttpServer() as server:
        settings = _settings(
            engine,
            tmp_path=tmp_path,
            dataset_storage_root=dataset_storage_root,
            server=server,
        )
        worker = _executor(engine, settings)
        assert _run(worker, submitted.command.id).outcome is TrainingDispatchOutcome.BOUND
        bound = _load(engine, submitted.command.id)
        assert bound.base_job_id is not None
        server.run_job(bound.base_job_id, process_pid=4242)

        with session_factory(engine)() as session:
            requested = cancel_training_command(
                service=TrainingService.VLM,
                dataset_id=dataset_id,
                command_id=submitted.command.id,
                caller=_caller(Permission.DATASET_EDIT),
                now=NOW,
                datasets=PostgresDatasetRepository(session),
            )
            session.commit()
        assert requested.status is TrainingCommandStatus.BOUND
        assert requested.cancel_requested_by is not None
        assert requested.cancel_response is None

        observed = _run(worker, submitted.command.id)
        assert observed.outcome is TrainingDispatchOutcome.BOUND
        cancelled = _load(engine, submitted.command.id)
        assert cancelled.status is TrainingCommandStatus.BOUND
        # 基座真实入口确实终止了进程树；中心仍不据此宣告命令已停止。
        assert server.terminated == [4242]
        assert cancelled.cancel_response is not None
        assert cancelled.cancel_response["outcome"] == "http"
        assert cancelled.cancel_response["http_status"] == 200
        assert cancelled.cancel_response["message_length"] > 0
        assert cancelled.cancel_response["message_sha256"]
        assert "已停止" not in (cancelled.cancel_detail or "")

        # 取消请求只核实一次：重复观测不再调用基座取消入口。
        assert _run(worker, submitted.command.id).outcome is TrainingDispatchOutcome.BOUND
        assert cancelled.cancel_response == _load(engine, submitted.command.id).cancel_response


def test_vlm_cancel_before_dispatch_is_terminal_without_calling_the_backend(
    engine: Engine, tmp_path: Path, dataset_storage_root: Path, vlm_dataset: tuple[UUID, UUID]
) -> None:
    """尚未进入派发的命令按未派发取消；基座从未被调用，也不产生任务。"""
    dataset_id, check_id = vlm_dataset
    submitted = _submit(
        engine,
        idempotency_key="vlm-cancel-pending",
        selection=VlmInputSelection(dataset_id=dataset_id, usage_check_id=check_id),
    )
    with VlmHttpServer() as server:
        settings = _settings(
            engine,
            tmp_path=tmp_path,
            dataset_storage_root=dataset_storage_root,
            server=server,
        )
        worker = _executor(engine, settings)
        with session_factory(engine)() as session:
            cancelled = cancel_training_command(
                service=TrainingService.VLM,
                dataset_id=dataset_id,
                command_id=submitted.command.id,
                caller=_caller(Permission.DATASET_EDIT),
                now=NOW,
                datasets=PostgresDatasetRepository(session),
            )
            session.commit()
        assert cancelled.status is TrainingCommandStatus.CANCELLED
        assert cancelled.cancelled_by == ACTOR_ID

        result = _run(worker, submitted.command.id)
        assert result.outcome is TrainingDispatchOutcome.CANCELLED
        assert server.starts == []
        assert server.queries == []


def test_vlm_result_registration_requires_a_configured_result_volume(
    engine: Engine, tmp_path: Path, dataset_storage_root: Path, vlm_dataset: tuple[UUID, UUID]
) -> None:
    """结果读取未配置时基座完成也不登记：保留在途保护与可诊断原因。"""
    dataset_id, check_id = vlm_dataset
    submitted = _submit(
        engine,
        idempotency_key="vlm-no-volume",
        selection=VlmInputSelection(dataset_id=dataset_id, usage_check_id=check_id),
    )
    with VlmHttpServer() as server:
        settings = _settings(
            engine,
            tmp_path=tmp_path,
            dataset_storage_root=dataset_storage_root,
            server=server,
        )
        worker = _volume_less_executor(
            engine, settings, server=server, dataset_storage_root=dataset_storage_root
        )
        assert _run(worker, submitted.command.id).outcome is TrainingDispatchOutcome.BOUND
        bound = _load(engine, submitted.command.id)
        assert bound.base_job_id is not None
        server.complete_job(bound.base_job_id)
        assert _run(worker, submitted.command.id).outcome is TrainingDispatchOutcome.BOUND
        observed = _load(engine, submitted.command.id)
        assert observed.status is TrainingCommandStatus.BOUND
        assert observed.result_failure_code == "RESULT_VOLUME_UNCONFIGURED"
        assert server.starts == [f"vlm-{submitted.command.id}"]


def test_vlm_command_row_records_service_and_job_type(
    engine: Engine, vlm_dataset: tuple[UUID, UUID]
) -> None:
    """持久事实按服务区分：同一数据集上 DDM 与 VLM 的幂等键互不影响。"""
    dataset_id, check_id = vlm_dataset
    submitted = _submit(
        engine,
        idempotency_key="shared-key",
        selection=VlmInputSelection(dataset_id=dataset_id, usage_check_id=check_id),
    )
    service, job_type = row(
        engine,
        "SELECT command.service, job.job_type FROM dataset_training_command AS command "
        "JOIN job_application_job AS job ON job.id = :job_id WHERE command.id = :command_id",
        job_id=submitted.job.id,
        command_id=submitted.command.id,
    )
    assert service == "vlm"
    assert job_type == JobType.DATASET_VLM_TRAINING.value
    assert submitted.command.service.value == "vlm"
