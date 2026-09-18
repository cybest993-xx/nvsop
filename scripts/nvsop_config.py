"""读取仓库唯一的 NVSOP 机器配置声明。"""

from __future__ import annotations

import tomllib
from pathlib import Path


def load_center_modules(pyproject: Path) -> frozenset[str]:
    """从 `[tool.nvsop]` 读取 Center product-module 声明。"""
    with pyproject.open("rb") as handle:
        config = tomllib.load(handle)
    try:
        modules = config["tool"]["nvsop"]["center_modules"]
    except (KeyError, TypeError) as error:
        raise ValueError("[tool.nvsop].center_modules is required") from error
    if (
        not isinstance(modules, list)
        or not modules
        or not all(isinstance(module, str) and module for module in modules)
    ):
        raise ValueError("[tool.nvsop].center_modules must be a non-empty list of strings")
    return frozenset(modules)
