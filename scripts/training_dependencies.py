#!/usr/bin/env python3
"""冻结四个固定训练目标，并从原 Vendor Dockerfile 生成部署构建输入。"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import tempfile
from pathlib import Path

VENDOR = Path("vendor/sop-monitoring-blueprints/microservices/sop-training-bp/microservices")
OWNER = Path("deploy/dev")
TARGETS = {
    "augmentation": ("data-generation-pipeline", "3.10"),
    "cr": ("cr-training-ms", "3.12"),
    "ddm": ("ddm-training-ms", "3.10"),
    "evaluation": ("evaluation-ms", "3.12"),
    "control": (None, "3.10"),
    "cr-base": (None, "3.12"),
}
CUDA = (
    "nvcr.io/nvidia/cuda:12.8.1-devel-ubuntu22.04@"
    "sha256:6617a625f4090c76c545a0e7d63f2e441718ef9af7f4efe7dd1242a29e289fd7"
)
PYTORCH = (
    "nvcr.io/nvidia/pytorch:24.04-py3@"
    "sha256:0a8d976bfb2d77a16e9e1c80eeb25ccf3e44cef93c14e24f4434e01398fb28ee"
)
SOURCE_REPLACEMENTS = {
    "git clone -b ${GDRCOPY_VERSION} https://github.com/NVIDIA/gdrcopy.git /tmp/gdrcopy": (
        "git clone -b ${GDRCOPY_VERSION} https://github.com/NVIDIA/gdrcopy.git /tmp/gdrcopy "
        "&& git -C /tmp/gdrcopy checkout 9ecf5e58bb947dcd20a759c63f4f1cbe2b1b6458"
    ),
    (
        "git clone --branch v${FLASH_ATTN_VERSION} --single-branch "
        "https://github.com/Dao-AILab/flash-attention.git"
    ): (
        "git clone --branch v${FLASH_ATTN_VERSION} --single-branch "
        "https://github.com/Dao-AILab/flash-attention.git "
        "&& git -C flash-attention checkout 060c9188beec3a8b62b33a3bfa6d5d2d44975fab"
    ),
    "git clone --branch v0.3.9 --depth 1 https://github.com/nvidia-cosmos/cosmos-rl.git": (
        "git clone --branch v0.3.9 --depth 1 https://github.com/nvidia-cosmos/cosmos-rl.git "
        "&& git -C cosmos-rl checkout 5509ac0b9db541784e91231d8f7e2d96a1ff8c81"
    ),
    "NVIDIA/apex@bf903a2": "NVIDIA/apex@bf903a2d519a5c6d2e5b4defb05784fdd9813e87",
    "fanshiqing/grouped_gemm@v1.1.4": (
        "fanshiqing/grouped_gemm@172fada89fa7364fe5d026b3a0dfab58b591ffdd"
    ),
    "ARG DEEPEP_REF=567632d": "ARG DEEPEP_REF=567632dd59810d77b3cc05553df953cc0f779799",
    "transformer_engine[pytorch]==2.14.1": "transformer_engine[pytorch]==2.13.0",
}


def locks(root: Path, *, uv: str, write: bool) -> int:
    artifacts = root / ".nvsop/artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="training-lock-", dir=artifacts) as temporary:
        for target, (_, python) in TARGETS.items():
            lock = root / OWNER / f"training-{target}.lock"
            candidate = Path(temporary) / f"{target}.lock"
            if lock.is_file():
                shutil.copyfile(lock, candidate)
            command = [
                uv,
                "pip",
                "compile",
                str(OWNER / f"training-{'cr' if target == 'cr-base' else target}.in"),
                "--python-version",
                python,
                "--python-platform",
                "x86_64-manylinux_2_28",
                "--no-header",
                "--no-annotate",
                "--output-file",
                str(candidate),
            ]
            if target == "cr-base":
                command += ["--override", str(OWNER / "training-cr-base.in")]
            if target in {"cr", "ddm", "evaluation", "cr-base"}:
                command += [
                    "--extra-index-url",
                    "https://download.pytorch.org/whl/cu128",
                    "--index-strategy",
                    "unsafe-best-match",
                ]
            subprocess.run(command, cwd=root, check=True, stdout=subprocess.DEVNULL)
            if write:
                lock.write_bytes(candidate.read_bytes())
            elif not lock.is_file() or candidate.read_bytes() != lock.read_bytes():
                return 1
    return 0


def prepare(root: Path) -> None:
    output = root / ".nvsop/artifacts/training-build"
    for target, (service, _) in TARGETS.items():
        if service is None:
            continue
        context = output / target
        # 只覆盖本调用方拥有的生成 context，不清理工作树或其他服务。
        shutil.copytree(
            root / VENDOR / service,
            context,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        runtime = (root / OWNER / f"training-{target}.lock").read_text()
        (context / "nvsop-runtime.lock").write_text(runtime)
        # CR base 的 torchao 先装 0.16，package 阶段按基座原行为降为 0.15。
        base = (root / OWNER / "training-cr-base.lock").read_text() if target == "cr" else runtime
        (context / "nvsop-base.lock").write_text(base)
        shutil.copyfile(root / OWNER / "training-bootstrap.py", context / "training_bootstrap.py")
        source = (root / VENDOR / service / "Dockerfile").read_text()
        source = source.replace("nvcr.io/nvidia/cuda:${CUDA_VERSION}-devel-ubuntu22.04", CUDA)
        source = source.replace("nvcr.io/nvidia/pytorch:24.04-py3", PYTORCH)
        for old, new in SOURCE_REPLACEMENTS.items():
            source = source.replace(old, new)
        source = source.replace("ARG MAX_JOBS=8", "ARG MAX_JOBS=1")
        source = source.replace("make -j$(nproc)", "make -j1")
        lines = []
        for line in source.splitlines():
            lines.append(line)
            if line.startswith("FROM "):
                stage = line.rsplit(" AS ", 1)[-1]
                constraint = (
                    "nvsop-base.lock"
                    if stage in {"no-efa-base", "source-build", "pre-package"}
                    else "nvsop-runtime.lock"
                )
                lines += [
                    "COPY nvsop-base.lock nvsop-runtime.lock /opt/nvsop/",
                    f"ENV PIP_CONSTRAINT=/opt/nvsop/{constraint} "
                    f"PIP_BUILD_CONSTRAINT=/opt/nvsop/{constraint} MAX_JOBS=1 NVCC_THREADS=1",
                ]
        # 固定工具也在锁内；保留原构建命令、计算源码和显式 xformers 卸载。
        lines += [
            "COPY training_bootstrap.py ./training_bootstrap.py",
            'CMD ["/bin/sh", "-c", "exec uvicorn training_bootstrap:app '
            '--host 0.0.0.0 --port ${SERVICE_PORT}"]',
        ]
        (context / "Dockerfile").write_text("\n".join(lines) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uv", default="uv")
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--prepare", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    if args.prepare:
        prepare(root)
        return 0
    return locks(root, uv=args.uv, write=args.write)


if __name__ == "__main__":
    raise SystemExit(main())
