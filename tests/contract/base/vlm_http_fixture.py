"""原 VLM 查询/取消 handler 经 HTTP 执行；start 是唯一的故障注入边界。

`get_all_jobs` / `get_training_status` / `cancel_fine_tuning` 直接取自
`microservices/cr-training-ms/app.py` 的函数体（去掉装饰器后重新注册），因此中心 adapter
面对的是基座真实响应模型；训练导入、任务库与进程终止被替换为测试替身，因为它们需要 GPU、
真实 PostgreSQL 与真实进程树。
"""

from __future__ import annotations

import ast
import importlib.util
import logging
import os
import socket
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any
from uuid import uuid4

import uvicorn
from fastapi import FastAPI, HTTPException

BASE = (
    Path(__file__).resolve().parents[3]
    / "vendor/sop-monitoring-blueprints/microservices/sop-training-bp/microservices/cr-training-ms"
)
_RUNNING_STATUS = "running"
_CANCELLED_STATUS = "cancelled"


def _load(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"vlm_fixture_{path.stem}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _JobCache:
    """基座的进程内任务缓存替身；取消分支取决于 process_pid。"""

    def __init__(self, server: VlmHttpServer) -> None:
        self._server = server
        self._entries: dict[str, dict[str, Any]] = {}

    def set(self, job_id: str, process_pid: int | None) -> None:
        # 缓存条目的字段集与基座 insert_training_job 的写入一致：status 查询先读缓存。
        now = datetime.now(UTC)
        self._entries[job_id] = {
            "job_id": job_id,
            "status": _RUNNING_STATUS,
            "progress": 0.4,
            "current_step": 4,
            "total_steps": 10,
            "loss": 0.5,
            "created_at": now,
            "updated_at": now,
            "process_pid": process_pid,
            "log_file_path": None,
        }

    def get(self, job_id: str) -> dict[str, Any] | None:
        return self._entries.get(job_id)

    def update(self, job_id: str, **changes: object) -> None:
        entry = self._entries.get(job_id)
        assert entry is not None
        entry.update(changes)
        self._server.cancel_updates.append((job_id, dict(changes)))


class VlmHttpServer:
    """真实 handler + 可注入的 start 故障；cancel 走基座真实分支。"""

    def __init__(self) -> None:
        self.jobs: dict[str, Any] = {}
        self.starts: list[str] = []
        self.queries: list[str] = []
        self.cancel_updates: list[tuple[str, dict[str, object]]] = []
        self.before_start: Any = lambda: None
        self.start_error: int | None = None
        self.accept = True
        self.query_error = False
        self.terminated: list[int] = []
        self._model = _load(BASE / "validation/postgres_validation.py").TrainingJob
        schema = _load(BASE / "validation/request_validation.py")
        self.cache = _JobCache(self)
        app = FastAPI()
        namespace = {
            "postgres_db": self,
            "training_jobs_cache": self.cache,
            "TrainingStatus": schema.TrainingStatus,
            "HTTPException": HTTPException,
            "logger": logging.getLogger("vlm-fixture"),
            "os": os,
            "datetime": datetime,
            "terminate_process_tree": self._terminate_process_tree,
            "const": type(
                "const",
                (),
                {"RUNNING_STATUS": _RUNNING_STATUS, "CANCELLED_STATUS": _CANCELLED_STATUS},
            ),
        }
        tree = ast.parse((BASE / "app.py").read_text())
        for name, method, route in (
            ("get_all_jobs", "get", "/api/v1/fine-tuning/all_jobs"),
            ("get_training_status", "get", "/api/v1/fine-tuning/status/{job_id}"),
            ("cancel_fine_tuning", "post", "/api/v1/fine-tuning/cancel/{job_id}"),
        ):
            node = next(
                n
                for n in tree.body
                if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef) and n.name == name
            )
            node.decorator_list = []
            exec(
                compile(ast.Module(body=[node], type_ignores=[]), str(BASE / "app.py"), "exec"),
                namespace,
            )
            getattr(app, method)(route)(namespace[name])

        @app.post("/api/v1/fine-tuning/start")
        def start(dataset_id: str) -> dict[str, str]:
            """故障注入边界：真实 start 会在基座创建任务后才可能丢失响应。"""
            self.starts.append(dataset_id)
            self.before_start()
            job_id = self.add_job(dataset_id) if self.accept else str(uuid4())
            if self.start_error is not None:
                raise HTTPException(self.start_error, "accepted but response lost")
            return {
                "job_id": job_id,
                "status": "queued",
                "message": "Fine-tuning job has been queued and will start shortly",
                "created_at": datetime.now(UTC).isoformat(),
            }

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
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        return job_id

    def complete_job(self, job_id: str) -> None:
        self.jobs[job_id].status = "completed"
        self.jobs[job_id].progress = 1.0
        self.jobs[job_id].updated_at = datetime.now(UTC)

    def fail_job(self, job_id: str) -> None:
        self.jobs[job_id].status = "failed"
        self.jobs[job_id].updated_at = datetime.now(UTC)

    def run_job(self, job_id: str, *, process_pid: int) -> None:
        """把任务放进进程内缓存并给出 PID，使真实取消 handler 走终止分支。"""
        self.cache.set(job_id, process_pid)

    def _terminate_process_tree(self, process_pid: int) -> bool:
        self.terminated.append(process_pid)
        return True

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

    def __enter__(self) -> VlmHttpServer:
        self._thread.start()
        deadline = time.monotonic() + 5
        while not self._server.started:
            if time.monotonic() >= deadline:
                raise RuntimeError("VLM fixture HTTP startup timed out")
            time.sleep(0.01)
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        del exc_type, exc, traceback
        self._server.should_exit = True
        self._thread.join(timeout=5)
        self._socket.close()
        assert not self._thread.is_alive()


__all__ = ["VlmHttpServer", "uuid4"]
