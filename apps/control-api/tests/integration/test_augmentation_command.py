"""S084：真实 PG/outbox、HTTP 契约与文件登记；基座计算输出使用合成夹具。"""

from __future__ import annotations

import asyncio
import importlib.util
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Thread
from types import TracebackType
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine
from test_ddm_training_command import _caller, _seed_ddm_rows

from factory_sop.auth.api import Permission
from factory_sop.dataset.adapters.augmentation import AugmentationVolume
from factory_sop.dataset.adapters.augmentation_execution import PostgresAugmentationExecutor
from factory_sop.dataset.adapters.media import FfprobeMediaProbe
from factory_sop.dataset.adapters.repository import PostgresDatasetRepository
from factory_sop.dataset.adapters.storage import LocalFileObjectStorage
from factory_sop.dataset.errors import DatasetRefusalCode, DatasetRefusedError
from factory_sop.dataset.model import AugmentationCommand
from factory_sop.dataset.storage import ObjectStorage
from factory_sop.dataset.usecases.augmentation import cancel_augmentation, request_augmentation
from factory_sop.job.adapters.repository import PostgresJobRepository
from factory_sop.job.adapters.worker import augment_dataset_job
from factory_sop.job.api import JobType
from factory_sop.persistence import session_factory

_spec = importlib.util.spec_from_file_location(
    "augmentation_http_fixture",
    Path(__file__).resolve().parents[4] / "tests/contract/base/augmentation_http_fixture.py",
)
assert _spec is not None
assert _spec.loader is not None
_fixture = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fixture)
NvidiaAugmentationHttpServer: Any = _fixture.NvidiaAugmentationHttpServer


def _seed(engine: Engine) -> tuple[UUID, UUID]:
    dataset_id, execution_id = uuid4(), uuid4()
    with session_factory(engine).begin() as session:
        _seed_ddm_rows(
            session,
            label=str(dataset_id),
            dataset_id=dataset_id,
            member_id=uuid4(),
            context_id=uuid4(),
            submission_id=uuid4(),
            execution_id=execution_id,
            now=datetime.now(UTC),
        )
    return dataset_id, execution_id


def test_acceptance_is_not_base_success_or_product_registration(engine: Engine) -> None:
    dataset_id, execution_id = _seed(engine)
    factory = session_factory(engine)
    with factory.begin() as session:
        command = request_augmentation(
            dataset_id=dataset_id,
            execution_id=execution_id,
            frame_drop_iterations=1,
            idempotency_key="first-command",
            caller=_caller(Permission.DATASET_EDIT),
            now=datetime.now(UTC),
            datasets=PostgresDatasetRepository(session),
            jobs=PostgresJobRepository(session),
        )
        assert command.status.value == "pending"
        assert command.base_augmentation_id is None
        assert command.result_status == "not_registered"
        assert command.job_id is not None
    with factory.begin() as session:
        replay = request_augmentation(
            dataset_id=dataset_id,
            execution_id=execution_id,
            frame_drop_iterations=1,
            idempotency_key="first-command",
            caller=_caller(Permission.DATASET_EDIT),
            now=datetime.now(UTC),
            datasets=PostgresDatasetRepository(session),
            jobs=PostgresJobRepository(session),
        )
        assert replay == command
        assert replay.input_snapshot["source_sha256"]
        assert replay.input_snapshot["source"] == "integration"
        assert replay.input_snapshot["annotation_execution_id"] == str(execution_id)


class AugmentationServer:
    """外部 NVIDIA HTTP 边界替身：计数身份、失败和丢响应，不执行增强算法。"""

    def __init__(self) -> None:
        self.starts: list[str] = []
        self.status = "running"
        self.drop_response = False
        self.query_error = False
        self.started = Event()
        self.post_gate: Event | None = None
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                from urllib.parse import parse_qs, urlsplit

                query = parse_qs(urlsplit(self.path).query)
                assert urlsplit(self.path).path == "/api/v1/augment"
                identity = f"{query['label_data_id'][0]}_augmented_{len(owner.starts)}"
                owner.starts.append(identity)
                owner.started.set()
                if owner.post_gate is not None:
                    assert owner.post_gate.wait(10)
                if owner.drop_response:
                    self.close_connection = True
                    return
                self.reply({"dataset_id": identity})

            def do_GET(self) -> None:
                if owner.query_error:
                    self.send_error(503)
                    return
                identity = self.path.rsplit("/", 1)[-1]
                self.reply({"dataset_id": identity, "status": owner.status, "progress": 100})

            def reply(self, body: dict[str, Any]) -> None:
                content = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> AugmentationServer:
        self.thread.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()


def _request(engine: Engine, dataset_id: UUID, execution_id: UUID, key: str) -> AugmentationCommand:
    with session_factory(engine).begin() as session:
        return request_augmentation(
            dataset_id=dataset_id,
            execution_id=execution_id,
            frame_drop_iterations=1,
            idempotency_key=key,
            caller=_caller(Permission.DATASET_EDIT),
            now=datetime.now(UTC),
            datasets=PostgresDatasetRepository(session),
            jobs=PostgresJobRepository(session),
        )


def _input(root: Path) -> None:
    from test_ddm_training_command import _SOURCE_BYTES

    directory = root / "data-1"
    directory.mkdir(parents=True)
    (directory / "actions.json").write_text(json.dumps({"actions": ["(1) 取料", "(2) 安装"]}))
    original = root / "registered" / "datasets" / "source-a.mp4"
    original.parent.mkdir(parents=True)
    original.write_bytes(_SOURCE_BYTES)
    (directory / "video-1_source.mp4").write_bytes(_SOURCE_BYTES)
    clips = directory / "video-1_source"
    clips.mkdir()
    for name in ("01_line-a_1_1.mp4", "02_line-a_1_2.mp4"):
        (clips / name).write_bytes(_SOURCE_BYTES)


def _run(
    engine: Engine, root: Path, server: AugmentationServer, command: AugmentationCommand
) -> AugmentationCommand:
    factory = session_factory(engine)
    ctx = {
        "session_factory": factory,
        "augmentation_executor": PostgresAugmentationExecutor(
            factory=factory,
            backend_url=server.url,
            timeout_seconds=5,
            volume_root=root,
            storage_factory=lambda: LocalFileObjectStorage(root / "registered"),
            media_probe=FfprobeMediaProbe(binary="ffprobe", timeout_seconds=10),
        ),
        "current_caller_resolver": lambda _user_id: _caller(Permission.DATASET_EDIT),
        "blocking_job_slots": asyncio.Semaphore(1),
    }
    asyncio.run(augment_dataset_job(ctx, str(command.job_id)))
    with factory() as session:
        observed = PostgresDatasetRepository(session).augmentation_by_id(command.id)
        assert observed is not None
        return observed


def test_base_completed_needs_real_files_before_registration(
    engine: Engine, tmp_path: Path, real_video_bytes: bytes
) -> None:
    dataset_id, execution_id = _seed(engine)
    command = _request(engine, dataset_id, execution_id, "real-output")
    _input(tmp_path)
    with AugmentationServer() as server:
        bound = _run(engine, tmp_path, server, command)
        assert bound.status.value == "bound"
        assert bound.base_augmentation_id == "data-1_augmented_0"
        server.status = "completed"
        missing = _run(engine, tmp_path, server, command)
        assert missing.base_observation["status"] == "completed"
        assert missing.result_status == "not_registered"
        assert missing.result_failure_code
        assert bound.base_augmentation_id is not None
        output = tmp_path / bound.base_augmentation_id
        output.mkdir()
        (output / "clip.mp4").write_bytes(real_video_bytes)
        (output / "bcq.json").write_text('[{"video": "clip.mp4"}]')
        completed = _run(engine, tmp_path, server, command)
        assert completed.status.value == "completed"
        assert completed.result_status == "registered"
        assert len(completed.result_manifest["files"]) == 2
        for entry in completed.result_manifest["files"]:
            assert entry["sha256"]
            assert (tmp_path / "registered" / entry["object_key"]).stat().st_size == entry["size"]
        assert _run(engine, tmp_path, server, command).result_manifest == completed.result_manifest
        assert len(server.starts) == 1


def _cancel(engine: Engine, command: AugmentationCommand) -> AugmentationCommand:
    with session_factory(engine).begin() as session:
        return cancel_augmentation(
            dataset_id=command.dataset_id,
            command_id=command.id,
            caller=_caller(Permission.DATASET_EDIT),
            now=datetime.now(UTC),
            datasets=PostgresDatasetRepository(session),
        )


def test_failed_result_copy_reuses_frozen_candidate_keys_without_orphaning_files(
    engine: Engine,
    tmp_path: Path,
    real_video_bytes: bytes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dataset_id, execution_id = _seed(engine)
    command = _request(engine, dataset_id, execution_id, "copy-failed-once")
    _input(tmp_path)
    with AugmentationServer() as server:
        bound = _run(engine, tmp_path, server, command)
        assert bound.base_augmentation_id is not None
        output = tmp_path / bound.base_augmentation_id
        output.mkdir()
        (output / "clip.mp4").write_bytes(real_video_bytes)
        (output / "bcq.json").write_text('[{"video": "clip.mp4"}]')
        server.status = "completed"
        original_copy = AugmentationVolume.copy_result
        copies = 0

        def fail_once_after_copy(
            self: AugmentationVolume,
            target: AugmentationCommand,
            files: list[dict[str, Any]],
            storage: ObjectStorage,
        ) -> None:
            nonlocal copies
            original_copy(self, target, files, storage)
            copies += 1
            if copies == 1:
                raise OSError("result copy acknowledgement lost")

        monkeypatch.setattr(AugmentationVolume, "copy_result", fail_once_after_copy)
        unregistered = _run(engine, tmp_path, server, command)
        assert unregistered.status.value == "bound"
        assert unregistered.result_status == "not_registered"
        frozen = unregistered.result_manifest["files"]
        assert len(frozen) == 2
        completed = _run(engine, tmp_path, server, command)
        assert completed.status.value == "completed"
        assert completed.result_manifest["files"] == frozen
        assert copies == 2
        root = tmp_path / "registered" / "augmentation-results" / str(command.id)
        files = [path for path in root.rglob("*") if path.is_file()]
        assert len(files) == len(frozen)
        assert len(server.starts) == 1


def test_lost_response_blocks_new_commands_across_worker_restarts(
    engine: Engine, tmp_path: Path
) -> None:
    dataset_id, execution_id = _seed(engine)
    first = _request(engine, dataset_id, execution_id, "lost-response")
    second = _request(engine, dataset_id, execution_id, "another-command")
    _input(tmp_path)
    with AugmentationServer() as server:
        server.drop_response = True
        unknown = _run(engine, tmp_path, server, first)
        assert unknown.status.value == "result_unknown"
        assert unknown.base_augmentation_id is None
        assert unknown.dispatched_at is not None
        server.drop_response = False
        assert _run(engine, tmp_path, server, first).status.value == "result_unknown"
        assert _run(engine, tmp_path, server, second).status.value == "pending"
        with pytest.raises(DatasetRefusedError) as refused:
            _cancel(engine, unknown)
        assert refused.value.code == DatasetRefusalCode.STATE_CONFLICT
        assert len(server.starts) == 1


def test_pg_intent_survives_expired_worker_lease_and_concurrent_commands(
    engine: Engine, tmp_path: Path
) -> None:
    dataset_id, execution_id = _seed(engine)
    first = _request(engine, dataset_id, execution_id, "concurrent-first")
    second = _request(engine, dataset_id, execution_id, "concurrent-second")
    _input(tmp_path)
    with AugmentationServer() as server, ThreadPoolExecutor(max_workers=1) as workers:
        server.post_gate = Event()
        future = workers.submit(_run, engine, tmp_path, server, first)
        try:
            assert server.started.wait(5)
            with session_factory(engine).begin() as session:
                assert (
                    PostgresJobRepository(session).recover_stale_running(
                        job_type=JobType.DATASET_AUGMENTATION,
                        now=datetime.now(UTC) + timedelta(hours=1),
                        stale_after_seconds=1,
                    )
                    == 1
                )
            assert _run(engine, tmp_path, server, second).status.value == "pending"
            assert _run(engine, tmp_path, server, first).status.value == "result_unknown"
            assert len(server.starts) == 1
        finally:
            server.post_gate.set()
        assert future.result(timeout=5).status.value == "bound"
        assert _run(engine, tmp_path, server, second).status.value == "pending"
        assert len(server.starts) == 1


def test_only_undispatched_cancel_and_failure_never_reopen_or_replace_history(
    engine: Engine, tmp_path: Path
) -> None:
    dataset_id, execution_id = _seed(engine)
    cancelled = _request(engine, dataset_id, execution_id, "cancel-before-post")
    assert _cancel(engine, cancelled).status.value == "cancelled"
    _input(tmp_path)
    with AugmentationServer() as server:
        assert _run(engine, tmp_path, server, cancelled).status.value == "cancelled"
        assert server.starts == []
        first = _request(engine, dataset_id, execution_id, "eventually-failed")
        bound = _run(engine, tmp_path, server, first)
        with pytest.raises(DatasetRefusedError) as refused:
            _cancel(engine, bound)
        assert refused.value.code == DatasetRefusalCode.STATE_CONFLICT
        server.query_error = True
        unavailable = _run(engine, tmp_path, server, first)
        assert unavailable.status.value == "bound"
        assert unavailable.last_observed_at is None
        server.query_error = False
        server.status = "failed"
        failed = _run(engine, tmp_path, server, first)
        assert failed.status.value == "failed"
        assert failed.result_status == "not_registered"
        assert _request(engine, dataset_id, execution_id, "eventually-failed") == failed
        assert _run(engine, tmp_path, server, failed) == failed
        with (
            pytest.raises(DatasetRefusedError) as conflict,
            session_factory(engine).begin() as session,
        ):
            request_augmentation(
                dataset_id=dataset_id,
                execution_id=execution_id,
                frame_drop_iterations=2,
                idempotency_key="eventually-failed",
                caller=_caller(Permission.DATASET_EDIT),
                now=datetime.now(UTC),
                datasets=PostgresDatasetRepository(session),
                jobs=PostgresJobRepository(session),
            )
        assert conflict.value.code == DatasetRefusalCode.IDEMPOTENCY_CONFLICT
        next_command = _request(engine, dataset_id, execution_id, "after-proven-terminal")
        server.status = "running"
        next_bound = _run(engine, tmp_path, server, next_command)
        assert next_bound.base_augmentation_id == "data-1_augmented_1"
        assert _run(engine, tmp_path, server, failed) == failed
        assert len(server.starts) == 2


def test_missing_referenced_media_does_not_register_unrelated_video(
    engine: Engine, tmp_path: Path, real_video_bytes: bytes
) -> None:
    dataset_id, execution_id = _seed(engine)
    command = _request(engine, dataset_id, execution_id, "missing-reference")
    _input(tmp_path)
    with AugmentationServer() as server:
        bound = _run(engine, tmp_path, server, command)
        assert bound.base_augmentation_id is not None
        output = tmp_path / bound.base_augmentation_id
        output.mkdir()
        (output / "unrelated.mp4").write_bytes(real_video_bytes)
        (output / "bcq.json").write_text('[{"video": "missing.mp4"}]')
        server.status = "completed"
        unregistered = _run(engine, tmp_path, server, command)
        assert unregistered.base_observation["status"] == "completed"
        assert unregistered.result_status == "not_registered"
        assert unregistered.result_failure_code == "AUGMENTATION_REGISTRATION_FAILED"


def test_real_nvidia_handlers_bind_only_response_identity_and_keep_success_history(
    engine: Engine, tmp_path: Path, real_video_bytes: bytes
) -> None:
    dataset_id, execution_id = _seed(engine)
    first = _request(engine, dataset_id, execution_id, "nvidia-original-handler")
    _input(tmp_path)
    with NvidiaAugmentationHttpServer(tmp_path) as server:
        bound = _run(engine, tmp_path, server, first)
        identity = bound.base_augmentation_id
        assert identity == "data-1_augmented_0"
        assert server.rows[identity].dataset_id == first.label_data_id
        assert bound.result_manifest["input_files"]
        output = tmp_path / identity / "bcq"
        output.mkdir()
        (output / "videos").mkdir()
        (output / "videos/clip.mp4").write_bytes(real_video_bytes)
        (output / "bcq.json").write_text('[{"video":"videos/clip.mp4","conversations":[]}]')
        server.rows[identity].status = "completed"
        complete = _run(engine, tmp_path, server, first)
        assert complete.result_status == "registered"
        saved_files = {
            entry["object_key"]: (tmp_path / "registered" / entry["object_key"]).read_bytes()
            for entry in complete.result_manifest["files"]
        }
        second = _request(engine, dataset_id, execution_id, "nvidia-next-command")
        next_bound = _run(engine, tmp_path, server, second)
        assert next_bound.base_augmentation_id == "data-1_augmented_1"
        assert (output / "videos/clip.mp4").read_bytes() == real_video_bytes
        assert _run(engine, tmp_path, server, first).result_manifest == complete.result_manifest
        assert all(
            (tmp_path / "registered" / key).read_bytes() == content
            for key, content in saved_files.items()
        )
