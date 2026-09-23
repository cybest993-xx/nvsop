"""数据集 worker 依赖装配的生命周期边界测试。"""

from typing import cast

import pytest
from sqlalchemy.orm import Session, sessionmaker

from factory_sop.dataset.adapters import dependencies
from factory_sop.dataset.adapters.artifact_execution import PostgresDatasetArtifactExecutor
from factory_sop.settings import Settings


def test_artifact_executor_defers_ddm_generator_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_generator() -> None:
        raise AssertionError("制品执行前不应定位 NVIDIA DDM utility")

    monkeypatch.setattr(
        dependencies,
        "NvidiaDdmAnnotationGenerator",
        unexpected_generator,
    )

    executor = dependencies.artifact_executor(
        cast(Settings, object()),
        cast(sessionmaker[Session], object()),
    )

    assert isinstance(executor, PostgresDatasetArtifactExecutor)
