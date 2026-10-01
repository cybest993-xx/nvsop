#!/usr/bin/env python3
"""用既有 uv 解析标注环境；默认只核对输入与冻结的完整版本集合。"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import tempfile
from pathlib import Path

BASE_BACKEND = Path(
    "vendor/sop-monitoring-blueprints/microservices/sop-training-bp/"
    "microservices/video-annotator-ms/annotation_backend"
)
LOCK = Path("deploy/dev/annotation-requirements.lock")
CONSTRAINTS = Path("deploy/dev/annotation-constraints.txt")


def compile_lock(root: Path, *, uv: str, write: bool) -> int:
    lock = root / LOCK
    artifacts = root / ".nvsop/artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    if not write and not lock.is_file():
        print("annotation lock is missing; run make annotation-lock")
        return 1
    with tempfile.TemporaryDirectory(prefix="annotation-lock-", dir=artifacts) as temporary:
        candidate = Path(temporary) / "requirements.txt"
        if lock.is_file():
            # 沿用已锁版本；核对不会因为包源发布了更新版本而误报过期。
            shutil.copyfile(lock, candidate)
        subprocess.run(
            [
                uv,
                "pip",
                "compile",
                str(BASE_BACKEND / "requirements.txt"),
                "--constraint",
                str(CONSTRAINTS),
                "--python-version",
                "3.10",
                "--python-platform",
                "x86_64-manylinux_2_28",
                "--no-header",
                "--no-annotate",
                "--output-file",
                str(candidate),
            ],
            cwd=root,
            check=True,
            stdout=subprocess.DEVNULL,
        )
        content = candidate.read_bytes()
        if write:
            lock.write_bytes(content)
        elif content != lock.read_bytes():
            print("annotation lock does not match its inputs; run make annotation-lock")
            return 1
    print("annotation lock matches its inputs")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uv", default="uv")
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    return compile_lock(Path(__file__).resolve().parents[1], uv=args.uv, write=args.write)


if __name__ == "__main__":
    raise SystemExit(main())
