"""原 DDM 查询 handler/schema/model 经 HTTP 执行；只隔离训练导入和外部任务库。"""

from __future__ import annotations

import ast
import importlib.util
import logging
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


class DdmHttpServer:
    """start 是故障注入边界；all_jobs/status 运行未经改写的原 handler。"""

    def __init__(self) -> None:
        self.jobs: dict[str, Any] = {}
        self.starts: list[tuple[str, str]] = []
        self.queries: list[str] = []
        self.before_start: Callable[[], None] = lambda: None
        self.start_error: int | None = None
        self.accept = True
        self.query_error = False
        self.before_response: Callable[[], None] = lambda: None
        self._model = _load(BASE / "validation/postgres_validation.py").TrainingJob
        schema = _load(BASE / "validation/request_validation.py")
        app = FastAPI()
        namespace = {
            "postgres_db": self,
            "training_jobs_cache": SimpleNamespace(get=lambda _id: None),
            "TrainingStatus": schema.TrainingStatus,
            "HTTPException": HTTPException,
            "logger": logging.getLogger("ddm-fixture"),
        }
        tree = ast.parse((BASE / "app.py").read_text())
        for name, route in (
            ("get_all_jobs", "/api/v1/fine-tuning/all_jobs"),
            ("get_training_status", "/api/v1/fine-tuning/status/{job_id}"),
        ):
            node = next(
                n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == name
            )
            node.decorator_list = []
            exec(
                compile(ast.Module(body=[node], type_ignores=[]), str(BASE / "app.py"), "exec"),
                namespace,
            )
            app.get(route)(namespace[name])

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
