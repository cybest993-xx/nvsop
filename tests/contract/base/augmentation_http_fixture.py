"""原 NVIDIA POST/GET 经真实 HTTP 执行；只隔离 GPU 计算和训练数据库。"""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import logging
import os
import shutil
import socket
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path
from pprint import pformat
from types import ModuleType
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Query

BASE = (
    Path(__file__).resolve().parents[3]
    / "vendor/sop-monitoring-blueprints/microservices/sop-training-bp"
    / "microservices/data-generation-pipeline"
)


def _load(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"augmentation_fixture_{path.stem}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class NvidiaAugmentationHttpServer:
    """输入校验、计数命名、建目录、写任务及查询都运行未经改写的原 handler。"""

    def __init__(self, root: Path) -> None:
        constants = _load(BASE / "utils/constant.py")
        constants.DATASET_ROOT = str(root)
        models = _load(BASE / "validation/postgres_validation.py")
        response = _load(BASE / "validation/response_validation.py")
        self._augmentation = models.Augmentation
        self.rows: dict[str, Any] = {}
        self.processed: list[str] = []
        self._logger = logging.getLogger("augmentation-fixture")
        namespace = {
            "asyncio": asyncio,
            "os": os,
            "shutil": shutil,
            "datetime": datetime,
            "pformat": pformat,
            "traceback": traceback,
            "Dict": dict,
            "List": list,
            "Any": Any,
            "Query": Query,
            "HTTPException": HTTPException,
            "const": constants,
            "postgres_db": self,
            "app_logger": self._logger,
            "Augmentation": models.Augmentation,
            "AugmentationStage": models.AugmentationStage,
            "Video": models.Video,
            "Chunk": models.Chunk,
            "AugResponse": response.AugResponse,
            "AugmentationStatusResponse": response.AugmentationStatusResponse,
            "load_config_yaml": lambda _path: {},
            "scrub_secrets": lambda value: str(value),
        }
        helpers = ast.parse((BASE / "utils/utils.py").read_text())
        for node in helpers.body:
            if isinstance(node, ast.FunctionDef) and node.name in {
                "safe_dataset_path",
                "clean_and_create_dir",
            }:
                exec(
                    compile(
                        ast.Module(body=[node], type_ignores=[]),
                        str(BASE / "utils/utils.py"),
                        "exec",
                    ),
                    namespace,
                )
        tree = ast.parse((BASE / "app.py").read_text())
        service = next(
            n
            for n in tree.body
            if isinstance(n, ast.ClassDef) and n.name == "VLMAugmentationService"
        )
        methods = [
            n
            for n in service.body
            if isinstance(n, ast.FunctionDef)
            and n.name in {"find_video_folders", "_validate_video_folders"}
        ]
        service.body = methods
        exec(
            compile(ast.Module(body=[service], type_ignores=[]), str(BASE / "app.py"), "exec"),
            namespace,
        )
        processor = namespace["VLMAugmentationService"]()
        processor.process_all_actions = self.process_all_actions
        namespace["vlm_service"] = processor
        app = FastAPI()
        for name, route, method in (
            ("augment", "/api/v1/augment", "post"),
            ("get_augmentation_status", "/api/v1/augmentation_status/{dataset_id}", "get"),
            ("get_all_augmented_datasets", "/api/v1/augmented_datasets", "get"),
        ):
            node = next(
                n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == name
            )
            node.decorator_list = []
            exec(
                compile(ast.Module(body=[node], type_ignores=[]), str(BASE / "app.py"), "exec"),
                namespace,
            )
            getattr(app, method)(route)(namespace[name])
        self.app = app
        self._socket = socket.socket()
        self._socket.bind(("127.0.0.1", 0))
        self.url = f"http://127.0.0.1:{self._socket.getsockname()[1]}"
        self._server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
        self._thread = threading.Thread(
            target=self._server.run, kwargs={"sockets": [self._socket]}, daemon=True
        )

    async def list_data(self, *, schema: object, condition: dict[str, object]) -> list[Any]:
        if schema is not self._augmentation:
            return []
        return [
            row
            for row in self.rows.values()
            if all(getattr(row, key) == value for key, value in condition.items())
        ]

    async def insert_data(self, **values: object) -> None:
        row = self._augmentation(**values)
        self.rows[row.id] = row

    async def update_data(self, *, id: str, **values: object) -> None:  # noqa: A002 -- vendor API keyword
        for key, value in values.items():
            setattr(self.rows[id], key, value)

    async def get_data(self, *, schema: object, id: str) -> object | None:  # noqa: A002 -- vendor API keyword
        return self.rows.get(id)

    async def process_all_actions(self, *args: object, **kwargs: object) -> None:
        # 明确的外部计算 seam；不生成假产物，也不将 GPU 计算冒充已执行。
        self.processed.append(str(args[4]))

    def __enter__(self) -> NvidiaAugmentationHttpServer:
        self._thread.start()
        deadline = time.monotonic() + 5
        while not self._server.started:
            if time.monotonic() >= deadline:
                raise RuntimeError("NVIDIA contract HTTP startup timed out")
            time.sleep(0.01)
        return self

    def __exit__(self, *_args: object) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=5)
        self._socket.close()
        assert not self._thread.is_alive()
