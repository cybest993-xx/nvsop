"""原 DDM 查询 handler/schema/model 经 HTTP 执行；只隔离训练导入和外部任务库。"""

from __future__ import annotations

import ast
import importlib.util
import logging
import os
import socket
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from uuid import uuid4

import uvicorn
from fastapi import FastAPI, HTTPException

BASE = (
    Path(__file__).resolve().parents[3]
    / "vendor/sop-monitoring-blueprints/microservices/sop-training-bp/microservices/ddm-training-ms"
)


def _load(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"ddm_fixture_{path.stem}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _JobCache:
    """真实 handler 的最小内存缓存替身：命中有无 PID 由测试显式控制。"""

    def __init__(self, values: dict[str, dict[str, Any]]) -> None:
        self._values = values

    def get(self, job_id: str) -> dict[str, Any] | None:
        return self._values.get(job_id)

    def update(self, job_id: str, **fields: object) -> None:
        self._values.setdefault(job_id, {}).update(fields)


class DdmHttpServer:
    """start 是故障注入边界；all_jobs/status/cancel 运行未经改写的原 handler。"""

    def __init__(self) -> None:
        self.jobs: dict[str, Any] = {}
        self.starts: list[tuple[str, str]] = []
        self.queries: list[str] = []
        self.before_start: Callable[[], None] = lambda: None
        self.start_error: int | None = None
        self.accept = True
        self.query_error = False
        self.before_response: Callable[[], None] = lambda: None
        # 取消入口的可控协作者：缓存决定是否存在 PID，terminate 决定进程能否终止。
        self.cache: dict[str, dict[str, Any]] = {}
        self.terminated: list[int] = []
        self.terminate_succeeds = True
        self._model = _load(BASE / "validation/postgres_validation.py").TrainingJob
        schema = _load(BASE / "validation/request_validation.py")
        const = _load(BASE / "utils/constant.py")
        app = FastAPI()
        namespace = {
            "postgres_db": self,
            "training_jobs_cache": _JobCache(self.cache),
            "TrainingStatus": schema.TrainingStatus,
            "const": const,
            "datetime": datetime,
            "os": os,
            "terminate_process_tree": self._terminate_process_tree,
            "HTTPException": HTTPException,
            "logger": logging.getLogger("ddm-fixture"),
        }
        tree = ast.parse((BASE / "app.py").read_text())
        for name, verb, route in (
            ("get_all_jobs", "get", "/api/v1/fine-tuning/all_jobs"),
            ("get_training_status", "get", "/api/v1/fine-tuning/status/{job_id}"),
            ("cancel_fine_tuning", "post", "/api/v1/fine-tuning/cancel/{job_id}"),
        ):
            node = next(
                n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == name
            )
            node.decorator_list = []
            exec(
                compile(ast.Module(body=[node], type_ignores=[]), str(BASE / "app.py"), "exec"),
                namespace,
            )
            getattr(app, verb)(route)(namespace[name])

        @app.post("/api/v1/fine-tuning/start")
        def start(dataset_id: str, validation_dataset_id: str) -> dict[str, str]:
            self.starts.append((dataset_id, validation_dataset_id))
            self.before_start()
            job_id = self.add_job(dataset_id) if self.accept else str(uuid4())
            self.before_response()
            if self.start_error is not None:
                raise HTTPException(self.start_error, "accepted but response lost")
            return {"job_id": job_id}

        self.app = app
        self._socket = socket.socket()
        self._socket.bind(("127.0.0.1", 0))
        self.url = f"http://127.0.0.1:{self._socket.getsockname()[1]}"
        self._server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
        self._thread = threading.Thread(
            target=self._server.run, kwargs={"sockets": [self._socket]}, daemon=True
        )

    def add_job(self, dataset_id: str, *, job_id: str | None = None) -> str:
        job_id = job_id or str(uuid4())
        self.jobs[job_id] = self._model(
            id=job_id,
            aug_dataset_id=dataset_id,
            status="queued",
            progress=0.0,
            current_step=0,
            total_steps=10,
            loss=None,
            process_pid=None,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        return job_id

    def cache_job(self, job_id: str, *, process_pid: int | None = None) -> None:
        """按基座真实缓存形状放入内存缓存，让取消走有/无 PID 分支。"""
        document = self.jobs[job_id].to_dict()
        document["process_pid"] = process_pid
        document["log_file_path"] = None
        self.cache[job_id] = document

    def _terminate_process_tree(self, process_pid: int, timeout: int = 30) -> bool:
        del timeout
        self.terminated.append(process_pid)
        return self.terminate_succeeds

    async def update_training_job(self, job_id: str, **fields: object) -> None:
        """镜像基座 DB 更新：只写入模型已有字段，未知字段忽略。"""
        job = self.jobs.get(job_id)
        if job is None:
            return
        for key, value in fields.items():
            if hasattr(job, key):
                setattr(job, key, value)

    def add_malformed_job(self, *, aug_dataset_id: str) -> None:
        """注入一个键与任务体身份不一致的非法条目，用于验证 malformed 结果不被绑定。"""
        key = "not-a-uuid"
        self.jobs[key] = SimpleNamespace(
            id=key,
            to_dict=lambda: {"id": "different-id", "aug_dataset_id": aug_dataset_id},
        )

    async def list_training_jobs(self) -> list[Any]:
        self.queries.append("all_jobs")
        if self.query_error:
            raise HTTPException(503, "unreachable")
        return list(self.jobs.values())

    async def get_training_job(self, job_id: str) -> object:
        self.queries.append(job_id)
        if self.query_error:
            raise HTTPException(503, "unreachable")
        return self.jobs.get(job_id)

    def __enter__(self) -> DdmHttpServer:
        self._thread.start()
        deadline = time.monotonic() + 5
        while not self._server.started:
            if time.monotonic() >= deadline:
                raise RuntimeError("DDM fixture HTTP startup timed out")
            time.sleep(0.01)
        return self

    def __exit__(self, *_args: object) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=5)
        self._socket.close()
        assert not self._thread.is_alive()
