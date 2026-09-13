"""固定开发脚本共用的 HTTP 列表校验和报告写入。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

from factory_sop.dataset.client import ControlPlaneClient, DatasetImportError


def list_items(client: ControlPlaneClient, path: str) -> list[dict[str, object]]:
    """读取正式列表响应并拒绝缺失或错误的 items 形状。"""
    result = client.request_json("GET", path, expected={200})
    items = result.get("items")
    if not isinstance(items, list):
        raise DatasetImportError(f"{path} 响应没有 items 列表")
    return [cast(dict[str, object], item) for item in items if isinstance(item, dict)]


def write_report(path: Path, value: dict[str, object]) -> None:
    """原子写入开发报告，避免状态面板读到半个 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
