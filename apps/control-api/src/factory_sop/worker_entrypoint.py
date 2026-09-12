"""训练视频 worker 的独立组合根。"""

from __future__ import annotations

import os
from collections.abc import Mapping

from factory_sop.dataset.adapters.dependencies import (
    annotation_runtime,
    usage_runtime,
    validation_runtime,
)
from factory_sop.job.adapters.worker import run_worker as run_job_worker


def run_worker(environment: Mapping[str, str]) -> None:
    """用真实数据集适配器装配运行时后启动 job worker。"""
    run_job_worker(
        environment,
        runtime_factory=validation_runtime,
        usage_runtime_factory=usage_runtime,
        annotation_runtime_factory=annotation_runtime,
    )


if __name__ == "__main__":
    run_worker(os.environ)
