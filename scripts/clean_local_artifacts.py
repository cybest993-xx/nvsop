#!/usr/bin/env python3
"""只删除仓库明确拥有的可再生本地产物。"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

# .nvsop/dev-main 持有凭据、本地 secret 和固定开发实例状态，
# 因此故意不进入通用制品清理范围。
ARTIFACT_PATHS = (
    Path(".nvsop/artifacts"),
    Path("apps/control-web/coverage"),
    Path("apps/control-web/dist"),
    Path("apps/control-web/playwright-report"),
    Path("apps/control-web/test-results"),
)

# landing/ 含队列 receipt、repair claim/resume marker 与 agent log；BLOCKED 修复期间不是可再生产物。
# 日常 clean 保留它，只有任务结束前的 purge 才移除。
LANDING_ARTIFACT_PATH = Path(".nvsop/artifacts/landing")

ENVIRONMENT_PATHS = (
    Path(".nvsop/cache"),
    Path(".nvsop/tools"),
    Path(".nvsop/venv"),
    # pnpm/setuptools 当前仍要求这些传统布局。
    Path("node_modules"),
    Path("apps/control-web/node_modules"),
    # .nvsop 本地状态根引入前留下的旧生成路径。
    Path(".import_linter_cache"),
    Path(".mypy_cache"),
    Path(".pytest_cache"),
    Path(".ruff_cache"),
    Path(".venv"),
    Path(".tmp/tools"),
    Path(".husky/_"),
    Path("--version/_"),
    Path("apps/control-api/.pytest_cache"),
    Path("apps/edge-runtime/.mypy_cache"),
    Path("apps/edge-runtime/.pytest_cache"),
    Path("apps/edge-runtime/.ruff_cache"),
    Path("apps/edge-runtime/.venv"),
    Path("packages/contracts/.pytest_cache"),
)

GENERATED_METADATA_ROOTS = (
    Path("apps/control-api/src"),
    Path("apps/edge-runtime/src"),
    Path("packages/contracts/src"),
)

BYTECODE_ROOTS = (
    Path("apps/control-api"),
    Path("apps/edge-runtime"),
    Path("packages/contracts"),
    Path("scripts"),
    Path("tests"),
    Path("vendor/sop-monitoring-blueprints"),
)


def remove_path(path: Path) -> bool:
    if path.is_symlink() or path.is_file():
        path.unlink()
        return True
    if path.is_dir():
        shutil.rmtree(path)
        return True
    return False


def clean(root: Path = REPOSITORY_ROOT, *, purge: bool = False) -> list[Path]:
    removed: list[Path] = []

    paths = ARTIFACT_PATHS + ENVIRONMENT_PATHS if purge else ARTIFACT_PATHS
    for relative in paths:
        path = root / relative
        if (
            not purge
            and relative == Path(".nvsop/artifacts")
            and path.is_dir()
            and not path.is_symlink()
        ):
            for child in path.iterdir():
                child_relative = relative / child.name
                if child_relative == LANDING_ARTIFACT_PATH:
                    continue
                if remove_path(child):
                    removed.append(child_relative)
            continue
        if remove_path(path):
            removed.append(relative)

    if not purge:
        return removed

    generated: list[Path] = []
    for relative in GENERATED_METADATA_ROOTS:
        base = root / relative
        if base.is_dir():
            generated.extend(path for path in base.rglob("*.egg-info") if path.is_dir())
    for relative in BYTECODE_ROOTS:
        base = root / relative
        if base.is_dir():
            generated.extend(path for path in base.rglob("__pycache__") if path.is_dir())

    for path in sorted(set(generated), key=lambda item: len(item.parts), reverse=True):
        if remove_path(path):
            removed.append(path.relative_to(root))

    return removed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--purge", action="store_true", help="同时清除任务环境、工具和旧缓存")
    arguments = parser.parse_args()
    for path in clean(purge=arguments.purge):
        print(path.as_posix())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
